"""The run record kept in ``items.revision`` (T301, D255).

``revision = {"v": 1, "runs": [run, ...]}``, at most ``REVISION_RUNS_KEPT`` runs,
oldest dropped (and logged, rule 7). One run is one revise execution. The shape
is the contract with the preview (T302/T303): the preview is built from this,
never from the stage's in-memory state, so it survives a restart.

Rule 15: a record holds counts, slide numbers, closed codes and the operator's own
words (``note``) or model-written ask text (capped at ``MAX_ASK_CHARS`` by the
planner). It never holds exception text or an executor's free-text reason
(``closed_reason``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .check import fingerprint
from .model import MAX_NOTE_CHARS, REVISION_RUNS_KEPT, Dropped, Outcome

log = logging.getLogger(__name__)

RECORD_VERSION = 1
#: ``Outcome.reason`` codes are snake_case words (``no_results``, ``min_slides``).
#: Anything else is a human-readable sentence an executor wrote for the log
#: (text_edit's integrity problems embed the model's own figures), and must not be
#: persisted for the preview.
_CLOSED_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def closed_reason(reason: str | None) -> str:
    """``reason`` if it is a closed code, else "" (the full text is for the log)."""
    return reason if isinstance(reason, str) and _CLOSED_CODE.fullmatch(reason) else ""


def deck_fingerprint(slides: Sequence[Any] | None) -> str:
    """Identity of a deck (sha256 of its canonical JSON). A run records the deck it
    produced, so a LATER preview of a different deck (a legacy regen, a requeue, an
    overflow recompose) can tell the run is stale and not show it as its changes."""
    return fingerprint(list(slides or []))


def latest_run(
    revision: Mapping[str, Any] | None, slides: Sequence[Any] | None = None, *, current_deck: bool = True
) -> dict[str, Any] | None:
    """The last run in ``revision``, or None. With ``current_deck`` (a PREVIEW of
    ``slides``) a run recorded against a different deck is stale and is not returned:
    a legacy regen, a requeue or an overflow recompose after it rebuilt the deck, and
    "Revised per your note" over a different deck would be a lie. A run that recorded
    no deck (older records) is trusted."""
    runs = revision.get("runs") if isinstance(revision, Mapping) else None
    run = runs[-1] if isinstance(runs, list) and runs and isinstance(runs[-1], dict) else None
    if run is None:
        return None
    if current_deck and run.get("deck") is not None and run["deck"] != deck_fingerprint(slides):
        return None
    return run


def outcome_row(o: Outcome) -> dict[str, Any]:
    return {
        "op": o.op, "slide": o.slide, "ask": o.ask,
        "outcome": o.code.value, "reason": closed_reason(o.reason),
    }


def dropped_row(d: Dropped | Mapping[str, Any]) -> dict[str, Any]:
    """A planner drop: code and reason only (``Dropped.detail`` is log text).
    An already-built row (read back from a persisted plan) passes through."""
    if isinstance(d, Mapping):
        return {**d, "reason": closed_reason(d.get("reason"))}
    return {"op": d.op, "slide": d.slide, "outcome": d.code.value, "reason": d.reason.value}


def clip_note(note: Any) -> str | None:
    """The operator's note as stored on a run: a string, cut at ``MAX_NOTE_CHARS``."""
    return note[:MAX_NOTE_CHARS] if isinstance(note, str) and note else None


def new_run(
    request: Mapping[str, Any], *, at: str, rounds: int = 0, fallback: bool = False,
    cause: str | None = None, plan_failed: bool = False, ask_operator: bool = False, clipped: bool = False,
    asks: Sequence[str] = (), actions: Iterable[Outcome] = (),
    dropped: Iterable[Dropped | Mapping[str, Any]] = (),
    unaddressed: Sequence[str] = (), check: str | None = None,
    check_reasons: Sequence[str] = (), unsatisfied: Sequence[int] = (),
    missed_slides: Sequence[int] = (), missed_themes: Sequence[str] = (),
    changed: Sequence[int] = (), added: Sequence[int] = (), removed: Sequence[int] = (),
    reordered: bool = False, caption_changed: bool = False, theme: str | None = None,
    deck: str | None = None,
) -> dict[str, Any]:
    """One run record. ``check`` is a ``check.Status`` value, or None when no check
    ran (fallback compose, ask-operator, operator declined everything)."""
    return {
        "origin": request.get("origin"),
        "surface": request.get("surface"),
        "note": clip_note(request.get("note")),
        "at": at,
        "rounds": rounds,
        "fallback": fallback,
        "cause": cause,
        "plan_failed": plan_failed,
        "ask_operator": ask_operator,
        "clipped": clipped,
        "asks": [str(a) for a in asks],
        "actions": [outcome_row(o) for o in actions],
        "dropped": [dropped_row(d) for d in dropped],
        "unaddressed": [str(a) for a in unaddressed],
        "check": check,
        "check_reasons": list(check_reasons),
        "unsatisfied": list(unsatisfied),
        "missed_slides": list(missed_slides),
        "missed_themes": list(missed_themes),
        "changed": sorted(changed),
        "added": sorted(added),
        "removed": sorted(removed),
        "reordered": reordered,
        "caption_changed": caption_changed,
        "theme": theme,
        "deck": deck,  # deck_fingerprint of the deck this run left behind (None: unknown)
    }


def append_run(revision: Mapping[str, Any] | None, run: dict[str, Any]) -> dict[str, Any]:
    """``revision`` with ``run`` appended, oldest runs dropped beyond
    ``REVISION_RUNS_KEPT`` (logged, rule 7). Never mutates ``revision``. An
    unreadable existing value (wrong shape / version) starts a fresh history."""
    runs: list[Any] = []
    if isinstance(revision, Mapping) and revision.get("v") == RECORD_VERSION:
        existing = revision.get("runs")
        runs = list(existing) if isinstance(existing, list) else []
    runs.append(run)
    over = len(runs) - REVISION_RUNS_KEPT
    if over > 0:
        log.info("revision history: dropping %d oldest run(s) (kept %d)", over, REVISION_RUNS_KEPT)
        runs = runs[over:]
    return {"v": RECORD_VERSION, "runs": runs}
