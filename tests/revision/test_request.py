"""T301: revision/request.py - the writer API T302 calls, and the Yes/No answer."""

from __future__ import annotations

import asyncio

import pytest

from pipeline.models import Status
from pipeline.revision import request as rq
from pipeline.revision.model import Action
from pipeline.revision.request import ConfirmResult

REMOVE = Action(op="remove_slide", ask=0, slide=4)
EDIT = Action(op="edit_slide", ask=1, slide=2, instruction="tighten")
RECOMPOSE = Action(op="full_recompose", ask=0, instruction="start over")


def pending(*actions, answer=None, token="tok12345"):
    return {rq.P_TOKEN: token, rq.P_ANSWER: answer, rq.P_AT: "t", rq.P_PLAN: {
        rq.PL_ASKS: ["remove slide", "tighten slide 2"], rq.PL_ACTIONS: [rq.action_to_dict(a) for a in actions],
        rq.PL_DROPPED: [], rq.PL_UNADDRESSED: [], rq.PL_CALLS: 0, rq.PL_PLAN_FAILED: False}}


async def seed(db, status, request, **fields):
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=1, raw_text="x")
    await db.transition(i, status, {"revision_request": request, "slides": [{"type": "hook", "headline": "h"}],
                                    **fields})
    return i


def base(**kw):
    return {**rq.new_request(origin="operator", note="remove slide 4", surface="telegram"), **kw}


# ------------------------------------------------------------------- new_request


def test_blank_operator_note_is_a_skip_and_a_skip_carries_no_note():
    r = rq.new_request(origin="operator", note="   ", surface="telegram")
    assert r["origin"] == "skip" and r["note"] is None
    assert rq.new_request(origin="skip", note="ignored", surface="dashboard")["note"] is None
    assert rq.new_request(origin="operator", note=" hi ", surface="telegram")["note"] == "hi"


def test_payload_shape_is_the_documented_one():
    r = rq.new_request(origin="overflow", slide=3, reason="too long", surface="worker")
    assert set(r) == {"origin", "note", "slide", "reason", "surface", "at"}
    assert (r["origin"], r["slide"], r["reason"], r["surface"]) == ("overflow", 3, "too long", "worker")


def test_an_unknown_origin_or_surface_is_refused():
    with pytest.raises(ValueError):
        rq.new_request(origin="bogus", surface="telegram")
    with pytest.raises(ValueError):
        rq.new_request(origin="skip", surface="carrier-pigeon")


# --------------------------------------------------------------- request_revision


async def test_request_revision_moves_the_item_and_writes_the_payload(db):
    i = await seed(db, Status.AWAITING_APPROVAL, None)
    item = await db.get_item(i)
    assert await rq.request_revision(db, item, origin="operator", note="slide 2 shorter", surface="telegram")
    after = await db.get_item(i)
    assert after.status is Status.REVISION_REQUESTED
    assert after.revision_request["note"] == "slide 2 shorter" and after.revision_request["surface"] == "telegram"


async def test_request_revision_loses_a_race_and_writes_nothing(db):
    i = await seed(db, Status.AWAITING_APPROVAL, None)
    snapshot = await db.get_item(i)
    await db.transition(i, Status.APPROVED)  # the dashboard approved first
    assert not await rq.request_revision(db, snapshot, origin="skip", surface="telegram")
    after = await db.get_item(i)
    assert after.status is Status.APPROVED and after.revision_request is None


async def test_a_new_request_supersedes_a_pending_confirm(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE)}))
    snapshot = await db.get_item(i)
    assert await rq.request_revision(db, snapshot, origin="operator", note="actually, slide 2 shorter",
                                     surface="telegram")
    after = await db.get_item(i)
    assert after.status is Status.REVISION_REQUESTED and rq.pending_confirm(after.revision_request) is None
    # the old button now says so
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.STALE


# ------------------------------------------------------------------ the answer


async def test_yes_requeues_with_the_answer_recorded(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE, EDIT)}))
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.YES
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED
    assert rq.pending_confirm(item.revision_request)[rq.P_ANSWER] is True
    assert item.revision_request["note"] == "remove slide 4"  # the request is intact


async def test_no_with_other_actions_continues(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE, EDIT)}))
    assert await rq.confirm_revision(db, i, False, "tok12345") is ConfirmResult.NO_CONTINUE
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED
    assert rq.pending_confirm(item.revision_request)[rq.P_ANSWER] is False


@pytest.mark.parametrize("held", [(REMOVE,), (RECOMPOSE,), (REMOVE, REMOVE)])
async def test_no_with_nothing_else_returns_to_the_gate_with_a_declined_record(db, held):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(*held)}))
    assert await rq.confirm_revision(db, i, False, "tok12345") is ConfirmResult.NO_NOTHING_LEFT
    item = await db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL and item.revision_request is None
    run = item.revision["runs"][-1]
    assert [a["outcome"] for a in run["actions"]] == ["declined"] * len(held)
    assert run["note"] == "remove slide 4" and run["check"] is None


