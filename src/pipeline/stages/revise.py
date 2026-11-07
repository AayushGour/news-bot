"""The revise stage (T301, D248-D258): an operator's note becomes targeted edits.

Runs on ``Status.REVISION_REQUESTED``. Reads ``items.revision_request`` (written by
``revision.request.request_revision``), clears it atomically with the next status.

    flag off ──────────────────────────────► SYNTHESIZED + regen_note (legacy, D254)
    /skip, no note, no slides, overflow ───► compose with the previous deck (T292)
    plan (rules first, <=2 model calls)
      PlanFailed ──── rules actions kept, else ─► compose fallback
      nothing grounded ─────────────────────────► AWAITING_APPROVAL + "what do you mean?"
      remove_slide / full_recompose in plan ────► REVISION_CONFIRM  (Yes/No, U6/D269)
    round 1..MAX_ROUNDS:  execute -> check (code + one judge call)
      replan ONCE with feedback, only when a model-planned ask is retryable AND the
      slide numbers are still the operator's (D253: never an automatic full recompose)
    persist ──► COMPOSED (slides/theme changed) | RENDERED (caption-only / no change)

Faults (rule 17): ``Retryforever`` / ``ConfigFault`` / ``EnginesBlocked`` defer the item
in REVISION_REQUESTED with its request intact. ``EnginesBlocked`` is capped:
CAPTCHA suspensions last days, so after ``revise_engines_blocked_max_s`` the
search-backed actions are reported as not done and the rest runs.

Slide ids: a ``Deck`` numbers its slides by position, and a second round only ever
runs while the numbering is still the operator's (nothing removed, added or reordered
in round 1), so an id means the same slide in both rounds and in ``check_revision``'s
``before``/``after``. When round 1 shifted the numbering there is no second round:
the note says "slide 5" and slide 5 is no longer that slide.

Rule 15: nothing in a run record, an event or a notice carries exception text.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from ..config import REVISE_ENGINES_BLOCKED_RETRY_S
from ..conversation import NeedsInput
from ..db import Database, now_iso
from ..errors import EnginesBlocked, Retryforever
from ..models import Item, Status
from ..revision import request as rq
from ..revision.check import CheckReport, changed_ids, check_revision
from ..revision.check import Status as CheckStatus
from ..revision.execute import SEARCH_OPS, execute_round
from ..revision.model import (
    ACTION_SPECS,
    MAX_NOTE_CHARS,
    MAX_ROUNDS,
    Action,
    Op,
    Outcome,
    OutcomeCode,
    NoteRefs,
    Source,
    is_clipped,
    needs_confirmation,
    note_references,
    theme_request_drop,
)
from ..revision.plan import PlanFailed, RevisionPlan, plan_revision, rules_plan
from ..revision.record import append_run, closed_reason, deck_fingerprint, dropped_row, new_run
from ..revision.structural import Deck
from ..verify import run_advisory
from .compose import compose
from .render import available_themes

log = logging.getLogger(__name__)

#: Why a run fell back to ``compose`` (recorded as ``run.cause``; copy §5.6 picks its line from it).
CAUSE_SKIP = "skip"  # /skip: the operator asked for a fresh version
CAUSE_NO_SLIDES = "no_slides"  # nothing to edit
CAUSE_PLAN_FAILED = "plan_failed"  # legacy records only: since D289 a planner failure with no rules asks the operator
CAUSE_REBUILD = "rebuild"  # a confirmed whole-deck redo
CAUSE_OVERFLOW = "overflow"  # the renderer said a slide did not fit (until T304 plans it)

#: Outcomes after which asking the planner again cannot help (the executor said why).
_FINAL_CODES = frozenset({
    OutcomeCode.NOT_FOUND, OutcomeCode.REFUSED_INVARIANT, OutcomeCode.UNAVAILABLE,
    OutcomeCode.DROPPED_INVALID, OutcomeCode.DROPPED_UNGROUNDED, OutcomeCode.DROPPED_CONFLICT,
    OutcomeCode.DECLINED, OutcomeCode.DEFERRED,
})


class NoticeKind(StrEnum):
    CONFIRM = "confirm"  # the plan holds destructive actions: ask Yes/No
    ASK_OPERATOR = "ask_operator"  # nothing grounded: "which slide?" (item is back at the gate)
    DEFERRED = "deferred"  # infrastructure is down: one message per request


@dataclass(frozen=True)
class Notice:
    """What T302 needs to tell the operator. Built from code-owned data only.

    CONFIRM: ``token`` (callback data), ``held`` (what the Yes/No is about) and
    ``rest`` (what also runs). ASK_OPERATOR: the run is the last entry of
    ``item.revision.runs``. Delivered AFTER the DB transition (``_after``), so a
    button tapped at once finds the state it expects."""

    kind: NoticeKind
    token: str | None = None
    held: tuple[Action, ...] = ()
    rest: tuple[Action, ...] = ()


Notifier = Callable[[Item, Notice], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------


async def revise(
    item: Item, *, llm: Any, http: Any, settings: Any, db: Database, notify: Notifier | None = None,
) -> dict:
    """Stage entry point (see the module docstring). ``db`` is used ONLY to record
    the deferral bookkeeping (``notified``, ``blocked_since``) on a fault; every
    other write is the returned dict, committed by the worker."""
    request = dict(item.revision_request or {})
    try:
        return await _revise(item, request, llm, http, settings, notify)
    except Retryforever as exc:
        await _on_deferred(item, request, exc, db, notify)
        raise


def _fallback_note(request: Mapping[str, Any]) -> str | None:
    """The note a full compose is given: the operator's, or the overflow sentence the
    worker has always written (T304 replaces it with a planned edit)."""
    note = request.get(rq.K_NOTE)
    if isinstance(note, str) and note.strip():
        # A stored note may exceed the parse cap (dashboard); a prompt must not (budget).
        return note.strip()[:MAX_NOTE_CHARS]
    if request.get(rq.K_ORIGIN) == rq.Origin.OVERFLOW.value and request.get(rq.K_SLIDE):
        return f"Slide {request[rq.K_SLIDE]} did not fit: {request.get(rq.K_REASON) or 'too long'}. Shorten it."
    return None


async def _revise(
    item: Item, request: dict, llm: Any, http: Any, settings: Any, notify: Notifier | None,
) -> dict:
    origin = request.get(rq.K_ORIGIN)
    note = (request.get(rq.K_NOTE) or "").strip() if isinstance(request.get(rq.K_NOTE), str) else ""

    pending = rq.pending_confirm(request)
    if not settings.revise_enabled:  # the rollback switch: today's regen, exactly
        if pending is not None and pending[rq.P_ANSWER] is not None and pending[rq.P_ANSWER] is not True:
            # The operator said No to the destructive part: do not hand that note to a full compose.
            return {**rq.declined_fields(item, request, pending), "_next": Status.AWAITING_APPROVAL,
                    "_detail": "revision disabled; operator had declined; nothing changed"}
        return {
            "regen_note": _fallback_note(request), "revision_request": None,
            "_next": Status.SYNTHESIZED, "_detail": "revision disabled; regenerating",
        }

    if origin == rq.Origin.OVERFLOW.value:
        return await _fallback(item, request, CAUSE_OVERFLOW, llm, settings)
    if origin == rq.Origin.SKIP.value or not note:
        return await _fallback(item, request, CAUSE_SKIP, llm, settings)
    if not item.slides:
        return await _fallback(item, request, CAUSE_NO_SLIDES, llm, settings)

    themes = available_themes()
    plan_failed = False
    if pending is not None and pending[rq.P_ANSWER] is not None:
        plan, declined = _stored_plan(pending)
        plan_failed = bool(pending[rq.P_PLAN].get(rq.PL_PLAN_FAILED))
    elif pending is not None:
        # Re-run on a plan still waiting for its answer: park it again, unchanged.
        return {"_next": Status.REVISION_CONFIRM, "_detail": "revision awaiting confirmation"}
    else:
        declined = []
        try:
            plan = await plan_revision(
                llm, note, item.slides, item.caption or "", _claims(item.research), themes,
                settings=settings,
            )
        except PlanFailed as exc:
            if not exc.rules_actions:
                # D289: never a whole-deck rebuild the operator did not confirm. The old deck
                # and preview stay valid; the operator is asked to rephrase (or /skip).
                log.info("item %s: planner failed with no rules actions; asking the operator", item.id)
                # The note may still have asked for a theme by a name that does not exist: say so.
                theme = theme_request_drop(note, themes)
                failed = RevisionPlan(dropped=(theme,) if theme else (), ask_operator=True)
                return _ask_operator(item, request, failed, True, notify)
            plan, plan_failed = rules_plan(exc.rules_actions), True
        if not plan.actions:
            return _ask_operator(item, request, plan, plan_failed, notify)
        held = needs_confirmation(plan.actions)
        if held:
            return _park_for_confirm(item, request, plan, plan_failed, held, notify)

    if any(a.op == Op.FULL_RECOMPOSE.value for a in plan.actions):  # confirmed rebuild
        return await _fallback(item, request, CAUSE_REBUILD, llm, settings)
    return await _edit(item, request, note, plan, declined, plan_failed, themes, llm, http, settings)


# --------------------------------------------------------------------------
# the three exits that do not edit
# --------------------------------------------------------------------------


async def _fallback(
    item: Item, request: dict, cause: str, llm: Any, settings: Any,
) -> dict:
    """compose() with the previous deck in view (T292), as the legacy regen did. The
    note goes in memory (``regen_note``); compose clears the column itself."""
    try:
        result = await compose(replace(item, regen_note=_fallback_note(request)), llm, settings)
    except NeedsInput as exc:
        # Rule 14: the park is the only channel that survives; a request left on
        # the item would outlive the run it belongs to.
        exc.fields.setdefault("revision_request", None)
        raise
    run = new_run(request, at=now_iso(), fallback=True, cause=cause, plan_failed=cause == CAUSE_PLAN_FAILED,
                  clipped=is_clipped(request.get(rq.K_NOTE)),
                  deck=deck_fingerprint(result.get("slides", item.slides)))
    return {
        **result,
        "revision": append_run(item.revision, run),
        "revision_request": None,
        "_next": Status.COMPOSED,
        "_detail": f"revision fallback ({cause}): rebuilt with compose",
    }


def _ask_operator(
    item: Item, request: dict, plan: RevisionPlan, plan_failed: bool, notify: Notifier | None,
) -> dict:
    """Zero grounded actions: nothing changed, the old preview stays valid. The stage
    never raises NeedsInput (the operator is already in the loop), it answers."""
    run = new_run(
        request, at=now_iso(), ask_operator=True, plan_failed=plan_failed,
        clipped=is_clipped(request.get(rq.K_NOTE)), asks=plan.asks, dropped=plan.dropped,
        unaddressed=plan.unaddressed,
    )
    return {
        "revision": append_run(item.revision, run), "revision_request": None,
        "_next": Status.AWAITING_APPROVAL, "_detail": "revision: nothing grounded; asked the operator",
        **_after(item, notify, Notice(NoticeKind.ASK_OPERATOR)),
    }


def _plan_blob(plan: RevisionPlan, plan_failed: bool) -> dict[str, Any]:
    return {
        rq.PL_ASKS: list(plan.asks),
        rq.PL_ACTIONS: [rq.action_to_dict(a) for a in plan.actions],
        rq.PL_DROPPED: [dropped_row(d) for d in plan.dropped],
        rq.PL_UNADDRESSED: list(plan.unaddressed),
        rq.PL_CALLS: plan.calls,
        rq.PL_PLAN_FAILED: plan_failed,
    }


def _park_for_confirm(
    item: Item, request: dict, plan: RevisionPlan, plan_failed: bool, held: list[Action],
    notify: Notifier | None,
) -> dict:
    """The plan holds destructive actions. Persist the WHOLE plan (so the answer runs
    exactly what was shown, with no second planner call) and wait for the operator."""
    pending = rq.new_pending(_plan_blob(plan, plan_failed))
    rest = tuple(a for a in plan.actions if not a.spec.needs_confirm)
    return {
        "revision_request": {**request, rq.K_PENDING: pending},
        "_next": Status.REVISION_CONFIRM,
        "_detail": f"revision awaiting confirmation ({len(held)} destructive action(s))",
        **_after(item, notify, Notice(NoticeKind.CONFIRM, pending[rq.P_TOKEN], tuple(held), rest)),
    }


def _stored_plan(pending: Mapping[str, Any]) -> tuple[RevisionPlan, list[Action]]:
    """The plan the operator answered, minus what they declined. ``declined`` is
    what Yes/No dropped (``No``), so the run can say so."""
    blob = pending[rq.P_PLAN]
    actions = rq.actions_from_plan(blob)
    declined = [a for a in actions if a.spec.needs_confirm] if pending[rq.P_ANSWER] is not True else []
    kept = [a for a in actions if a not in declined]
    rows = blob.get(rq.PL_DROPPED)
    plan = RevisionPlan(
        tuple(blob.get(rq.PL_ASKS) or ()), False, tuple(kept),
        # Persisted drops stay rows: ``record.dropped_row`` passes them through.
        tuple(r for r in rows if isinstance(r, Mapping)) if isinstance(rows, list) else (),
        calls=int(blob.get(rq.PL_CALLS) or 0), unaddressed=tuple(blob.get(rq.PL_UNADDRESSED) or ()),
    )
    return plan, declined


def _after(item: Item, notify: Notifier | None, notice: Notice) -> dict[str, Any]:
    if notify is None:
        return {}

    async def deliver() -> None:
        await notify(item, notice)

    return {"_after": deliver}


# --------------------------------------------------------------------------
# the edit loop
# --------------------------------------------------------------------------


def _claims(research: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """The planner sees research CLAIMS only, never page text (D260)."""
    return [c for n in research or [] if isinstance(n, Mapping) and not n.get("fail_open")
            and (c := str(n.get("claim") or "").strip())]


def _search_blocked_too_long(request: Mapping[str, Any], settings: Any) -> bool:
    since = request.get(rq.K_BLOCKED_SINCE)
    if not isinstance(since, str):
        return False
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        return False
    started = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    return (_now() - started).total_seconds() >= settings.revise_engines_blocked_max_s


def _retryable(report: CheckReport, outcomes: Sequence[Outcome]) -> list[int]:
    """Unsatisfied asks a second plan could still serve: they were planned by the MODEL
    (rules ops are deterministic, retrying them changes nothing) and no outcome behind
    them is final (not found, refused, ...). 'Unchecked' is not unsatisfied."""
    out: list[int] = []
    for ask in report.asks:
        if ask.status is not CheckStatus.UNSATISFIED:
            continue
        mine = [o for o in outcomes if o.ask == ask.index and ACTION_SPECS[o.op].source is Source.MODEL]
        if mine and not {o.code for o in mine} & _FINAL_CODES:
            out.append(ask.index)
    return out


def _feedback(asks: Sequence[str], indexes: Sequence[int], outcomes: Sequence[Outcome]) -> list[str]:
    """Round 2's FEEDBACK lines: the ask's text and, per action behind it, only
    closed codes (the planner's own words and slide numbers, no executor prose)."""
    lines = []
    for i in indexes:
        why = "; ".join(
            f"{o.op}{f' slide {o.slide}' if o.slide else ''}: {o.code.value}"
            + (f" ({r})" if (r := closed_reason(o.reason)) else "")
            for o in outcomes if o.ask == i
        )
        lines.append(f"{asks[i]} -- not achieved ({why})")
    return lines


def _map_asks(asks: list[str], retry: Sequence[int], replan_asks: Sequence[str]) -> dict[int, int]:
    """Round-2 ask index -> index in the cumulative ``asks``.

    The planner was told which asks failed (FEEDBACK) and plans them again, usually
    in other words. A replan ask that matches an existing one by text (case-insensitive)
    IS that ask; the rest are restatements of the retried asks, taken in order; any
    left over are genuinely new and are appended. Without this a reworded retry would
    leave the original ask unsatisfied forever and the run would read 'partial' after
    it had succeeded."""
    low = [a.strip().lower() for a in asks]
    mapping: dict[int, int] = {}
    free = list(retry)
    for k, text in enumerate(replan_asks):
        key = text.strip().lower()
        if key in low:
            mapping[k] = low.index(key)
            if mapping[k] in free:
                free.remove(mapping[k])
    for k, text in enumerate(replan_asks):
        if k in mapping:
            continue
        if free:
            mapping[k] = free.pop(0)
        else:
            asks.append(text)
            mapping[k] = len(asks) - 1
    return mapping


async def _edit(
    item: Item, request: dict, note: str, plan: RevisionPlan, declined: list[Action], plan_failed: bool,
    themes: list[str], llm: Any, http: Any, settings: Any,
) -> dict:
    original = copy.deepcopy(list(item.slides))
    orig_by_id = dict(enumerate(original))
    n_orig = len(original)
    slides, caption, pin = copy.deepcopy(original), item.caption or "", item.theme_pin
    old_caption = caption

    asks = list(plan.asks)
    # An ask shared with an action that still runs is not masked.
    declined_asks = {a.ask for a in declined} - {a.ask for a in plan.actions}
    outcomes: list[Outcome] = []
    targeted: set[int] = set()
    new_notes: list[dict] = []
    refs = note_references(note, themes)
    skip_search = _search_blocked_too_long(request, settings)

    # An operator's No is not a failure: the declined asks/slides are masked from the check.
    declined_slides = {a.slide for a in declined if a.slide}
    check_refs = NoteRefs(
        tuple(s for s in refs.slides if s not in declined_slides), refs.themes, refs.clipped
    )
    declined_rows = [Outcome(a.op, OutcomeCode.DECLINED, a.slide, a.ask) for a in declined]

    round_actions: Sequence[Action] = plan.actions
    report: CheckReport | None = None
    ids: list[int] = list(range(n_orig))
    rounds = 0
    for rounds in range(1, MAX_ROUNDS + 1):
        run_item = replace(item, research=[*(item.research or []), *new_notes])
        res = await execute_round(
            round_actions, item=run_item, deck=Deck.of(slides), caption=caption, theme_pin=pin,
            note=note, llm=llm, http=http, settings=settings, themes=themes, skip_search=skip_search,
        )
        if not skip_search and any(a.op in SEARCH_OPS for a in round_actions):
            request.pop(rq.K_BLOCKED_SINCE, None)  # got past search: the engines are back
        outcomes += res.outcomes
        targeted |= res.targeted
        new_notes += res.new_notes

        masked = [("" if i in declined_asks else a) for i, a in enumerate(asks)]
        report, fixed = await check_revision(
            llm, asks=masked, outcomes=outcomes, note_refs=check_refs, before=orig_by_id,
            after=res.deck.by_id(), targeted_ids=targeted, caption_before=old_caption,
            caption_after=res.caption, change_notes=_change_notes(outcomes, res.theme_pin, item.theme_pin),
        )
        slides, ids = list(fixed.values()), list(fixed.keys())
        caption, pin = res.caption, res.theme_pin

        retry = _retryable(report, outcomes)
        # Numbering still the operator's? Only then may round 2 trust "slide N" in the note.
        if rounds >= MAX_ROUNDS or not retry or ids != list(range(n_orig)):
            break
        feedback = _feedback(asks, retry, outcomes)
        try:
            replan = await plan_revision(
                llm, note, slides, caption, _claims([*(item.research or []), *new_notes]), themes,
                feedback, settings=settings,
            )
        except PlanFailed:
            break
        model_only = [a for a in replan.actions if a.spec.source is Source.MODEL]
        if len(model_only) != len(replan.actions):  # rules ops were handled in round 1
            log.info("item %s: replan dropped %d rules-only action(s)", item.id, len(replan.actions) - len(model_only))
        if not model_only:
            break
        mapping = _map_asks(asks, retry, replan.asks)
        round_actions = [replace(a, ask=mapping.get(a.ask, retry[0])) for a in model_only]

    assert report is not None
    return await _persist(
        item, request, plan, plan_failed, declined_rows, asks, outcomes, report, rounds,
        original, slides, ids, caption, pin, new_notes, llm, settings,
    )


def _change_notes(outcomes: Sequence[Outcome], pin: str | None, old_pin: str | None) -> list[str]:
    """One-line, code-written descriptions of changes the judge cannot read from slide text."""
    notes = []
    for o in outcomes:
        if o.code is not OutcomeCode.APPLIED:
            continue
        if o.op == Op.REPLACE_IMAGE.value:
            notes.append(f"the picture on slide {o.slide} was replaced with a different one")
        elif o.op == Op.REMOVE_IMAGE.value:
            notes.append(f"the picture on slide {o.slide} was removed")
    if pin != old_pin:
        notes.append(f"the visual theme is now {pin}")
    return notes


async def _persist(
    item: Item, request: dict, plan: RevisionPlan, plan_failed: bool, declined_rows: list[Outcome],
    asks: list[str], outcomes: list[Outcome], report: CheckReport, rounds: int,
    original: list[dict], slides: list[dict], ids: list[int], caption: str, pin: str | None,
    new_notes: list[dict], llm: Any, settings: Any,
) -> dict:
    n_orig = len(original)
    changed = set(changed_ids(dict(enumerate(original)), dict(zip(ids, slides, strict=True))))
    kept_order = [g for g in ids if g < n_orig]
    reordered = kept_order != sorted(kept_order)
    slides_changed = bool(changed) or reordered
    theme_changed = pin != item.theme_pin
    caption_changed = caption != (item.caption or "")

    run = new_run(
        request, at=now_iso(), rounds=rounds, plan_failed=plan_failed,
        clipped=is_clipped(request.get(rq.K_NOTE)), asks=asks,
        actions=[*outcomes, *declined_rows], dropped=plan.dropped, unaddressed=plan.unaddressed,
        check=report.status.value, check_reasons=[r.value for r in report.reasons],
        unsatisfied=[a.index for a in report.asks if a.status is not CheckStatus.SATISFIED],
        missed_slides=report.missed_slides, missed_themes=report.missed_themes,
        changed=[i for i, g in enumerate(ids, 1) if g in changed and g < n_orig],
        added=[i for i, g in enumerate(ids, 1) if g >= n_orig],
        removed=[g + 1 for g in range(n_orig) if g not in ids],
        reordered=reordered, caption_changed=caption_changed, theme=pin if theme_changed else None,
        deck=deck_fingerprint(slides),
    )
    applied = sum(o.code is OutcomeCode.APPLIED for o in outcomes)
    fields: dict[str, Any] = {
        "revision": append_run(item.revision, run),
        "revision_request": None,
        "_next": Status.COMPOSED if (slides_changed or theme_changed or not item.rendered_paths) else Status.RENDERED,
        "_detail": f"revision {report.status.value}: {applied}/{len(outcomes)} action(s) applied, {rounds} round(s)",
    }
    research = [*(item.research or []), *new_notes]
    if new_notes:
        fields["research"] = research
    if slides_changed:
        fields["slides"] = slides
    if caption_changed:
        fields["caption"] = caption
    if theme_changed:
        fields["theme_pin"] = pin
    if slides_changed or caption_changed:
        # D209: the verdict belongs to the deck it judged; always (over)write it.
        fields["verification"] = await run_advisory(
            replace(item, research=research, slides=slides, caption=caption), slides, caption, llm, settings,
        )
    return fields


# --------------------------------------------------------------------------
# deferral (rule 17)
# --------------------------------------------------------------------------


async def _on_deferred(
    item: Item, request: dict, exc: Retryforever, db: Database, notify: Notifier | None,
) -> None:
    """Bookkeeping when infrastructure is down: the request stays, with two one-shot
    markers - ``blocked_since`` (the EnginesBlocked cap's clock) and ``notified`` (one
    deferral message per request, not one per retry)."""
    updated = dict(request)
    if isinstance(exc, EnginesBlocked):
        updated.setdefault(rq.K_BLOCKED_SINCE, _now().isoformat())
        exc.retry_after_s = max(exc.retry_after_s or 0, REVISE_ENGINES_BLOCKED_RETRY_S)
    else:
        # A different outage: the engines were not the problem this time, so a later
        # blockage starts a fresh clock rather than inheriting an old one.
        updated.pop(rq.K_BLOCKED_SINCE, None)
    first = not request.get(rq.K_NOTIFIED)
    if first:
        updated[rq.K_NOTIFIED] = True
    if updated != (item.revision_request or {}):
        await db.update_fields(item.id, {"revision_request": updated})
    if first and notify is not None:
        try:
            await notify(item, Notice(NoticeKind.DEFERRED))
        except Exception as err:  # advisory: the deferral itself must still happen
            log.warning("item %s deferral notice failed (%s)", item.id, type(err).__name__)
