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
from enum import StrEnum
from urllib.parse import urlparse

from ..budget import Source, budget_chars_for_role, pack
from ..errors import Retryable, Retryforever
from ..conversation import NeedsInput
from ..coverage import unaddressed
from ..models import Item, Status
from pathlib import Path

from ..search import (
    DEFAULT_CATEGORIES,
    dedupe_by_domain,
    download_image,
    fetch_text,
    image_search,
    searx,
)

log = logging.getLogger(__name__)

MIN_NOTES = 2

#: Note-dict key marking a note built from _research_one's fail-open path
#: (B1/B2, bounce 1) -- one researcher's read of a SINGLE source the gate
#: never actually cleared, not two independently-verified findings. Set at
#: the only place a fail-open note is ever created; read once, in
#: research(), to exclude it from MIN_NOTES (D90) -- see FAIL_OPEN_KEY's
#: use sites for both.
FAIL_OPEN_KEY = "fail_open"

#: T72 replaced the original, uncited flat ``body[:1200]`` slice with the
#: WHOLE note-writing corpus budget (12,288 chars local / 131,072 hosted) --
#: the wrong quantity for a single binary relevance verdict. D91
#: (project-context.md, 2026-09-23) already measured that swap directly:
#: widening to the full budget bought ``gate_accept_rate`` +0.0232 -- one
#: extra accepted source in 43, INSIDE this codebase's own established
#: +/-0.023 noise floor (D83) -- while nearly tripling per-item wall-clock
#: on item 503 (229s -> 651s) at identical gate/note counts. That is
#: evidence the wider excerpt buys nothing measurable, not evidence 1200 is
#: sufficient on its own merits, so 1200 is kept here as the smallest
#: already-measured point rather than re-guessed. ``scripts/measure_
#: relevance_excerpt.py`` (new, this task) is the re-runnable, cited
#: confirmation house rule 4 asks for -- built to reuse eval_research.py's
#: own concurrent research() run rather than a slower sequential one; a live
#: local-model confirmation run was started but did not complete inside
#: this task's session (outdated/slow local inference, not a code problem)
#: and is reported as outstanding, not silently dropped.
RELEVANCE_EXCERPT_CHARS = 1200

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "entity": {"type": "string"},
        "entity_context": {"type": "string"},
        "image_query": {"type": "string"},
        "clauses": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 6,
        },
    },
    "required": ["entity", "entity_context", "queries", "image_query", "clauses"],
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

"clauses": the distinct things the request asks for, in the requester's own
words. Most news items ask one thing and get one clause. An operator request
often asks several joined by commas or "and", and each is a separate clause.

  "Write a 10 pager on the early signs of burnout and depression in IT
   professionals, how ai is causing more/less"
     -> ["the early signs of burnout and depression in IT professionals",
         "how AI is causing more or less of it"]

Split on what is being ASKED, not on grammar. Do not invent a clause the
request does not contain, and do not merge two asks into one — a merged clause
is how a request gets researched as though half of it were never written.

"queries": 4-5 plain search strings, each targeting a DIFFERENT facet:
  - what exactly happened
  - technical or business background
  - who the parties are and how they relate
  - prior comparable events
  - criticism, risks, or consequences

EVERY clause needs at least one query of its own, searched on ITS OWN terms.
A request about burnout AND about AI's effect on it needs queries for burnout
by itself, not only queries with "AI" welded onto them — the intersection of
two topics is far thinner than either, and searching only the intersection is
how a whole clause ends up with no sources at all.

If the subject is a FORMAT, PROTOCOL, SPECIFICATION, API, LIBRARY or TOOL —
anything a reader could go and use — at least one query MUST target the primary
source rather than coverage of it. Add words like "documentation", "specification",
"reference", "schema", "github", or "example" to that query.

This matters more than it looks. News articles describe a format in prose;
documentation shows it. A story about a protocol researched only through
journalism yields no example a reader can act on, which is the difference
between explaining something and merely mentioning it.

Every query must include the disambiguating context when you have one. When
entity_context is empty, search the bare term with words taken from the item —
that finds the real subject instead of a confident guess.

