"""T302 end to end: a real Worker + the real revise stage + the real notifier and bot
handlers, with a fake Telegram. The operator's walkthrough, as tests.

Reuses the T301 harness (real temp DB, scripted model, frozen real decks from
``tests/fixtures/t299_decks.json``; no test reads data/app.db and nothing is sent).
"""
from __future__ import annotations

import dataclasses

import pytest

from pipeline.approval.bot import (
    Pending,
    build_preview,
    handle_callback,
    handle_pending_reply,
    make_revision_notifier,
    parse_confirm_callback,
)
from pipeline.config import Settings
from pipeline.db import Database
from pipeline.errors import Retryforever
from pipeline.models import Status
from pipeline.revision import copy as rc
from tests.approval.test_bot import OPERATOR, FakeBot, _callback, _message
from tests.approval.test_revision_bot import Toasts
from tests.conftest import TEST_ENV, FakeHTTP
from tests.test_t301_blackbox import A, Env, ScriptLLM, act, plan, shas, trimmed


@pytest.fixture
def cfg(tmp_path) -> Settings:
    s = Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "t.db")})
    return dataclasses.replace(s, verify_enabled=False)


@pytest.fixture
async def env(tmp_path, cfg):
    db = await Database(tmp_path / "t.db").connect()
    media = tmp_path / "media"
    media.mkdir()
    yield Env(db, ScriptLLM(), FakeHTTP(), cfg, media)
    await db.close()


class Telegram(FakeBot):
    async def edit_message_text(self, text, chat_id, message_id):
        self.edited = [*getattr(self, "edited", []), (message_id, text)]


def wire(env: Env):
    bot, pending = Telegram(), Pending()
    return bot, pending, make_revision_notifier(bot, env.db, env.cfg, pending)


async def operator_says(env, bot, pending, i, note):
    """Regenerate -> prompt -> the operator's reply, through the real handlers."""
    ui = Toasts()
    await handle_callback(_callback(f"regen:{i}"), env.db, env.cfg, pending, answer=ui.answer, send=ui.send)
    return await handle_pending_reply(_message(note), env.db, env.cfg, pending, bot), ui


async def test_walkthrough_remove_a_slide_confirm_yes_and_read_the_changes(env):
    i, d = await env.make(A)
    bot, pending, notify = wire(env)

    got, ui = await operator_says(env, bot, pending, i, "remove slide 6 and make slide 3 shorter")
    assert got == "regen" and ui.sent[0].startswith(f"Revising item {i}.")
    env.llm.q["plan"].append(plan(["shorten slide 3"], act("edit_slide", ask=0, slide=3, instruction="shorter")))
    assert await env.tick(notify=notify) == 1

    # The question: nothing has changed yet, and the buttons say what they do.
    item = await env.db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM and shas(item.slides) == shas(d["slides"])
    question = bot.messages[-1]
    assert question.text.startswith("Remove slide 6? There is no undo")
    assert "Also doing: slide 3 reworded." in question.text
    yes, no = question.reply_markup.inline_keyboard[0]
    assert (yes.text, no.text) == ("✅ Yes, remove", "↩️ No, keep it")
    assert parse_confirm_callback(yes.callback_data)[:2] == (True, i)

    ui = Toasts()
    await handle_callback(
        _callback(yes.callback_data), env.db, env.cfg, pending, answer=ui.answer, send=ui.send, edit=ui.edit)
    assert ui.edited == ["Removing slide 6…"]

    env.llm.q["edit"].append(trimmed(d["slides"][2]))
    await env.tick(notify=notify)
    item, run = await env.last(i)
    assert item.status is Status.COMPOSED and len(item.slides) == len(d["slides"]) - 1

    from pipeline.revision.record import deck_fingerprint

    assert run["deck"] == deck_fingerprint(item.slides), "the run knows which deck it produced"

    # The preview the operator gets next: the Changes block first, the caption untouched below it.
    preview = build_preview(item).text
    first = preview.split("\n")[0]
    assert first.startswith("Revised per your note") or first.startswith("Partly revised")
    assert "✓ Slide 6 (was) — removed" in preview and "Slide numbers below are the new ones." in preview
    assert "✓ Slide 3 — reworded" in preview
    assert preview.endswith(build_preview(dataclasses.replace(item, revision=None)).text)
    assert item.caption in preview


async def test_walkthrough_no_keeps_the_slide_and_says_so_in_the_next_preview(env):
    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    await operator_says(env, bot, pending, i, "remove slide 6 and make the caption more casual")
    env.llm.q["plan"].append(plan(["casual caption"], act("rewrite_caption", ask=0, instruction="casual")))
    env.llm.q["caption"].append({"caption": "totally casual take " + d["caption"][:60], "hashtags": ["ai"]})
    await env.tick(notify=notify)
    _, no = bot.messages[-1].reply_markup.inline_keyboard[0]
    ui = Toasts()
    await handle_callback(_callback(no.callback_data), env.db, env.cfg, pending,
                          answer=ui.answer, send=ui.send, edit=ui.edit)
    assert ui.edited == ["Kept slide 6. Applying the rest…"]
    await env.tick(notify=notify)
    item, _ = await env.last(i)
    assert item.status is Status.RENDERED and len(item.slides) == len(d["slides"])
    preview = build_preview(item).text
    assert "– Slide 6: kept (you said no)" in preview and "✓ Caption — rewritten" in preview


