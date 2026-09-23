"""Synthesize: collapse research notes into one sourced factual brief.

The brief is stored separately from the slides on purpose. Regenerating slide
copy then costs one model call instead of a full re-research — which is what
makes the operator's Regenerate button cheap enough to press freely.

T108/D58: both prompt blobs below used to interpolate every note's full
``detail`` straight into an ``llm.good`` call with no cap and no trace — a
spam README measured at ~64,000 chars for a single note, one stage above
compose's own T105/T70 fix. Reuses ``budget.pack()`` (T70) rather than
re-implementing trimming: each note becomes one whole rendered record (D60 —
question/finding/detail/confidence/sources for the news blob, "claim:
detail" for the list blob), fair-shared across the budget so ``pack()``'s
sanitization (marker-forgery stripping) covers every attacker-reachable
field in one pass, not just ``detail`` alone. Nothing is dropped without a
trace (D5): the "[... N chars omitted ...]" marker survives into the prompt
and ``kept_chars + omitted_chars == original_chars`` holds per note.
"""

from __future__ import annotations

import logging
import sys

from ..budget import PackedSource, Source, budget_chars_for_role, pack
from ..errors import Retryable
from ..models import Item

log = logging.getLogger(__name__)

SYSTEM = """You are a fact-focused editor preparing an Instagram carousel about
a tech-news story.

Combine the research notes into one tight factual brief.

Rules:
- Use only claims the notes support. Drop anything unsupported.
- If two notes contradict each other, say so explicitly rather than choosing
  one. A flagged contradiction is useful; a silently resolved one is a lie.
- Attribute every fact to its source inline, like (source: https://...).
- Prefer specifics: numbers, dates, names, versions.
- 250 words of prose maximum, not counting any code blocks.
- No preamble, no headings, no bullet points.

If a research note contains a fenced block — a config sample, file tree,
request body, command, or schema — carry it into the brief VERBATIM, still
fenced, with its language tag. Never paraphrase it into prose and never invent
one that was not in the notes. A later stage can show a real example only if it
survives this step, and prose describing a format is not a substitute for the
format itself."""


LIST_SYSTEM = """You are introducing a collection for an Instagram carousel.

You are given several things that were found, already ranked. Write a short
framing paragraph: what this collection is, who it is for, and what the items
have in common. 80 words maximum.

Do not describe the items one by one — each gets its own slide. Do not invent
items or claim a count you were not given."""


#: D59/T109: ``budget.RESERVED_TOKENS`` (2048) was sized for research.py's
#: own scaffold, and this module's is a different size too — the wrong
#: quantity in the *other* direction from compose's (D59: compose's own
#: scaffold is 3.3x the default and under-reserved; this module's is
#: comfortably under it). "Comfortably under" is still not "correct" —
#: "close enough to the default" is exactly the kind of guess house rule 4
#: forbids, so it is measured here too, the same way.
#:
#: Measured 2026-09-23, ``python3 -c "import sys; sys.path.insert(0,'src'); \
#: from pipeline.stages.synthesize import SYSTEM, LIST_SYSTEM; \
#: print(len(SYSTEM), len(LIST_SYSTEM))"``: SYSTEM=1002 chars,
#: LIST_SYSTEM=370 chars. Only one is ever the system prompt for a given
#: call (news uses SYSTEM, list uses LIST_SYSTEM — never both in the same
#: call, unlike compose's SYSTEM+SLIDES_SCHEMA which both ride the same
#: one) so the reserve is sized off the LARGER of the two, not their sum:
#: scaffold = max(1002, 370) = 1002 chars.
#:
#: On top of the scaffold, the reserve must also cover what
#: ``budget.RESERVED_TOKENS``'s own comment names besides the system
#: prompt: the original item text, the small fixed prompt labels around
#: it, and the model's own completion.
#: - item.raw_text: bot_intake.py's TELEGRAM_TEXT_LIMIT=4096 chars is the
#:   platform ceiling for a single Telegram message/caption this field is
#:   built from; a same-window continuation (db.append_raw_text) can still
#:   exceed it, a known, separate, out-of-scope gap (named, not hidden) —
#:   this reserve is headroom for the common case, not a hard cap on
#:   raw_text, and the margin below absorbs a moderately longer one.
#: - fixed prompt labels ("REQUEST:\\n" / "FOUND (%d items):\\n" /
#:   "ORIGINAL NEWS ITEM:\\n" / "RESEARCH NOTES:\\n\\n"): under 40 chars,
#:   rounded up to 100 for margin.
#: - the completion: both SYSTEM and LIST_SYSTEM cap their own output in
#:   WORDS (250 / 80 — 250 is the worst case). Converted at 6 chars/word
#:   (average English word + a trailing space; a coarser, deliberately
#:   generous unit than D28's measured 6.34 chars/TOKEN prose figure,
#:   since this is chars-per-WORD) = 1500 chars worst case.
#:
#: Total: 1002 (scaffold) + 4096 (raw_text headroom) + 100 (labels) + 1500
#: (completion) = 6698 chars -> // CHARS_PER_TOKEN (2, the same conservative
#: worst-case chars/token this module's budget conversions already use) =
#: 3349 tokens. Rounded up to 3584 (a clean step above the computed figure,
#: same round-number style as RESERVED_TOKENS' own 2048) for margin.
SYNTHESIZE_RESERVED_TOKENS = 3584