"image_query": a separate short search for a PHOTOGRAPH to sit behind the first
slide. This is a picture search, not a text search, so describe the subject
visually rather than asking a question — "Uber robotaxi London street",
"OpenAI headquarters building", "data centre server racks". Prefer a concrete
physical subject: a place, a building, a device, a vehicle, a person. Avoid
abstractions like "artificial intelligence" or "innovation", which return stock
imagery of glowing brains. If the story has no physical subject at all, name the
most closely associated real-world object.

Write search strings, not questions to a chatbot."""

RELEVANCE_SYSTEM = """You are filtering search results for a research pipeline.

You will be given a subject and an excerpt from a web page. Answer one
question: is this page about the same THING as the subject?

Reject only when the page is about something genuinely different that happens
to share a word. A page about mouse cursors is not about the company Cursor. A
page about SQL cursors is not either. A login page, a category listing, or a
homepage that never discusses the subject is not either.

Accept whenever the page covers the subject area, EVEN IF it is partial:

- It discusses the topic but does not name every person in the subject. An
  analysis of Anthropic and OpenAI revenue is relevant to a story about an
  executive commenting on that revenue, whether or not it quotes him.
- It covers the technology but not this specific announcement. Background is
  what research is for.
- It disagrees with the subject, or corrects an assumption in it. Those pages
  are especially valuable.
- The subject description may itself be imperfect; it can name an abbreviation
  whose expansion was guessed. Judge against the SUBJECT NAME first, and keep
  a page that is plainly about a thing with that name even when it contradicts
  the parenthetical.

You are not judging whether the page proves the story, and you are not ranking
quality. A later stage weighs the evidence and a human approves the result. Your
only job is to keep out pages about a different subject entirely.

