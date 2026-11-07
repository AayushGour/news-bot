"""Did the work actually answer what was asked?

A request can carry more than one ask. "Write a 10 pager highlighting the early
signs of burnout and depression in IT professionals, how ai is causing
more/less" is two: the clinical signs, and AI's effect on them. The planner
treated it as one subject and put "AI" into every query, so nothing ever
searched the clinical half; three researchers came back reporting they had
found nothing, and the deck that shipped answered only the second clause.

Nothing noticed, because nothing was checking. This is that check, and it runs
twice against the same clauses: once on the research notes, where a gap can
still be fixed by asking the operator, and again on the finished slides, where
a gap means recomposing.

The judge is deliberately conservative. A clause counts as covered when
anything genuinely speaks to it — the cost of a false alarm is a wasted
recompose or a needless question, and both are worse than they sound when the
operator is the one being interrupted.
"""

from __future__ import annotations

import logging

from .errors import Retryforever, Terminal

log = logging.getLogger(__name__)

COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "uncovered": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["uncovered"],
}

COVERAGE_SYSTEM = """You check whether work answers what was asked.

You get the ASKS — the distinct things a request wanted — and the MATERIAL that
was produced. For each ask, decide whether the material genuinely addresses it.

Return "uncovered": the asks nothing addresses. Copy each one back verbatim.
An ask that IS addressed must not appear. If everything is addressed, return an
empty list.

Judge generously. Material that speaks to an ask partially, or in different
words, or from a different angle, counts as addressing it. Only list an ask
when the material says essentially nothing about it.

Two traps:
- Adjacent is not the same. "How to tell burnout from depression" does not
  address "the early signs of burnout" — one distinguishes two conditions, the
  other lists warning signs. Different asks.
- A statement that no information was found does not address anything. Material
  reading "the excerpts do not contain information about X" is a report of
  failure, not coverage of X."""


def _material(entries: list[str]) -> str:
    """Everything, uncut. A judge deciding whether an ask was answered
    cannot do it from the first 220 characters of each item."""
    lines = [str(e).strip() for e in entries if str(e or "").strip()]
    return "\n".join(f"- {line}" for line in lines)


def _clean_clauses(clauses: list[str] | None) -> list[str]:
    return [str(c).strip() for c in (clauses or []) if str(c or "").strip()]


async def judge_uncovered(
    llm, clauses: list[str], material: str, *, system: str = COVERAGE_SYSTEM
) -> list[str] | None:
    """The strict core of the coverage check: which ``clauses`` does the
    already-rendered ``material`` fail to address?

    Returns ``None`` when the check itself could not run: a model fault that is
    the check's own, a reply that is not the expected shape. ``None`` is not
    ``[]``: [] says "checked, everything covered", None says "could not check",
    and a caller that gates on this (the revise step, D253) must be able to
    tell them apart. Infrastructure faults (``Retryforever``, ``ConfigFault``,
    ``Terminal``) propagate (rule 17).

    ``system`` lets a caller with a different question (did THIS EDIT answer the
    ask) reuse the one parse-and-filter path instead of copying it.
    """
    clauses = _clean_clauses(clauses)
    if not clauses:
        return []

    asks = "\n".join(f"- {c}" for c in clauses)
    user = f"ASKS:\n{asks}\n\nMATERIAL:\n{material}"
    try:
        verdict = await llm.cheap(system, user, schema=COVERAGE_SCHEMA)
    except (Retryforever, Terminal):
        # Not "covered": an outage says nothing about the material, and
        # waving the item through on one is a gate failing open (T260, D235).
        # Defer it; the check runs again when the model is back.
        raise
    except Exception as exc:
        log.warning("coverage check unavailable (%s)", exc)
        return None

    raw = verdict.get("uncovered") if isinstance(verdict, dict) else None
    if not isinstance(raw, list):
        log.warning("coverage check unavailable (reply has no 'uncovered' list)")
        return None

    # Only clauses we actually asked about count. A model that paraphrases, or
    # invents an ask, must not be able to halt an item over something nobody
    # requested.
    known = {c.lower(): c for c in clauses}
    out: list[str] = []
    for entry in raw:
        match = known.get(str(entry).strip().lower())
        if match and match not in out:
            out.append(match)
    return out


async def unaddressed(llm, clauses: list[str], material: list[str]) -> list[str]:
    """Which of ``clauses`` does ``material`` fail to address?

    Returns [] when there is nothing to judge, and on a model fault that is
    the check's own. Infrastructure faults (``Retryforever``, ``ConfigFault``,
    ``Terminal``) propagate so the worker defers the item: "could not check"
    must never read as "checked, covered" (T260, D235).

    Advisory callers (research, compose, enumerate) want fail-open; the revise
    check wants ``judge_uncovered``'s ``None`` instead.
    """
    if not material:
        return []
    out = await judge_uncovered(llm, clauses, _material(material))
    if out is None:
        log.warning("coverage check unavailable; treating as covered")
        return []
    return out
