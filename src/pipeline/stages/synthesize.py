"""Synthesize: collapse research notes into one sourced factual brief.

The brief is stored separately from the slides on purpose. Regenerating slide
copy then costs one model call instead of a full re-research — which is what
makes the operator's Regenerate button cheap enough to press freely.
"""

from __future__ import annotations

from ..errors import Retryable
from ..models import Item

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


async def synthesize(item: Item, llm) -> dict:
    notes = item.research or []
    if not notes:
        raise Retryable("cannot synthesize with no research notes")

    blob = "\n\n".join(
        f"QUESTION: {n.get('question', '')}\n"
        f"FINDING: {n.get('claim', '')}\n"
        f"DETAIL: {n.get('detail', '')}\n"
        f"CONFIDENCE: {n.get('confidence', 'low')}\n"
        f"SOURCES: {', '.join(n.get('sources', []))}"
        for n in notes
    )
    user = f"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\nRESEARCH NOTES:\n\n{blob}"

    brief = await llm.good(SYSTEM, user, temperature=0.3)
    brief = (brief or "").strip()
    if not brief:
        raise Retryable("synthesis returned an empty brief")
    return {"brief": brief}
