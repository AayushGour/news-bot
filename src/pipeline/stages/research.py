"""Research: fan out across the web, one researcher per facet.

This stage carries the two mitigations for the failure that made the PoC unsafe.
Searching for "Cursor" returned mouse-cursor download sites, and the synthesis
then cited one of them as the source for a statement by Cursor's leadership.

Two independent defences, because either alone can fail:

1. **Query disambiguation** — the planner names the entity and the context that
   distinguishes it, and every query is guaranteed to carry that context.
2. **Relevance gate** — each fetched document is judged on whether it actually
   concerns that entity before it is allowed into the corpus.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from ..errors import Retryable
from ..models import Item
from ..search import dedupe_by_domain, fetch_text, searx

log = logging.getLogger(__name__)

MIN_NOTES = 2
RELEVANCE_EXCERPT_CHARS = 1200

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "entity": {"type": "string"},
        "entity_context": {"type": "string"},
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 6,
        },
    },
    "required": ["entity", "entity_context", "queries"],
}

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["relevant", "why"],
}

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "detail": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["claim", "detail", "confidence"],
}

PLAN_SYSTEM = """You plan web research for a tech-news publisher.

Given a news item, identify the main subject and produce search queries.

"entity": the main subject's name as people search for it.
"entity_context": 2-5 words that separate this subject from unrelated things
with similar names — but ONLY words justified by the news item itself.

This is the part that goes wrong. If the item names an unfamiliar abbreviation
and does not say what it stands for, you must NOT guess an expansion. Guessing
sends every search after the wrong subject, and a later filter then discards
the correct pages for not matching your guess. Two failures, one cause.

- The item explains what it is? Use its own words.
  "Cursor" described as an AI editor -> "Anysphere AI coding editor".
- The item does NOT explain it? Leave entity_context EMPTY and let the search
  find out. An empty context is always better than an invented one.
- Never expand an acronym from your own knowledge. "OKF" is not necessarily
  the Open Knowledge Foundation, or a Framework, or a research lab. If the
  item does not say, you do not know.

"queries": 4-5 plain search strings, each targeting a DIFFERENT facet:
  - what exactly happened
  - technical or business background
  - who the parties are and how they relate
  - prior comparable events
  - criticism, risks, or consequences

Every query must include the disambiguating context when you have one. When
entity_context is empty, search the bare term with words taken from the item —
that finds the real subject instead of a confident guess.

Write search strings, not questions to a chatbot."""

RELEVANCE_SYSTEM = """You are filtering search results for a research pipeline.

You will be given a subject and an excerpt from a web page. Decide whether the
page is genuinely about that subject.

The subject description may be imperfect — it can name an abbreviation whose
expansion was guessed. Judge against the SUBJECT NAME first. If the page is
plainly about a thing with that name, keep it, even when it contradicts the
parenthetical description. A page that corrects a wrong assumption about the
subject is exactly the page the research needs.

Reject the page if it merely shares a word with the subject. A page about mouse
cursors is not about the company Cursor. A page about SQL cursors is not either.
A generic homepage, login page, or product listing that never discusses the
subject is also not relevant.

Be strict. A page that only mentions the subject in passing, in a sidebar, or in
a list of links is not relevant. When uncertain, reject."""

RESEARCH_SYSTEM = """You are a research assistant for a tech-news publisher.

Read the web excerpts and answer the research question with a single factual
finding.

Rules:
- Use ONLY what the excerpts support.
- If the excerpts do not answer the question, say so and set confidence "low".
- Never invent numbers, dates, quotes, or names.
- "claim" is one sentence. "detail" adds the specifics that back it up.

If the excerpts contain a concrete technical artifact — a config or file
example, a request or response body, a file tree, a command, a schema — copy it
into "detail" VERBATIM inside a fenced block, exactly as written:

```yaml
type: concept
title: Onboarding
```