def _log_if_trimmed(packed: list[PackedSource], budget_chars: int, kind: str) -> None:
    """Shared bookkeeping for both packing call sites below — the RENDERING
    differs materially between them (D61), the manifest logging does not."""
    if any(p.omitted_chars for p in packed):
        # %-style, not an eager f-string: note text is untrusted (research.py's
        # corpus, relayed through the note-writing model) and %-style logging
        # defers str()/repr() of the manifest until formatting, which escapes
        # embedded newlines rather than emitting them as real log lines — see
        # budget.py / research.py / compose.py for the full case.
        log.warning(
            "synthesize %s notes packed into %d/%d chars budget: %s",
            kind, sum(len(p.text) for p in packed), budget_chars,
            [{"note": p.id, "kept": p.kept_chars, "omitted": p.omitted_chars}
             for p in packed],
        )


def _pack_list_notes(notes: list[dict], budget_chars: int) -> str:
    """Render the list intent's framing listing, budgeted (T108/D58).

    Each note's whole rendered record — "claim: detail" — becomes one
    ``Source.text`` (D60's pattern), not ``detail`` alone: a forged marker
    sentinel could just as easily ride in ``claim``, and packing the whole
    record means ``pack()``'s sanitization covers it in one pass.
    """
    records = [f"{n.get('claim', '')}: {n.get('detail', '')}" for n in notes]
    packed = pack(
        [Source(id=str(i), text=r) for i, r in enumerate(records, start=1)],
        budget_chars,
    )
    _log_if_trimmed(packed, budget_chars, "list")
    return "\n".join(f"- {p.text}" for p in packed)


def _pack_news_notes(notes: list[dict], budget_chars: int) -> str:
    """Render the news intent's research-notes blob, budgeted (T108/D58).

    Each note's whole rendered record — question, finding, detail,
    confidence, sources — becomes one ``Source.text`` (D60's pattern), the
    same reason as ``_pack_list_notes``: the marker-forgery sanitization has
    to cover every attacker-reachable field, not just ``detail``.
    """
    records = [
        f"QUESTION: {n.get('question', '')}\n"
        f"FINDING: {n.get('claim', '')}\n"
        f"DETAIL: {n.get('detail', '')}\n"
        f"CONFIDENCE: {n.get('confidence', 'low')}\n"
        f"SOURCES: {', '.join(n.get('sources', []))}"
        for n in notes
    ]
    packed = pack(
        [Source(id=str(i), text=r) for i, r in enumerate(records, start=1)],
        budget_chars,
    )
    _log_if_trimmed(packed, budget_chars, "news")
    return "\n\n".join(p.text for p in packed)


async def synthesize(item: Item, llm, settings=None) -> dict:
    notes = item.research or []
    if not notes:
        raise Retryable("cannot synthesize with no research notes")

    # ``pack()`` always runs, even with ``budget_chars`` at ``sys.maxsize``
    # (no ``settings``, e.g. a direct unit test) — it is what applies
    # ``_sanitize`` (marker-forgery stripping), and that guarantee belongs to
    # this stage, not to whichever caller happened to pass real settings
    # (mirrors compose.py's ``_notes_block`` / D60).
    budget_chars = (
        budget_chars_for_role(
            settings, role="good", reserved_tokens=SYNTHESIZE_RESERVED_TOKENS,
        )
        if settings is not None
        else sys.maxsize
    )

    if item.intent == "list":
        # A list needs a frame, not an argument merged from sources.
        listing = _pack_list_notes(notes, budget_chars)
        brief = await llm.good(
            LIST_SYSTEM,
            f"REQUEST:\n{item.raw_text}\n\nFOUND ({len(notes)} items):\n{listing}",
            temperature=0.3,
        )
        brief = (brief or "").strip()
        if not brief:
            raise Retryable("list synthesis returned an empty brief")
        return {"brief": brief}

    blob = _pack_news_notes(notes, budget_chars)
    user = f"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\nRESEARCH NOTES:\n\n{blob}"

    brief = await llm.good(SYSTEM, user, temperature=0.3)
    brief = (brief or "").strip()
    if not brief:
        raise Retryable("synthesis returned an empty brief")
    return {"brief": brief}