When genuinely uncertain whether it is the same thing, accept."""

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


async def plan_queries(item: Item, llm) -> tuple[list[str], str, str, list[str]]:
    """Return disambiguated text queries, the subject description, an image
    query for the hook background, and the clauses the request asks for."""
    context_blob = _item_context(item)
    plan = await llm.cheap(
        PLAN_SYSTEM, f"NEWS ITEM:\n{context_blob}", schema=PLAN_SCHEMA
    )
    entity = str(plan.get("entity", ""))
    entity_context = str(plan.get("entity_context", ""))
    queries = disambiguate(list(plan.get("queries", [])), entity, entity_context)
    subject = f"{entity} ({entity_context})" if entity_context else entity
    image_query = str(plan.get("image_query", "")).strip() or entity
    clauses = [str(c).strip() for c in (plan.get("clauses") or []) if str(c).strip()]
    return queries, subject, image_query, clauses


async def research(item: Item, llm, http, settings) -> dict:
    """Fan out over queries and return the surviving research notes."""
    queries, subject, image_query, clauses = await plan_queries(item, llm)
    # A channel post is a story, not a request, so it asks for nothing and the
    # planner splitting it produced fragments of its own prose — one item was
    # parked demanding a source for "r considerably harder to dismiss as pure
    # speculation", a mid-word slice of the article's last sentence. Nineteen
    # channel items wedged this way. The gate exists for compound operator
    # requests and applies only to them.
    if item.source != "dm":
        clauses = []
    if not queries:
        raise Retryable("query planner produced no usable queries")

    semaphore = asyncio.Semaphore(max(1, settings.research_concurrency))

    async def guarded(index: int, query: str):
        async with semaphore:
            return await _research_one(index, query, subject, item, llm, http, settings)

    # One branch hunts a photograph for the hook background while the others
    # read text. It is deliberately outside the note accounting: a story with
    # no good picture is still a story, so this can come back empty without
    # affecting whether the item has enough research to proceed.
    results, background = await asyncio.gather(
        asyncio.gather(
            *(guarded(i, q) for i, q in enumerate(queries)), return_exceptions=True
        ),
        _find_background(image_query, item, http, settings),
    )

    notes: list[dict] = []
    for query, result in zip(queries, results):
        if isinstance(result, Retryforever):
            # Infrastructure, not this researcher. If Ollama or the network is
            # down it is down for all of them, so defer the whole item rather
            # than reporting a research shortfall that never happened.
            raise result
        if isinstance(result, BaseException):
            # One researcher dying is survivable; the others carry the item.
            log.warning("researcher failed for %r: %s", query[:60], result)
            continue
        if result:
            notes.append(result)

    # B2 (bounce 1, D90): a fail-open note (FAIL_OPEN_KEY) is one
    # researcher's read of a SINGLE source the gate never actually cleared —
    # real signal, but not the same evidentiary weight as two independently
    # gate-cleared findings. Counting it toward MIN_NOTES is how item 501
    # went 3 real notes -> 1 real + 4 fail-open non-answers and still
    # completed: only notes the gate actually cleared count here, so that
    # shape now fails closed (Retryable) instead of silently succeeding.
    # (Fail-open notes still ride along in `notes`/`out["research"]` below —
    # marked, not discarded — so a channel item's clause-coverage check
    # (which never runs for it; see `clauses = []` above and D85) is not
    # where this is enforced. For a `dm` item, a fail-open note can still
    # count as `supported` evidence below; narrowing that too is out of
    # T131's stated scope.)
    real_notes = [n for n in notes if not n.get(FAIL_OPEN_KEY)]
    if len(real_notes) < MIN_NOTES:
        raise Retryable(
            f"only {len(real_notes)} of {len(queries)} researchers produced "
            f"non-fail-open notes; need at least {MIN_NOTES}"
        )

    # Every clause the request made needs something that speaks to it. A
    # researcher reporting "the excerpts do not contain information" is stored
    # like any other note, so counting notes proves nothing about coverage —
    # item 45 had four notes and three of them were reports of failure.
    supported = [
        f"{n.get('claim','')} {n.get('detail','')}"
        for n in notes if n.get("confidence") != "low"
    ] or [n.get("claim", "") for n in notes]
    uncovered = await unaddressed(llm, clauses, supported)
    if uncovered:
        raise NeedsInput(
            "I researched this but found nothing that answers:\n"
            + "\n".join(f"  · {c}" for c in uncovered)
            + "\n\nReply with a better angle or a source, /post to send what "
              "I have for approval, or /drop.",
            resume_status=Status.TRIAGED,
            # Carried so answering resumes from the searches already paid for,
            # and so a parked item can be inspected to see what parked it.
            # Which clauses failed, not just that some did: /post uses
            # this to tell compose what it is missing.
            fields={"research": notes, "clauses": clauses,
                    "gaps": uncovered},
        )

    out: dict = {"research": notes, "clauses": clauses}
    if background:
        # Presented to compose exactly like an attached image, so the existing
        # index-resolution and rating guards apply unchanged.
        extracted = dict(item.extracted or {})
        images = list(extracted.get("image_descriptions", []))
        images.append({
            "path": str(background["path"]),
            "description": f"Researched photograph: {background['title'][:160]}",
            "usable": "background",
            "why": f"found for the hook background via {background['host']}",
            "researched": True,
        })
        extracted["image_descriptions"] = images
        out["extracted"] = extracted
    return out


async def _find_background(
    query: str, item: Item, http, settings
) -> dict | None:
    """Find one photograph for the hook background. Never raises."""
    if not query:
        return None
    try:
        candidates = await image_search(http, settings.searxng_url, query)
    except Exception as exc:
        log.info("background image search failed for %r: %s", query[:50], exc)
        return None

    target = Path(settings.media_dir) / "researched" / str(item.id)
    for candidate in candidates[:6]:
        path = await download_image(http, candidate["url"], target)
        if path:
            host = urlparse(candidate["url"]).netloc
            log.info("background image for item %s: %s", item.id, host)
            return {"path": path, "title": candidate.get("title", ""), "host": host}
    log.info("no usable background image found for %r", query[:50])
    return None


# ----------------------------------------------------------------- internals


class _GateVerdict(StrEnum):
    """Outcome of one relevance-gate call.

    Kept distinct from a plain bool (B1, bounce 1, D88) so a fail-open
    decision can tell a gate that returned a correct, working "not relevant"
    verdict (REFUSED) from a gate that returned no verdict at all (ERRORED).
    Collapsing the two into one boolean was the exact defect that let T72's
    fail-open override a correct rejection on >=96.6% of its firings: the
    code had no way to distinguish "the gate broke" from "the gate worked
    and said no" once both had become ``False``.
    """

    ACCEPTED = "accepted"
    REFUSED = "refused"
    ERRORED = "errored"


def _item_context(item: Item) -> str:
    parts = [item.raw_text or ""]
    extracted = item.extracted or {}
    for described in extracted.get("image_descriptions", []):
        if described.get("description"):
            parts.append(f"[image] {described['description']}")
    for page in extracted.get("url_texts", []):
        if page.get("text"):
            parts.append(f"[link {page['url']}]\n{page['text']}")
    return "\n\n".join(p for p in parts if p.strip())


async def _research_one(
    index: int, query: str, subject: str, item: Item, llm, http, settings
) -> dict | None:
    results = await searx(
        http, settings.searxng_url, query, settings.results_per_query,
        categories=getattr(settings, "searxng_categories", DEFAULT_CATEGORIES),
    )
    picked = dedupe_by_domain(results, settings.docs_per_query)
    if not picked:
        return None

    fetched: list[tuple[str, str]] = []
    # B1 (bounce 1, D88): everything fetched but turned away by the gate,
    # split by WHY it was turned away. Only ERRORED (the gate returned no
    # verdict at all) may ever fail open below — a REFUSED source (the gate
    # returned a working "not relevant" verdict) is dropped for good. The
    # grep that motivated widening this to "any rejection" was wrong: of
    # every "rejected as irrelevant" event logged in production, <=3.4% were
    # a genuine gate failure and >=96.6% were a correct, working verdict
    # (D88) — collapsing REFUSED into the same pool as ERRORED is what let
    # fail-open override the gate on almost every firing.
    errored: list[tuple[str, str]] = []
    for result in picked:
        url = result["url"]
        text = await fetch_text(http, url)
        body = (text or result.get("content") or "").strip()
        if not body:
            continue

        verdict = await _is_relevant(llm, subject, url, body, settings)
        if verdict is _GateVerdict.ACCEPTED:
            fetched.append((url, body))
        elif verdict is _GateVerdict.ERRORED:
            errored.append((url, body))
        # _GateVerdict.REFUSED: dropped here, never reconsidered — this is
        # what restores the mouse-cursor guarantee (B1).

    fail_open_used = False
    if not fetched:
        if not errored:
            log.info("researcher %s: every source rejected as irrelevant", index)
            return None
        # RELEVANCE_SYSTEM's own instruction is "when genuinely uncertain,
        # accept" — a facet where every fetched candidate ERRORED (the gate
        # itself never returned a verdict) is a broken gate, not a genuinely
        # empty facet. Re-admit only the single highest-ranked
        # (first-searched) ERRORED candidate — bounded blast radius, never
        # the whole errored pool and never anything the gate explicitly
        # refused — and log the trade loudly so it stays visible rather than
        # silently thinning the corpus. The note built from it is marked
        # FAIL_OPEN_KEY below so research()'s MIN_NOTES gate (B2) does not
        # count one unverified source as a real finding.
        fail_open_used = True
        url, body = errored[0]
        log.warning(
            "researcher %s: every source rejected/errored; failing open on "
            "top-ranked ERRORED %s (%d source(s) errored)",
            index, urlparse(url).netloc, len(errored),
        )
        fetched = [(url, body)]

    # T70/T61: uncapped article text (T60) plus a pinned local num_ctx (llm.py)
    # means the corpus can outgrow what the model actually loaded can hold.
    # budget.py fair-shares the corpus across sources and marks anything cut,
    # sized for whichever model this call's settings will reach first — see
    # budget.py's module docstring for why that is as precise as it can be.
    budget_chars = budget_chars_for_role(settings, role="cheap")
    packed = pack(
        [Source(id=url, text=body) for url, body in fetched], budget_chars
    )
    if any(p.omitted_chars for p in packed):
        # This manifest embeds p.id (an attacker-influenced URL — see
        # budget.py's "load-bearing, not defense-in-depth" note) in a log
        # line. It is currently safe from log injection only BY ACCIDENT:
        # the %-style lazy logging call defers str()/repr() of the list
        # argument until formatting, and dict/str repr() escapes embedded
        # newlines as literal "\n" rather than emitting a real line break.
        # If this is ever refactored to an eager f-string, that escaping is
        # lost and a URL containing a real newline could forge extra log
        # lines — keep this call %-style (or otherwise repr()-escaped).
        log.info(
            "researcher %s: packed corpus into %d/%d chars budget: %s",
            index, sum(len(p.text) for p in packed), budget_chars,
            [
                {"url": p.id, "kept": p.kept_chars, "omitted": p.omitted_chars}
                for p in packed
            ],
        )

    corpus = [f"[SOURCE: {p.id}]\n{p.text}" for p in packed]
    sources = [p.id for p in packed]

    user = (
        f"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\n"
        f"RESEARCH QUESTION: {query}\n\n"
        f"WEB EXCERPTS:\n\n" + "\n\n---\n\n".join(corpus)
    )
    # T71: the note IS the post's substance — everything downstream (brief,
    # deck) can only be as specific as it is — so which role writes it is
    # config, not a call site pinned to `cheap`. Quota-neutral (D8): the
    # free tier is one 20 RPM / 1000-day pool shared account-wide by every
    # ":free" slug, so moving this call between tiers spends the same
    # quota either way. Reversible with one env var.
    write_note = getattr(llm, settings.research_note_role)
    note = await write_note(RESEARCH_SYSTEM, user, schema=NOTE_SCHEMA)
    return {
        "question": query,
        "claim": str(note.get("claim", "")),
        "detail": str(note.get("detail", "")),
        "confidence": str(note.get("confidence", "low")),
        "sources": sources,
        # B2 (bounce 1, D90): always present, never inferred — a downstream
        # reader must not have to guess whether a note came from the
        # fail-open path.
        FAIL_OPEN_KEY: fail_open_used,
    }


async def _is_relevant(llm, subject: str, url: str, body: str, settings) -> _GateVerdict:
    """Gate a single document.

    Returns ACCEPTED, REFUSED (a working verdict said no — never re-admitted,
    see ``_research_one``) or ERRORED (no verdict was returned at all — the
    only one of the three ``_research_one`` may ever fail open on). A model
    that cannot be reached means nothing about the page at all — treating
    that as a rejection turns a transient outage into a permanent item
    failure, reported as "every source rejected as irrelevant", which sends
    anyone debugging it after the research logic instead of the
    infrastructure.

    T72 originally sized the excerpt from the same context budget
    ``budget.py`` derives for the whole note-writing corpus — the wrong
    quantity for a single binary verdict (B3, bounce 1): it measurably
    bought no accept-rate improvement beyond noise while tripling wall-clock
    on a real item. ``RELEVANCE_EXCERPT_CHARS`` is sized from a dedicated
    measurement instead (see its own comment) and packed through the same
    marked-omission mechanism as the corpus itself: a page that still does
    not fit is trimmed with a trace (D5/D32), never a silent, unmarked cut.
    """
    packed = pack([Source(id=url, text=body)], RELEVANCE_EXCERPT_CHARS)[0]
    try:
        verdict = await llm.cheap(
            RELEVANCE_SYSTEM,
            f"SUBJECT: {subject}\n\nPAGE URL: {packed.id}\n\n"
            f"PAGE EXCERPT:\n{packed.text}",
            schema=RELEVANCE_SCHEMA,
        )
    except Retryforever:
        # Infrastructure is down. Let this propagate so the worker defers the
        # item without spending an attempt, rather than silently discarding
        # perfectly good sources.
        raise
    except Exception as exc:
        # No verdict at all: a real gate failure, distinct from a working
        # "not relevant" — see _GateVerdict.
        log.warning("relevance gate errored for %s: %s", packed.id, exc)
        return _GateVerdict.ERRORED

    if not verdict.get("relevant"):
        log.info(
            "rejected %s as irrelevant: %s",
            urlparse(packed.id).netloc, str(verdict.get("why", ""))[:120],
        )
        return _GateVerdict.REFUSED
    return _GateVerdict.ACCEPTED
