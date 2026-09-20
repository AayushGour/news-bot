"""The dashboard must offer the same actions as the bot.

Telegram has offered regenerate and caption-rewrite since the beginning; the
dashboard never did. An operator working from the browser could only requeue,
which throws the deck away and re-runs the search — a different, far more
expensive thing than "same research, compose it again with this note".
"""

import importlib.util
from pathlib import Path

import pytest
from aiohttp import web

from pipeline.models import Status

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dash():
    spec = importlib.util.spec_from_file_location(
        "dashboard_actions_under_test", ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRequest:
    def __init__(self, db, item_id, action, form=None):
        self.app = {"db": db}
        self.match_info = {"id": str(item_id), "action": action}
        self._form = form or {}
        self.headers = {}

    async def post(self):
        return self._form


async def _deck(db, status=Status.AWAITING_APPROVAL, brief="THE BRIEF", msg_id=1):
    i = await db.insert_item(source="channel", source_chat_id=-1,
                             source_msg_id=msg_id, raw_text="news")
    await db.transition(i, status, {
        "brief": brief, "caption": "Original caption.",
        "slides": [{"type": "hook", "headline": "H"}],
        "research": [{"claim": "c"}],
    })
    return i


# ------------------------------------------------------------- regenerate


async def test_regen_goes_to_synthesized_not_composed(dash, db):
    """A status names the stage that finished and the registry maps it to the
    NEXT one — COMPOSED would re-render the same slides instead of rebuilding
    them. Same choice the bot makes."""
    i = await _deck(db)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "regen", {"note": "lead with the number"}))

    item = await db.get_item(i)
    assert item.status == Status.SYNTHESIZED.value
    assert item.regen_note == "lead with the number"


async def test_regen_keeps_the_research_and_the_brief(dash, db):
    """This is what makes it cheaper than a requeue: one compose call."""
    i = await _deck(db)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "regen", {"note": "shorter"}))

    item = await db.get_item(i)
    assert item.brief == "THE BRIEF"
    assert item.research == [{"claim": "c"}]


async def test_regen_without_a_note_is_allowed(dash, db):
    """"Try again" is a legitimate instruction on its own."""
    i = await _deck(db)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "regen", {"note": "   "}))
    assert (await db.get_item(i)).regen_note is None


async def test_regen_is_refused_when_there_is_no_brief(dash, db):
    """Nothing to recompose from; the timeline is the right tool."""
    i = await _deck(db, status=Status.TRIAGED, brief=None)
    # aiohttp puts the explanation in .text; str(exc) is just "Conflict".
    with pytest.raises(web.HTTPConflict) as exc:
        await dash.act(FakeRequest(db, i, "regen", {"note": "x"}))
    assert "no brief" in exc.value.text


async def test_regen_is_refused_on_a_published_item(dash, db):
    i = await _deck(db, status=Status.PUBLISHED)
    with pytest.raises(web.HTTPConflict) as exc:
        await dash.act(FakeRequest(db, i, "regen", {"note": "x"}))
    assert "already published" in exc.value.text


# ---------------------------------------------------------------- caption


async def test_caption_replaces_the_text_without_moving_the_item(dash, db):
    """Rewriting words should not cost a re-render of nine slides."""
    i = await _deck(db)
    with pytest.raises(web.HTTPFound):
        await dash.act(FakeRequest(db, i, "caption", {"caption": "A better caption."}))

    item = await db.get_item(i)
    assert item.caption == "A better caption."
    assert item.status == Status.AWAITING_APPROVAL.value


async def test_an_empty_caption_is_refused(dash, db):
    """It would silently replace a real one with nothing."""
    i = await _deck(db)
    with pytest.raises(web.HTTPBadRequest):
        await dash.act(FakeRequest(db, i, "caption", {"caption": "  "}))
    assert (await db.get_item(i)).caption == "Original caption."


async def test_caption_is_refused_when_not_at_the_gate(dash, db):
    i = await _deck(db, status=Status.RENDERED)
    with pytest.raises(web.HTTPConflict):
        await dash.act(FakeRequest(db, i, "caption", {"caption": "x"}))


# ------------------------------------------------------------------- forms


async def test_the_regen_form_is_offered_wherever_regen_is_allowed(dash, db):
    """A form shown for an action the handler refuses is worse than none."""
    i = await _deck(db)
    item = await db.get_item(i)
    assert f"/item/{i}/regen" in dash.regen_form(item)


async def test_no_regen_form_without_a_brief(dash, db):
    i = await _deck(db, status=Status.TRIAGED, brief=None)
    assert dash.regen_form(await db.get_item(i)) == ""


async def test_no_caption_editor_away_from_the_gate(dash, db):
    i = await _deck(db, status=Status.RENDERED)
    assert dash.caption_form(await db.get_item(i)) == ""
