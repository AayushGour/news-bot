"""Widen a search before running it.

A planner writes one phrasing of a question. Sources use another. Searching
"burnout and depression in IT professionals" found nothing, not because the
material does not exist — it is abundant — but because that exact compound
phrase is how a request is written, not how an article is titled. "burnout
warning signs", "occupational burnout symptoms", "developer mental health"
all reach it.

Expansion widens the SEARCH, not the research. Each researcher still produces
one note from one information need; it just gets more documents to pick from.
Multiplying researchers instead would multiply cost and produce near-duplicate
notes that later stages have to reconcile.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Per original query. Enough to escape one vocabulary, few enough that the
#: merged result pool stays readable.
VARIANTS_PER_QUERY = 3

EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {
        "expansions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "variants": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query", "variants"],
            },
        },
    },
    "required": ["expansions"],
}

EXPANSION_SYSTEM = """You rewrite search queries so they find more of what
already exists.

For each query you are given, write alternative phrasings of the SAME
information need — the words a source that answers it would actually use.

What makes a good variant:
- Different vocabulary, not different word order. "burnout warning signs" and
  "signs of burnout" are the same query twice; "occupational burnout symptoms"
  and "employee exhaustion indicators" are not.
- Register matters. A clinical topic is written up clinically in one place and
  colloquially in another. Reach both.
- Each variant must stand alone as a search. Not a fragment, not a follow-up.

What breaks a search, all of it seen in production:
- Do NOT weld two topics together. A query about burnout AND about AI finds the
  intersection of two literatures, which is far smaller than either. Write a
  variant for one topic on its own.
- Do NOT expand an acronym you were not given. If the query says "OKF", it
  stays "OKF" — guessing what it stands for sends every variant after the wrong
  subject.
- Do NOT add facts, dates, versions, or names that are not already in the
  query.
- Do NOT narrow. A variant with more qualifiers finds less, which is the
  opposite of the point."""


async def expand_queries(
    llm, queries: list[str], subject: str = "", per_query: int = VARIANTS_PER_QUERY,
) -> dict[str, list[str]]:
    """Map each query to alternative phrasings of the same need.

    Returns ``{}`` on any failure and for empty input — the caller then
    searches exactly what it would have searched before. Expansion improves
    recall; it must never be able to prevent a search from happening.
    """
    wanted = [str(q).strip() for q in (queries or []) if str(q or "").strip()]
    if not wanted:
        return {}

    listing = "\n".join(f"- {q}" for q in wanted)
    user = (
        (f"SUBJECT: {subject}\n\n" if subject else "")
        + f"Write up to {per_query} variants for each query.\n\nQUERIES:\n{listing}"
    )
    try:
        result = await llm.cheap(EXPANSION_SYSTEM, user, schema=EXPANSION_SCHEMA)
    except Exception as exc:
        log.warning("query expansion unavailable (%s); searching as planned", exc)
        return {}

    known = {q.lower(): q for q in wanted}
    out: dict[str, list[str]] = {}
    for entry in result.get("expansions") or []:
        original = known.get(str(entry.get("query", "")).strip().lower())
        if original is None:
            # A variant group for a query nobody asked for would search off
            # topic under the banner of an original query.
            continue
        seen = {original.lower()}
        variants: list[str] = []
        for raw in entry.get("variants") or []:
            variant = str(raw).strip()
            if variant and variant.lower() not in seen:
                seen.add(variant.lower())
                variants.append(variant)
            if len(variants) >= per_query:
                break
        if variants:
            out[original] = variants
    return out


def widen(query: str, expansions: dict[str, list[str]]) -> list[str]:
    """The original query first, then its variants."""
    return [query, *expansions.get(query, [])]
