"""T302: the dashboard's side of agentic regeneration.

Regen/say go through ``request_revision`` when REVISE_ENABLED (and keep the legacy
SYNTHESIZED path when it is off); the Yes/No for a destructive plan; the run history on
the detail page (escaped, closed codes mapped to the Telegram wording); and
``items.last_error`` replaced by a closed sentence while a revision waits (rule 15).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from pipeline.models import Status
from pipeline.revision import copy as rc
from pipeline.revision import request as rq
from pipeline.revision.model import Action, Dropped, Outcome, OutcomeCode, Reason
from pipeline.revision.record import append_run, deck_fingerprint, new_run

ROOT = Path(__file__).resolve().parents[1]
C = OutcomeCode
DECK = [
    {"type": "hook", "headline": "Blackwell B300 is here"},
    {"type": "point", "headline": "Memory is the bottleneck"},
    {"type": "photo", "headline": "The die up close"},
    {"type": "point", "headline": "Pricing leaks"},
    {"type": "follow"},
]
XSS = "<script>alert('x')</script>"


@pytest.fixture
def dash(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dashboard_revision_under_test", ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "pipeline.log")  # never the real log
    return module


class FakeRequest:
    def __init__(self, db, item_id=0, action="", form=None, revise_enabled=True, query=None):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=revise_enabled)}
        self.match_info = {"id": str(item_id), "action": action}
        self.query = query or {}
        self._form = form or {}
        self.headers = {}

    async def post(self):
        return self._form


_MSG = iter(range(1, 10_000))


async def make(db, status=Status.AWAITING_APPROVAL, brief="BRIEF", **fields):
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=next(_MSG), raw_text="news")
    base = {"brief": brief, "caption": "Original caption.", "slides": DECK, "research": [{"claim": "c"}],
            "rendered_paths": ["/tmp/a.png"]}
    await db.transition(i, status, {**base, **fields})
    return i


async def regen(dash, db, i, note="", **kw):
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "regen", {"note": note}, **kw))


# ===================================================================== regen / say


async def test_regen_is_a_guarded_revision_request_when_enabled(dash, db):
    i = await make(db)
    await regen(dash, db, i, "make slide 3 shorter")
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED and item.regen_note is None
    assert (item.revision_request["origin"], item.revision_request["surface"], item.revision_request["note"]) == (
        "operator", "dashboard", "make slide 3 shorter")
    assert item.brief == "BRIEF" and item.research == [{"claim": "c"}]


async def test_regen_without_a_note_is_a_skip_request(dash, db):
    i = await make(db)
    await regen(dash, db, i, "   ")
    assert (await db.get_item(i)).revision_request["origin"] == "skip"


@pytest.mark.parametrize("status", [Status.FAILED, Status.REJECTED, Status.AWAITING_APPROVAL])
async def test_regen_is_offered_from_the_revisable_statuses(dash, db, status):
    i = await make(db, status=status)
    await regen(dash, db, i, "x" * 20)
    assert (await db.get_item(i)).status is Status.REVISION_REQUESTED


async def test_regen_keeps_its_old_guards(dash, db):
    no_brief = await make(db, brief=None)
    with pytest.raises(web.HTTPConflict) as e:
        await dash.act(FakeRequest(db, no_brief, "regen", {"note": "x"}))
    assert "no brief" in e.value.text
    published = await make(db, status=Status.PUBLISHED)
    with pytest.raises(web.HTTPConflict) as e:
        await dash.act(FakeRequest(db, published, "regen", {"note": "x"}))
    assert "already published" in e.value.text
    assert (await db.get_item(no_brief)).status is Status.AWAITING_APPROVAL


async def test_regen_on_an_item_being_revised_is_refused_not_forced(dash, db):
    i = await make(db, status=Status.REVISION_REQUESTED)
    with pytest.raises(web.HTTPConflict):
        await dash.act(FakeRequest(db, i, "regen", {"note": "x"}))
    assert (await db.get_item(i)).status is Status.REVISION_REQUESTED


async def test_regen_on_a_mid_pipeline_item_keeps_the_legacy_recompose(dash, db):
    i = await make(db, status=Status.RENDERED)
    await regen(dash, db, i, "shorter")
    item = await db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note == "shorter" and item.revision_request is None


async def test_regen_flag_off_is_exactly_the_legacy_path(dash, db):
    i = await make(db)
    await regen(dash, db, i, "lead with the number", revise_enabled=False)
    item = await db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note == "lead with the number"
    assert item.revision_request is None


async def test_a_lost_race_is_a_conflict_and_changes_nothing(dash, db, monkeypatch):
    i = await make(db)

    async def lose(*a, **k):
        return False

    monkeypatch.setattr(dash, "request_revision", lose)
    with pytest.raises(web.HTTPConflict) as e:
        await dash.act(FakeRequest(db, i, "regen", {"note": "x"}))
    assert f"Item {i} has moved on" in e.value.text
    assert (await db.get_item(i)).status is Status.AWAITING_APPROVAL


async def test_say_requests_a_revision_when_enabled_and_recomposes_when_not(dash, db):
    on, off = await make(db), await make(db)
    with pytest.raises(web.HTTPFound):
        await dash.say(FakeRequest(db, on, form={"text": "tighten slide 2"}))
    with pytest.raises(web.HTTPFound):
        await dash.say(FakeRequest(db, off, form={"text": "tighten slide 2"}, revise_enabled=False))
    a, b = await db.get_item(on), await db.get_item(off)
    assert a.status is Status.REVISION_REQUESTED and a.revision_request["surface"] == "dashboard"
    assert b.status is Status.SYNTHESIZED and b.regen_note == "tighten slide 2"


async def test_say_on_an_open_confirm_replaces_it(dash, db):
    i = await make(db, status=Status.REVISION_CONFIRM)
    with pytest.raises(web.HTTPFound):
        await dash.say(FakeRequest(db, i, form={"text": "actually just tighten slide 2"}))
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED and rq.K_PENDING not in item.revision_request


async def test_say_elsewhere_still_only_records_the_message(dash, db):
    i = await make(db, status=Status.COMPOSED)
    with pytest.raises(web.HTTPFound):
        await dash.say(FakeRequest(db, i, form={"text": "hi"}))
    assert (await db.get_item(i)).status is Status.COMPOSED


# ===================================================================== the Yes/No


def plan_blob(*actions):
    return {rq.PL_ASKS: ["a", "b"], rq.PL_ACTIONS: [rq.action_to_dict(a) for a in actions], rq.PL_DROPPED: [],
            rq.PL_UNADDRESSED: [], rq.PL_CALLS: 0, rq.PL_PLAN_FAILED: False}


async def parked(db, *actions, note="remove slide 4 and tighten slide 2", slides=DECK):
    i = await make(db, slides=slides)
    request = rq.new_request(origin="operator", note=note, surface="dashboard")
    pending = rq.new_pending(plan_blob(*actions))
    await db.transition(i, Status.REVISION_CONFIRM, {"revision_request": {**request, rq.K_PENDING: pending}})
    return i, pending[rq.P_TOKEN]


REMOVE4 = Action("remove_slide", ask=0, slide=4)
EDIT2 = Action("edit_slide", ask=1, slide=2, instruction="tighter")


async def test_yes_and_no_use_confirm_revision_with_the_token(dash, db):
    i, token = await parked(db, REMOVE4, EDIT2)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "revise_yes", {"token": token}))
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED and rq.pending_confirm(item.revision_request)["answer"] is True
    j, token = await parked(db, REMOVE4)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, j, "revise_no", {"token": token}))
    assert (await db.get_item(j)).status is Status.AWAITING_APPROVAL


@pytest.mark.parametrize("token", ["", "deadbeef", None])
async def test_a_missing_or_stale_token_is_refused_and_changes_nothing(dash, db, token):
    i, real = await parked(db, REMOVE4)
    form = {} if token is None else {"token": token}
    with pytest.raises(web.HTTPConflict) as e:
        await dash.act(FakeRequest(db, i, "revise_yes", form))
    assert e.value.text == rc.CONFIRM_TOASTS[rq.ConfirmResult.STALE]
    assert (await db.get_item(i)).status is Status.REVISION_CONFIRM


async def test_answering_twice_is_a_conflict_with_the_plain_sentence(dash, db):
    i, token = await parked(db, REMOVE4, EDIT2)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "revise_yes", {"token": token}))
    with pytest.raises(web.HTTPConflict) as e:
        await dash.act(FakeRequest(db, i, "revise_yes", {"token": token}))
    assert e.value.text == rc.CONFIRM_TOASTS[rq.ConfirmResult.ALREADY_ANSWERED]


# ===================================================================== detail page


async def page(dash, db, i):
    res = await dash.detail(FakeRequest(db, i))
    return res.text


async def test_the_pending_plan_shows_with_yes_and_no_and_the_token(dash, db):
    i, token = await parked(db, REMOVE4, EDIT2)
    html = await page(dash, db, i)
    assert "waiting on your answer" in html and "Remove slide 4?" in html and "There is no undo" in html
    assert f"action='/item/{i}/revise_yes'" in html and f"action='/item/{i}/revise_no'" in html
    assert html.count(f"name=token value='{token}'") == 2
    assert "Yes, remove" in html and "No, keep it" in html and "Also doing: slide 2 reworded." in html


async def test_no_confirm_card_when_nothing_is_pending(dash, db):
    i = await make(db)
    assert "waiting on your answer" not in await page(dash, db, i)


async def test_headlines_and_notes_are_escaped_everywhere(dash, db):
    deck = [dict(s) for s in DECK]
    deck[3]["headline"] = XSS
    i, token = await parked(db, REMOVE4, slides=deck, note=XSS)
    run = new_run(rq.new_request(origin="operator", note=XSS, surface="dashboard"), at=XSS, asks=[XSS],
                  actions=[Outcome("edit_slide", C.NOT_FOUND, 2, 0)], check="unsatisfied", unsatisfied=[0],
                  unaddressed=[XSS], dropped=[Dropped("edit_slide", 9, C.DROPPED_INVALID, Reason.OUT_OF_RANGE)])
    run["surface"] = XSS
    run["origin"] = XSS
    await db.update_fields(i, {"revision": append_run(None, run)})
    html = await page(dash, db, i)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(" in html
    # the same through the confirm card (headline) and the run history (note, ask, unaddressed, surface)
    assert html.count("&lt;script&gt;") >= 4


async def test_the_run_history_uses_the_telegram_wording(dash, db):
    i = await make(db)
    run = new_run(rq.new_request(origin="operator", note="slide 3 shorter", surface="telegram"), at="2026-11-07T10:00:00",
                  asks=["shorten slide 3"], check="satisfied", changed=[3], deck=deck_fingerprint(DECK),
                  actions=[Outcome("edit_slide", C.APPLIED, 3, 0)])
    old = new_run(rq.new_request(origin="operator", note="x", surface="telegram"), at="2026-11-06T10:00:00",
                  ask_operator=True)
    await db.update_fields(i, {"revision": append_run(append_run(None, old), run)})
    html = await page(dash, db, i)
    assert "revisions · 2" in html
    assert "Revised per your note: &quot;slide 3 shorter&quot;" in html or 'Revised per your note: "slide 3 shorter"' in html
    assert "✓ Slide 3 — reworded" in html and rc.RUN_ASKED_SUMMARY in html
    assert html.index("✓ Slide 3") < html.index(rc.RUN_ASKED_SUMMARY), "newest first"
    for raw in ("edit_slide", "satisfied", "ask_operator"):
        assert raw not in html.split("revisions ·")[1].split("</details></div>")[0]


async def test_no_revisions_card_without_runs(dash, db):
    i = await make(db)
    assert "revisions ·" not in await page(dash, db, i)


async def test_a_garbled_revision_value_does_not_break_the_page(dash, db):
    i = await make(db)
    await db.update_fields(i, {"revision": {"v": 1, "runs": ["x", 3, None, {"actions": "no"}]}})
    assert "Item" in await page(dash, db, i)


# ===================================================================== last_error (rule 15)

PROVIDER_BODY = "ReadTimeout: ollama at http://127.0.0.1:11434 echoed 'CANARY-user-text'"


async def test_a_waiting_revision_never_shows_the_deferral_exception_text(dash, db):
    i = await make(db, status=Status.REVISION_REQUESTED)
    await db.update_fields(i, {"last_error": PROVIDER_BODY})
    html = await page(dash, db, i)
    assert "CANARY" not in html and "ollama" not in html
    assert rc.REVISION_WAITING_SUMMARY in html
    board = (await dash.index(FakeRequest(db))).text
    assert "CANARY" not in board and rc.REVISION_WAITING_SUMMARY in board


async def test_other_statuses_keep_showing_last_error(dash, db):
    i = await make(db, status=Status.FAILED)
    await db.update_fields(i, {"last_error": "Terminal: slide 3 did not fit"})
    assert "Terminal: slide 3 did not fit" in await page(dash, db, i)


def test_error_text_unit(dash):
    assert dash.error_text(Status.REVISION_REQUESTED.value, None) == ""
    assert dash.error_text(Status.REVISION_REQUESTED.value, "boom") == rc.REVISION_WAITING_SUMMARY
    assert dash.error_text(Status.FAILED.value, "boom") == "boom"


async def test_the_waiting_on_you_lane_links_to_the_pending_question(dash, db):
    i, _ = await parked(db, REMOVE4)
    board = (await dash.index(FakeRequest(db, query={"lane": "attn"}))).text
    assert f"href='/item/{i}'>answer</a>" in board


def test_chat_hint_is_flag_aware(dash):
    """T305: with the flag on the deck is edited in place, not recomposed."""
    item = SimpleNamespace(id=1, status=Status.AWAITING_APPROVAL.value)
    on = dash.chat_html([], item, targeted=True)
    off = dash.chat_html([], item)
    assert "targeted edit" in on and "recomposed" not in on
    assert "recomposed" in off and "targeted edit" not in off