async def test_walkthrough_a_vague_note_gets_the_question_then_the_rephrase_works(env):
    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    canary = "make it better CANARY-77"
    await operator_says(env, bot, pending, i, canary)
    env.llm.q["plan"].append(plan([]))  # nothing concrete
    await env.tick(notify=notify)
    item = await env.db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL and shas(item.slides) == shas(d["slides"])
    ask = bot.messages[-1].text
    assert "CANARY" not in ask and ask.startswith("I couldn't tell what to change")
    assert f"Slides in item {i}:" in ask and f'1 "{d["slides"][0]["headline"][:8]}' in ask
    assert pending.pop(OPERATOR) == ("regen", i)
    pending.set(OPERATOR, "regen", i)

    got = await handle_pending_reply(_message("make slide 3 shorter"), env.db, env.cfg, pending, bot)
    assert got == "regen" and (await env.db.get_item(i)).status is Status.REVISION_REQUESTED


async def test_walkthrough_the_deferral_message_is_sent_once_with_no_exception_text(env):
    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    await operator_says(env, bot, pending, i, "make slide 3 shorter")
    sent_before = len(bot.messages)
    for _ in range(4):
        env.llm.q["plan"].append(Retryforever("ollama at http://127.0.0.1:11434 timed out: CANARY-secret"))
        await env.db.clear_backoff(i)
        await env.tick(notify=notify)
    deferral = [m.text for m in bot.messages[sent_before:]]
    assert deferral == [rc.DEFERRED.format(id=i)]
    assert "CANARY" not in deferral[0] and "ollama" not in deferral[0].lower()


async def test_walkthrough_a_newer_note_replaces_an_open_question(env):
    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    await operator_says(env, bot, pending, i, "remove slide 6")
    await env.tick(notify=notify)
    yes, _ = bot.messages[-1].reply_markup.inline_keyboard[0]
    confirm_id = bot.messages[-1].message_id
    got, ui = await operator_says(env, bot, pending, i, "actually, shorten slide 3 instead")
    assert got == "regen" and bot.edited == [(confirm_id, rc.REPLACED_CONFIRM)]
    # the old Yes is now harmless
    ui = Toasts()
    await handle_callback(_callback(yes.callback_data), env.db, env.cfg, pending,
                          answer=ui.answer, send=ui.send, edit=ui.edit)
    assert ui.said[0] in (rc.CONFIRM_TOASTS[c] for c in rc.CONFIRM_TOASTS)
    assert ui.edited == [rc.REPLACED_CONFIRM] or ui.edited == [rc.CONFIRM_TOASTS[rc.ConfirmResult.NOT_WAITING]]
    env.llm.q["plan"].append(plan(["shorten slide 3"], act("edit_slide", ask=0, slide=3, instruction="shorter")))
    env.llm.q["edit"].append(trimmed(d["slides"][2]))
    await env.tick(notify=notify)
    item, _ = await env.last(i)
    assert len(item.slides) == len(d["slides"]), "slide 6 was never removed"


async def test_flag_off_the_whole_flow_is_the_legacy_regen(env):
    off = dataclasses.replace(env.cfg, revise_enabled=False)
    i, d = await env.make(A)
    pending = Pending()
    ui = Toasts()
    await handle_callback(_callback(f"regen:{i}"), env.db, off, pending, answer=ui.answer, send=ui.send)
    assert ui.sent == [] and "Regenerating item" in ui.said[0]
    await handle_pending_reply(_message("remove slide 6"), env.db, off, pending, Telegram())
    item = await env.db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note == "remove slide 6"
    assert item.revision_request is None and item.revision is None


async def test_a_legacy_regen_after_a_revision_does_not_show_the_old_changes(env):
    """Rolled back (REVISE_ENABLED=false) after using the new flow: the next deck is a
    different deck, so the previous run's report must not ride on its preview."""
    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    await operator_says(env, bot, pending, i, "make slide 3 shorter")
    env.llm.q["plan"].append(plan(["shorten slide 3"], act("edit_slide", ask=0, slide=3, instruction="shorter")))
    env.llm.q["edit"].append(trimmed(d["slides"][2]))
    await env.tick(notify=notify)
    item, _ = await env.last(i)
    assert build_preview(item).text.startswith("Revised per your note")
    other = [dict(s) for s in item.slides]
    other[0] = {**other[0], "headline": other[0]["headline"] + " (recomposed)"}
    stale = dataclasses.replace(item, slides=other)  # what a legacy compose leaves behind
    assert "Revised per your note" not in build_preview(stale).text


async def test_a_fresh_version_previews_as_a_rebuild_and_a_stale_run_never_rides_a_different_deck(env):
    """/skip: compose runs, the run records the NEW deck, the preview says the deck was rebuilt."""
    import copy

    from pipeline.revision.record import deck_fingerprint

    i, d = await env.make(A)
    bot, pending, notify = wire(env)
    await operator_says(env, bot, pending, i, "/skip")
    env.llm.q["compose"].append({"slides": copy.deepcopy(d["slides"][:6]), "caption": "fresh", "hashtags": ["ai"]})
    await env.tick(notify=notify)
    item, run = await env.last(i)
    assert run["fallback"] and run["deck"] == deck_fingerprint(item.slides) != deck_fingerprint(d["slides"])
    text = build_preview(item).text
    assert text.startswith("Fresh version (no note).") and "Every slide is new — compare before approving." in text