Do not paraphrase such material and do not tidy it up. Downstream stages can
only show a real example if you preserve one here; describing the shape of a
format in prose is not the same as showing it."""


def disambiguate(queries: list[str], entity: str, context: str) -> list[str]:
    """Guarantee every query carries the disambiguating context.

    The model is told to do this, but a prompt instruction is not a guarantee —
    and this is the exact failure that poisoned the PoC's source pool. So it is
    also enforced deterministically here.
    """
    entity = (entity or "").strip()
    context = (context or "").strip()
    if not context:
        return [q for q in queries if q.strip()]

    context_tokens = {t for t in context.lower().split() if len(t) > 2}
    out: list[str] = []
    for query in queries:
        query = query.strip()
        if not query:
            continue
        tokens = set(query.lower().split())
        if not context_tokens & tokens:
            query = f"{query} {context}"
        out.append(query)
    return out


async def plan_queries(item: Item, llm) -> tuple[list[str], str]:
    """Return disambiguated queries and the subject description for the gate."""
    context_blob = _item_context(item)
    plan = await llm.cheap(
        PLAN_SYSTEM, f"NEWS ITEM:\n{context_blob}", schema=PLAN_SCHEMA
    )
    entity = str(plan.get("entity", ""))
    entity_context = str(plan.get("entity_context", ""))
    queries = disambiguate(list(plan.get("queries", [])), entity, entity_context)
    subject = f"{entity} ({entity_context})" if entity_context else entity
    return queries, subject


async def research(item: Item, llm, http, settings) -> dict:
    """Fan out over queries and return the surviving research notes."""
    queries, subject = await plan_queries(item, llm)
    if not queries:
        raise Retryable("query planner produced no usable queries")

    semaphore = asyncio.Semaphore(max(1, settings.research_concurrency))

    async def guarded(index: int, query: str):
        async with semaphore:
            return await _research_one(index, query, subject, item, llm, http, settings)

    results = await asyncio.gather(
        *(guarded(i, q) for i, q in enumerate(queries)), return_exceptions=True
    )

    notes: list[dict] = []
    for query, result in zip(queries, results):
        if isinstance(result, BaseException):
            # One researcher dying is survivable; the others carry the item.
            log.warning("researcher failed for %r: %s", query[:60], result)
            continue
        if result:
            notes.append(result)

    if len(notes) < MIN_NOTES:
        raise Retryable(
            f"only {len(notes)} of {len(queries)} researchers produced notes; "
            f"need at least {MIN_NOTES}"
        )
    return {"research": notes}


# ----------------------------------------------------------------- internals


def _item_context(item: Item) -> str:
    parts = [item.raw_text or ""]
    extracted = item.extracted or {}
    for described in extracted.get("image_descriptions", []):
        if described.get("description"):
            parts.append(f"[image] {described['description']}")
    for page in extracted.get("url_texts", []):
        if page.get("text"):
            parts.append(f"[link {page['url']}]\n{page['text'][:2000]}")
    return "\n\n".join(p for p in parts if p.strip())


async def _research_one(
    index: int, query: str, subject: str, item: Item, llm, http, settings
) -> dict | None:
    results = await searx(http, settings.searxng_url, query, settings.results_per_query)
    picked = dedupe_by_domain(results, settings.docs_per_query)
    if not picked:
        return None

    corpus: list[str] = []
    sources: list[str] = []

    for result in picked:
        url = result["url"]
        text = await fetch_text(http, url)
        body = (text or result.get("content") or "").strip()
        if not body:
            continue

        if not await _is_relevant(llm, subject, url, body):
            continue

        corpus.append(f"[SOURCE: {url}]\n{body}")
        sources.append(url)

    if not corpus:
        log.info("researcher %s: every source rejected as irrelevant", index)
        return None

    user = (
        f"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\n"
        f"RESEARCH QUESTION: {query}\n\n"
        f"WEB EXCERPTS:\n\n" + "\n\n---\n\n".join(corpus)
    )
    note = await llm.cheap(RESEARCH_SYSTEM, user, schema=NOTE_SCHEMA)
    return {
        "question": query,
        "claim": str(note.get("claim", "")),
        "detail": str(note.get("detail", "")),
        "confidence": str(note.get("confidence", "low")),
        "sources": sources,
    }


async def _is_relevant(llm, subject: str, url: str, body: str) -> bool:
    """Gate a single document. Errors reject rather than admit."""
    try:
        verdict = await llm.cheap(
            RELEVANCE_SYSTEM,
            f"SUBJECT: {subject}\n\nPAGE URL: {url}\n\n"
            f"PAGE EXCERPT:\n{body[:RELEVANCE_EXCERPT_CHARS]}",
            schema=RELEVANCE_SCHEMA,
        )
    except Exception as exc:
        log.warning("relevance gate errored for %s: %s", url, exc)
        return False

    if not verdict.get("relevant"):
        log.info(
            "rejected %s as irrelevant: %s",
            urlparse(url).netloc, str(verdict.get("why", ""))[:120],
        )
        return False
    return True
