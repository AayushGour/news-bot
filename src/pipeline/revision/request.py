"""The operator-facing side of a revision (T301): how a request is written, and how
a pending Yes/No is answered. T302 (bot, dashboard) calls ONLY this module; the
stage in ``stages/revise.py`` reads what it writes.

``items.revision_request`` (nullable JSON, D255) has one WRITER per field:

    request_revision()   origin, note, slide, reason, surface, at     (bot / dashboard / worker)
    the revise stage     notified, blocked_since, pending_confirm      (while it runs)
    confirm_revision()   pending_confirm.answer                        (the Yes/No button)

and one clearer: the revise stage, atomically with the item's next status.

State machine (the confirm gate, U6/D269)::

    AWAITING_APPROVAL --request_revision--> REVISION_REQUESTED --revise--> COMPOSED | RENDERED
                                                  |  plan holds remove_slide / full_recompose
                                                  v
                                           REVISION_CONFIRM  (WORKER_HALTS: nobody advances it)
                     confirm_revision(yes) --> REVISION_REQUESTED  (plan executes as stored)
                     confirm_revision(no)  --> REVISION_REQUESTED  (the rest runs)
                                           or  AWAITING_APPROVAL   (nothing was left; old preview valid)

Why a status and not a flag: a flag on ``REVISION_REQUESTED`` has to be skipped by
every reader of that status (the worker claims by status); the human gate in
``WORKER_HALTS`` is enforced by the state machine itself, survives a restart as
plain DB state, and cannot be bypassed by a future caller forgetting a check.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from ..db import Database, now_iso
from ..models import Item, Status
from .model import (
    ACTION_SPECS,
    MAX_NOTE_CHARS,
    Action,
    Outcome,
    OutcomeCode,
    needs_confirmation,
)
from .record import append_run, new_run

log = logging.getLogger(__name__)


class Origin(StrEnum):
    OPERATOR = "operator"  # the operator typed a note
    SKIP = "skip"  # /skip: a fresh full version, no note (U5)
    OVERFLOW = "overflow"  # the renderer said slide N does not fit (T304)


class Surface(StrEnum):
    TELEGRAM = "telegram"
    DASHBOARD = "dashboard"
    WORKER = "worker"


# Keys of ``revision_request`` (the payload is one dict; these are its field names).
K_ORIGIN, K_NOTE, K_SLIDE, K_REASON, K_SURFACE, K_AT = (
    "origin", "note", "slide", "reason", "surface", "at"
)
K_NOTIFIED = "notified"  # the deferral message was sent (one per request)
K_BLOCKED_SINCE = "blocked_since"  # first EnginesBlocked deferral (the cap's clock)
K_PENDING = "pending_confirm"
#: Tokens of confirms this request replaced (a newer note while a Yes/No was open), so the
#: old button can be told "out of date" (STALE) instead of "no longer needed" (NOT_WAITING)
#: for as long as the newer request is alive. Cleared with the request.
K_SUPERSEDED = "superseded"
MAX_SUPERSEDED = 5
# Keys of ``pending_confirm``.
P_TOKEN, P_PLAN, P_ANSWER, P_AT = "token", "plan", "answer", "asked_at"
# Keys of ``pending_confirm.plan`` (everything the stage needs to run it as stored).
PL_ASKS, PL_ACTIONS, PL_DROPPED, PL_UNADDRESSED, PL_CALLS, PL_PLAN_FAILED = (
    "asks", "actions", "dropped", "unaddressed", "calls", "plan_failed"
)
#: Editorial: a stored note is cut here (a dashboard textarea is unbounded). It MUST
#: stay above ``MAX_NOTE_CHARS``: the parsers cut a note at that length and the check
#: refuses to call a cut note satisfied, which only works if the stored note is still
#: visibly longer than the parse cap.
MAX_STORED_NOTE_CHARS = 2 * MAX_NOTE_CHARS
TOKEN_BYTES = 4  # 8 hex chars: fits Telegram's 64-byte callback data beside a verb and an id


def new_request(
    *, origin: Origin | str, note: str | None = None, slide: int | None = None,
    reason: str | None = None, surface: Surface | str,
) -> dict[str, Any]:
    """The ``revision_request`` payload. An operator request with a blank note is a
    /skip (the legacy ``note or None``). A skip/overflow carries no operator note."""
    origin = Origin(origin)
    text = (note.strip()[:MAX_STORED_NOTE_CHARS] or None) if isinstance(note, str) else None
    if origin is Origin.OPERATOR and not text:
        origin = Origin.SKIP
    if origin is Origin.SKIP:
        text = None
    return {
        K_ORIGIN: origin.value, K_NOTE: text, K_SLIDE: slide, K_REASON: reason,
        K_SURFACE: Surface(surface).value, K_AT: now_iso(),
    }


async def request_revision(
    db: Database, item: Item, *, origin: Origin | str, note: str | None = None,
    slide: int | None = None, reason: str | None = None, surface: Surface | str,
    detail: str | None = None,
) -> bool:
    """Move ``item`` (the caller's snapshot) to ``REVISION_REQUESTED`` with a new
    request. Guarded on the snapshot's status AND ``status_updated_at``
    (``transition_if``), so a dashboard approve and a Telegram regen cannot both
    win: returns False when someone else moved the item first and NOTHING was
    written. Passing an item snapshot in ``REVISION_CONFIRM`` supersedes that
    pending confirm (the new request carries no ``pending_confirm``, so the old
    button's token no longer matches). Which statuses may request a revision is
    the caller's policy (bot: awaiting_approval; dashboard: also failed/rejected). A snapshot already in
    REVISION_REQUESTED is refused (False): the stage may be mid-run."""
    if item.status is Status.REVISION_REQUESTED:
        return False  # the worker may be running it right now; a second request would race the stage
    request = new_request(origin=origin, note=note, slide=slide, reason=reason, surface=surface)
    old = item.revision_request or {}
    replaced = [*(old.get(K_SUPERSEDED) or []), *([pc[P_TOKEN]] if (pc := pending_confirm(old)) else [])]
    if replaced:
        request[K_SUPERSEDED] = replaced[-MAX_SUPERSEDED:]
    return await db.transition_if(
        item.id, item.status, item.status_updated_at, Status.REVISION_REQUESTED,
        {"revision_request": request}, detail=detail or f"revision requested ({request[K_ORIGIN]})",
    )


# ------------------------------------------------------------------ plan <-> JSON


def action_to_dict(a: Action) -> dict[str, Any]:
    return {
        "op": a.op, "ask": a.ask, "slide": a.slide, "instruction": a.instruction,
        "query": a.query, "theme": a.theme, "order": list(a.order),
    }


def action_from_dict(d: Any) -> Action | None:
    """A stored action, or None if it is not a well-formed one (a hand-edited or
    downgraded row must not crash the worker; the caller drops it, logged)."""
    if not isinstance(d, Mapping) or d.get("op") not in ACTION_SPECS:
        return None
    try:
        order = tuple(int(x) for x in d.get("order") or ())
        return Action(
            op=d["op"], ask=int(d.get("ask", 0)), slide=int(d.get("slide", 0)),
            instruction=str(d.get("instruction", "")), query=str(d.get("query", "")),
            theme=str(d.get("theme", "")), order=order,
        )
    except (TypeError, ValueError):
        return None


def actions_from_plan(plan: Mapping[str, Any]) -> list[Action]:
    """The stored plan's actions in order; malformed entries are dropped and logged."""
    raw = plan.get(PL_ACTIONS)
    out: list[Action] = []
    for entry in raw if isinstance(raw, list) else []:
        a = action_from_dict(entry)
        if a is None:
            log.warning("revision: dropped a malformed stored action")
        else:
            out.append(a)
    return out


def pending_confirm(request: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``request.pending_confirm`` if it is well-formed (token + plan), else None."""
    pc = (request or {}).get(K_PENDING)
    if isinstance(pc, dict) and isinstance(pc.get(P_TOKEN), str) and isinstance(pc.get(P_PLAN), dict):
        return pc
    return None


def new_pending(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {P_TOKEN: secrets.token_hex(TOKEN_BYTES), P_PLAN: dict(plan), P_ANSWER: None, P_AT: now_iso()}


def confirm_items(actions: Sequence[Action]) -> list[Action]:
    """What the Yes/No is about (``needs_confirmation``); T302 builds the prompt from it."""
    return needs_confirmation(actions)


# ------------------------------------------------------------------ the answer


class ConfirmResult(StrEnum):
    """What ``confirm_revision`` did. T302 maps each to a toast / edit (copy §3.3-3.4)."""

    YES = "yes"  # the destructive actions will run (with the rest)
    NO_CONTINUE = "no_continue"  # those were dropped; the rest of the plan runs
    NO_NOTHING_LEFT = "no_nothing_left"  # nothing else was planned: back to awaiting approval
    ALREADY_ANSWERED = "already_answered"  # a second tap; the run is in progress
    STALE = "stale"  # the token belongs to a replaced request
    NOT_WAITING = "not_waiting"  # the item is no longer waiting on a confirm
    NO_ITEM = "no_item"


def declined_fields(item: Item, request: Mapping[str, Any], pc: Mapping[str, Any]) -> dict[str, Any]:
    """The item fields for 'the operator declined and nothing runs': a run record whose
    held actions are all ``declined``, and the request cleared. The caller picks the
    next status (always AWAITING_APPROVAL: the old preview is still the truth)."""
    plan = pc[P_PLAN]
    held = confirm_items(actions_from_plan(plan))
    run = new_run(
        request, at=now_iso(), asks=plan.get(PL_ASKS) or (),
        actions=[Outcome(a.op, OutcomeCode.DECLINED, a.slide, a.ask) for a in held],
        dropped=plan.get(PL_DROPPED) or (), unaddressed=plan.get(PL_UNADDRESSED) or (),
    )
    return {"revision": append_run(item.revision, run), "revision_request": None}


async def confirm_revision(
    db: Database, item_id: int, yes: bool, token: str
) -> ConfirmResult:
    """Record the operator's Yes/No on a pending destructive revision.

    Idempotent and stale-safe. The single write is one ``transition_if`` on
    (status, status_updated_at), so two taps - or a tap racing a superseding note
    or a dashboard action - cannot both win; the loser re-reads and is classified
    (``ALREADY_ANSWERED`` / ``STALE`` / ``NOT_WAITING``). Nothing destructive runs
    here: Yes only re-queues the item with ``answer=True``; the stage executes the
    plan it stored. ``token`` is REQUIRED: ``pending_confirm.token`` from the
    button's callback data. A missing or different token is ``STALE``, so a Yes
    meant for one plan can never approve the plan that superseded it.
    """
    item = await db.get_item(item_id)
    if item is None:
        return ConfirmResult.NO_ITEM
    request = item.revision_request or {}
    pc = pending_confirm(request)

    if item.status is not Status.REVISION_CONFIRM or pc is None:
        if (item.status is Status.REVISION_REQUESTED and pc is not None
                and pc.get(P_ANSWER) is not None):
            if token != pc[P_TOKEN]:
                return ConfirmResult.STALE
            return ConfirmResult.ALREADY_ANSWERED
        if token and token in (request.get(K_SUPERSEDED) or []):
            return ConfirmResult.STALE  # a newer note replaced this question and is still being handled
        return ConfirmResult.NOT_WAITING
    if token != pc[P_TOKEN]:
        return ConfirmResult.STALE

    plan = pc[P_PLAN]
    actions = actions_from_plan(plan)
    rest = [a for a in actions if not a.spec.needs_confirm]
    answered = {**request, K_PENDING: {**pc, P_ANSWER: bool(yes)}}

    if yes or rest:
        to, fields, result = Status.REVISION_REQUESTED, {"revision_request": answered}, (
            ConfirmResult.YES if yes else ConfirmResult.NO_CONTINUE)
        detail = "revision confirmed" if yes else "revision: destructive actions declined"
    else:
        # Declined and nothing else was planned: the old preview is still the
        # truth, so go straight back to the gate with the run on record.
        to, result = Status.AWAITING_APPROVAL, ConfirmResult.NO_NOTHING_LEFT
        fields = declined_fields(item, request, pc)
        detail = "revision declined; nothing changed"

    won = await db.transition_if(
        item_id, Status.REVISION_CONFIRM, item.status_updated_at, to, fields, detail=detail,
    )
    if won:
        return result
    # Lost the race. Classify against the item as it is now (one re-read, no loop).
    now = await db.get_item(item_id)
    if now is None:
        return ConfirmResult.NO_ITEM
    npc = pending_confirm(now.revision_request)
    if now.status is Status.REVISION_REQUESTED and npc is not None and npc.get(P_ANSWER) is not None:
        return ConfirmResult.ALREADY_ANSWERED if npc[P_TOKEN] == pc[P_TOKEN] else ConfirmResult.STALE
    if now.status is Status.REVISION_CONFIRM:
        return ConfirmResult.STALE  # a newer pending confirm replaced this one
    if token in ((now.revision_request or {}).get(K_SUPERSEDED) or []):
        return ConfirmResult.STALE
    return ConfirmResult.NOT_WAITING