async def test_a_second_tap_is_already_answered_and_changes_nothing(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE, EDIT)}))
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.YES
    before = await db.get_item(i)
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.ALREADY_ANSWERED
    assert await rq.confirm_revision(db, i, False, "tok12345") is ConfirmResult.ALREADY_ANSWERED
    after = await db.get_item(i)
    assert after.revision_request == before.revision_request and after.status == before.status
    assert rq.pending_confirm(after.revision_request)[rq.P_ANSWER] is True  # the No did not flip it


async def test_two_simultaneous_taps_exactly_one_wins(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE, EDIT)}))
    results = await asyncio.gather(
        rq.confirm_revision(db, i, True, "tok12345"), rq.confirm_revision(db, i, False, "tok12345"))
    winners = [r for r in results if r in (ConfirmResult.YES, ConfirmResult.NO_CONTINUE)]
    losers = [r for r in results if r not in winners]
    assert len(winners) == 1 and losers == [ConfirmResult.ALREADY_ANSWERED]
    answer = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_ANSWER]
    assert answer is (winners[0] is ConfirmResult.YES)


async def test_a_stale_token_is_refused_and_nothing_changes(db):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE)}))
    assert await rq.confirm_revision(db, i, True, "other") is ConfirmResult.STALE
    item = await db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM and rq.pending_confirm(item.revision_request)[rq.P_ANSWER] is None


async def test_an_answered_request_with_a_stale_token_is_stale_not_answered(db):
    i = await seed(db, Status.REVISION_REQUESTED, base(**{rq.K_PENDING: pending(REMOVE, EDIT, answer=True)}))
    assert await rq.confirm_revision(db, i, True, "old") is ConfirmResult.STALE
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.ALREADY_ANSWERED


@pytest.mark.parametrize("status", [Status.AWAITING_APPROVAL, Status.APPROVED, Status.COMPOSED])
async def test_an_item_that_is_not_waiting_is_not_touched(db, status):
    i = await seed(db, status, None)
    assert await rq.confirm_revision(db, i, True, "tok12345") is ConfirmResult.NOT_WAITING
    assert (await db.get_item(i)).status is status


async def test_missing_item_and_corrupt_state(db):
    assert await rq.confirm_revision(db, 999, True, 'x') is ConfirmResult.NO_ITEM
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: {"token": 5}}))  # no plan
    assert await rq.confirm_revision(db, i, True, None) is ConfirmResult.NOT_WAITING
    assert (await db.get_item(i)).status is Status.REVISION_CONFIRM


@pytest.mark.parametrize("token", [None, "", 5])
async def test_a_missing_token_is_stale_never_a_yes(db, token):
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(REMOVE, EDIT)}))
    assert await rq.confirm_revision(db, i, True, token) is ConfirmResult.STALE
    assert (await db.get_item(i)).status is Status.REVISION_CONFIRM


async def test_a_yes_for_the_old_plan_cannot_approve_the_superseding_one(db):
    """"remove slide 3" asked, then "remove slide 5" replaced it: the first button's token is dead."""
    old = Action(op="remove_slide", ask=0, slide=3)
    new = Action(op="remove_slide", ask=0, slide=5)
    i = await seed(db, Status.REVISION_CONFIRM, base(**{rq.K_PENDING: pending(old, token="tokOLD")}))
    snapshot = await db.get_item(i)
    assert await rq.request_revision(db, snapshot, origin="operator", note="remove slide 5", surface="telegram")
    await db.transition(i, Status.REVISION_CONFIRM, {"revision_request": base(
        note="remove slide 5", **{rq.K_PENDING: pending(new, token="tokNEW")})})
    assert await rq.confirm_revision(db, i, True, "tokOLD") is ConfirmResult.STALE
    item = await db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM and rq.pending_confirm(item.revision_request)[rq.P_ANSWER] is None
    assert await rq.confirm_revision(db, i, True, "tokNEW") is ConfirmResult.YES


def test_a_malformed_stored_action_is_dropped_not_fatal(caplog):
    plan = {rq.PL_ACTIONS: [rq.action_to_dict(EDIT), {"op": "rm -rf"}, "x", {"op": "edit_slide", "slide": "NaN"}]}
    assert rq.actions_from_plan(plan) == [EDIT]
    assert caplog.text.count("malformed stored action") == 3


def test_action_round_trip_is_exact():
    a = Action(op="reorder", ask=2, order=(1, 3, 2))
    assert rq.action_from_dict(rq.action_to_dict(a)) == a


async def test_request_revision_refuses_a_snapshot_already_running(db):
    i = await seed(db, Status.REVISION_REQUESTED, base())
    snap = await db.get_item(i)
    assert not await rq.request_revision(db, snap, origin="operator", note="other", surface="telegram")
    assert (await db.get_item(i)).revision_request["note"] == "remove slide 4"


def test_dropped_rows_are_reclosed_when_read_back():
    from pipeline.revision.record import dropped_row

    row = dropped_row({"op": "edit_slide", "slide": 2, "outcome": "dropped_invalid", "reason": "free text here"})
    assert row["reason"] == ""
    assert dropped_row({"op": "x", "slide": 1, "outcome": "o", "reason": "out_of_range"})["reason"] == "out_of_range"
