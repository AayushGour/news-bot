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
import re

from .budget import data_block
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


_LEAD_NOISE = re.compile(r"^(?:[\s\-*+\u2022\u2013\u2014>]+|\d+[.)]\s+)+")
_QUOTES = " \t\"'`\u201c\u201d\u2018\u2019"
_ASK_ID = re.compile(r"^(?:ask[-_ ]?)?(\d+)$")


def _fold(text: str) -> str:
    return " ".join(text.split()).lower()


def _resolve_echo(entry: object, clauses: list[str], nonce: str) -> str | None:
    """Map one entry the model returned back to the ask it names, or None.

    A model told to "copy each ask back verbatim" often echoes the prompt's
    framing instead: the ``[DATA-<nonce> id=ask-N]`` markers, an ``ask-N`` id,
    a bullet, quotes. Those are still a clear reference to one ask. Anything
    else (a paraphrase) names no ask we can be sure of. ``clauses`` is the
    cleaned list the prompt was numbered from, so ``ask-N`` is clauses[N-1].
    """
    raw = str(entry)
    by_text = {_fold(c): c for c in reversed(clauses)}  # first duplicate wins
    if _fold(raw) in by_text:  # verbatim, even if the ask itself looks like noise
        return by_text[_fold(raw)]
    marker = re.compile(rf"\[/?DATA-{re.escape(nonce)}(?:\s+id=([^\]\s]*))?\]", re.IGNORECASE)
    ids = [m for m in marker.findall(raw) if m]
    body = marker.sub("\n", raw)
    body = _LEAD_NOISE.sub("", body.strip()).strip(_QUOTES)
    body = _LEAD_NOISE.sub("", body).strip(_QUOTES)
    if _fold(body) in by_text:
        return by_text[_fold(body)]
    for candidate in ([body] if body else []) + ids:
        m = _ASK_ID.match(_fold(candidate))
        if m and 1 <= int(m.group(1)) <= len(clauses):
            return clauses[int(m.group(1)) - 1]
    return None


async def judge_uncovered(
    llm, clauses: list[str], material: str, *, system: str = COVERAGE_SYSTEM, nonce: str | None = None
) -> list[str] | None:
    """The strict core of the coverage check: which ``clauses`` does the
    already-rendered ``material`` fail to address?

    Returns ``None`` when the check itself could not run: a model fault that is
    the check's own, a reply that is not the expected shape. ``None`` is not
    ``[]``: [] says "checked, everything covered", None says "could not check",
    and a caller that gates on this (the revise step, D253) must be able to
    tell them apart. Infrastructure faults (``Retryforever``, ``ConfigFault``,
    ``Terminal``) propagate (rule 17).

    ``nonce`` set: each ask is wrapped in a per-run ``data_block`` (the revise
    judge's asks are planner-model text). Unset, the prompt is the bare list the
    live callers have always sent, byte for byte.

    ``system`` lets a caller with a different question (did THIS EDIT answer the
    ask) reuse the one parse-and-filter path instead of copying it.
    """
    clauses = _clean_clauses(clauses)
    if not clauses:
        return []

    if nonce:
        asks = "\n".join(data_block(nonce, f"ask-{i}", c) for i, c in enumerate(clauses, 1))
    else:
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

    if nonce:
        # Strict (revise) mode: an entry that names no ask is a reply we cannot
        # read, and a dropped entry would read as "covered" (a gate failing
        # open, rule 17). Unreadable means unchecked.
        strict: list[str] = []
        for entry in raw:
            match = _resolve_echo(entry, clauses, nonce)
            if match is None:
                log.warning("coverage check unavailable (reply names no known ask)")
                return None
            if match not in strict:
                strict.append(match)
        return strict

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
