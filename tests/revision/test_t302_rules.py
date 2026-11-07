"""T302 bounce 1: the drop reasons the ask-the-operator message depends on (D2, D3) and
the superseded-token memory behind the 'out of date' toast (D1)."""

from __future__ import annotations

import pytest

from pipeline.models import Status
from pipeline.revision import request as rq
from pipeline.revision.copy import AskCause, ask_cause
from pipeline.revision.model import Action, Op, Reason, ground, theme_request_drop

DECK = [{"type": "hook", "headline": "Intro"}, {"type": "point", "headline": "Memory"},
        {"type": "point", "headline": "Pricing"}, {"type": "follow"}]
THEMES = ["aurora", "riso", "signal"]


# ----- D3
@pytest.mark.parametrize("note", ["don't remove slide 2", "do not delete slide 2 please", "never drop slide 3"])
def test_a_negated_model_removal_is_reported_as_negated(note):
    kept, dropped = ground([Action("remove_slide", slide=2)], note, DECK)
    assert not kept and [d.reason for d in dropped] == [Reason.NEGATED]
    assert ask_cause(dropped) is AskCause.NEGATED


@pytest.mark.parametrize("note", ["remove it somehow", "get slide 2 out of here", "tighten slide 2"])
def test_a_removal_the_note_never_asked_for_plainly_is_rules_only(note):
    kept, dropped = ground([Action("remove_slide", slide=2)], note, DECK)
    assert [d.reason for d in dropped] == [Reason.RULES_ONLY] and ask_cause(dropped) is AskCause.EXPLICIT_NEEDED


def test_a_partly_negated_note_is_not_called_negated():
    """'don't remove slide 2 but remove slide 3': a removal IS asked for, so not 'you said not to'."""
    _, dropped = ground([Action("remove_slide", slide=3)], "don't remove slide 2 but remove slide 3", DECK)
    assert [d.reason for d in dropped] == [Reason.RULES_ONLY]


def test_other_rules_only_ops_are_negation_aware_too():
    _, d = ground([Action("full_recompose", instruction="x")], "don't start over from scratch", DECK)
    assert d[0].reason is Reason.NEGATED
    _, d = ground([Action("reorder", order=(1, 3, 2, 4))], "never swap slides 2 and 3", DECK)
    assert d[0].reason is Reason.NEGATED
    _, d = ground([Action("set_theme", theme="riso")], "don't change the theme", DECK)
    assert d[0].reason is Reason.NEGATED


# ----- D2
def test_an_unknown_theme_name_is_a_bad_theme_and_asks_which():
    d = theme_request_drop("use the xyzzy theme", THEMES)
    assert d.op == Op.SET_THEME.value and d.reason is Reason.BAD_THEME and ask_cause([d]) is AskCause.WHICH_THEME
    assert theme_request_drop("switch to blorp style", THEMES).reason is Reason.BAD_THEME


@pytest.mark.parametrize("note", ["use a different theme", "change the theme", "I want a new theme", "theme please"])
def test_a_theme_asked_for_without_a_name_is_not_named(note):
    assert theme_request_drop(note, THEMES).reason is Reason.THEME_NOT_NAMED


@pytest.mark.parametrize("note", ["shorten slide 3", "don't change the theme", "make it punchier", "use the riso theme"])
def test_no_theme_drop_when_none_was_asked_negated_or_a_real_name(note):
    assert theme_request_drop(note, THEMES) is None


def test_no_theme_drop_when_the_rules_already_set_one():
    assert theme_request_drop("use the xyzzy theme", THEMES, [Action("set_theme", theme="riso")]) is None


async def test_the_planner_path_carries_the_theme_drop():
    from pipeline.revision.plan import plan_revision

    class LLM:
        async def good(self, *a, **k):
            return {"asks": [], "global": False, "actions": []}

    plan = await plan_revision(LLM(), "use the xyzzy theme", DECK, "cap", [], THEMES)
    assert plan.ask_operator and [d.reason for d in plan.dropped] == [Reason.BAD_THEME]
    assert ask_cause(plan.dropped) is AskCause.WHICH_THEME


# ----- D1
REMOVE = Action("remove_slide", ask=0, slide=2)


async def _confirm_item(db):
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=1, raw_text="n")
    pending = rq.new_pending({rq.PL_ASKS: ["a"], rq.PL_ACTIONS: [rq.action_to_dict(REMOVE)], rq.PL_DROPPED: [],
                              rq.PL_UNADDRESSED: [], rq.PL_CALLS: 0, rq.PL_PLAN_FAILED: False})
    req = rq.new_request(origin="operator", note="remove slide 2", surface="telegram")
    await db.transition(i, Status.REVISION_CONFIRM, {"slides": DECK, "caption": "c",
                                                      "revision_request": {**req, rq.K_PENDING: pending}})
    return i, pending[rq.P_TOKEN]


async def test_a_superseded_confirm_says_stale_while_the_newer_note_is_alive(db):
    i, token = await _confirm_item(db)
    assert await rq.request_revision(db, await db.get_item(i), origin="operator", note="slide 3 shorter",
                                     surface="telegram")
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED and item.revision_request[rq.K_SUPERSEDED] == [token]
    assert await rq.confirm_revision(db, i, True, token) is rq.ConfirmResult.STALE
    assert await rq.confirm_revision(db, i, True, "0badbeef") is rq.ConfirmResult.NOT_WAITING
    assert await rq.confirm_revision(db, i, True, "") is rq.ConfirmResult.NOT_WAITING
    assert (await db.get_item(i)).status is Status.REVISION_REQUESTED


async def test_superseded_tokens_accumulate_but_stay_bounded_and_are_cleared_with_the_request(db):
    i, token = await _confirm_item(db)
    await db.update_fields(i, {"revision_request": {
        **(await db.get_item(i)).revision_request, rq.K_SUPERSEDED: [f"{n:08x}" for n in range(9)]}})
    await rq.request_revision(db, await db.get_item(i), origin="operator", note="x" * 20, surface="telegram")
    kept = (await db.get_item(i)).revision_request[rq.K_SUPERSEDED]
    assert len(kept) == rq.MAX_SUPERSEDED and kept[-1] == token
    # a request that replaced nothing carries no key
    j = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=2, raw_text="n")
    await db.transition(j, Status.AWAITING_APPROVAL, {"slides": DECK})
    await rq.request_revision(db, await db.get_item(j), origin="operator", note="x" * 20, surface="telegram")
    assert rq.K_SUPERSEDED not in (await db.get_item(j)).revision_request


def test_a_negated_unknown_theme_is_not_asked_about():
    assert theme_request_drop("don't use the xyzzy theme", THEMES) is None
    assert theme_request_drop("never switch to blorp style", THEMES) is None


async def test_losing_the_race_to_a_superseding_note_is_still_stale(db, monkeypatch):
    """The tap's own write loses because a newer note landed between its read and its write."""
    i, token = await _confirm_item(db)
    real = db.transition_if
    first = True

    async def supersede_then_lose(*a, **k):
        nonlocal first
        if first:
            first = False
            item = await db.get_item(i)
            assert await rq.request_revision(db, item, origin="operator", note="slide 3 shorter", surface="telegram")
            return False
        return await real(*a, **k)

    monkeypatch.setattr(db, "transition_if", supersede_then_lose)
    assert await rq.confirm_revision(db, i, True, token) is rq.ConfirmResult.STALE
