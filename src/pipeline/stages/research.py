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
import re
from enum import StrEnum
from urllib.parse import urlparse

from ..budget import Source, budget_chars_for_role, pack
from ..errors import EnginesBlocked, Retryable, Retryforever
from ..conversation import NeedsInput
from ..coverage import unaddressed
from ..models import Item, Status
from .enumerate_items import INTENT_EXPLAIN
from pathlib import Path

from ..search import (
    DEFAULT_CATEGORIES,
    dedupe_by_domain,
    download_image,
    extract_urls,
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

#: A **measured** limit (house rule 2's taxonomy: platform / measured /
#: editorial) -- not a platform ceiling and not an editorial judgement call,
#: so it is expected to move if a future, better-resolved measurement says
#: so; see below for exactly what would still need to change it.
#:
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
#: already-measured point rather than re-guessed. Note the DIRECTION, not
#: just the magnitude: D91's own point estimate favoured the WIDER excerpt
#: (+0.0232, i.e. more accepted, not fewer) -- it just sat inside the noise
#: band, not outside it. Nothing below contradicts that.
#:
#: ``scripts/measure_relevance_excerpt.py`` (new, this task) is the
#: re-runnable, cited confirmation house rule 4 asks for -- reuses
#: eval_research.py's own concurrent research() run rather than a slower
#: sequential one. One live rep, item 501 only, provider=ollama,
#: cheap-model=qwen3:4b-instruct, docs_per_query=3, results_per_query=6,
#: 2026-09-23:
#:   size= 1200: gate_accept_rate=0.6667 (10/15), wall_clock=455.1s
#:   size=12288: gate_accept_rate=0.6000 ( 9/15), wall_clock=516.0s
#: A single gate call flipped between the two runs (10 vs 9 of the same 15
#: candidates) -- the identical 1-call magnitude D91 itself called noise,
#: just a larger FRACTION here because n=15 (one item) is far smaller than
#: D91's 43-candidate sample. WITHIN THIS SCRIPT'S OWN DATA it favours 1200
#: (higher accept rate, lower wall-clock) -- but that is not "never the
#: reverse" across every measurement taken: D91's larger-sample point
#: estimate leaned the other way (previous paragraph), just inside its own
#: band. D97 (project-context.md) gives the reason a 15-candidate sample
#: cannot settle the direction either way: the smallest move 15 candidates
#: can express is 1/15 = 0.0667 = 2.9x D83's entire +/-0.023 noise floor, so
#: this run sits BELOW the resolution of the threshold it is being compared
#: against -- it corroborates D91 by failing to contradict it, not by
#: confirming a direction. House rule 10 applies too: one rep proves nothing
#: on its own. Only a full-golden-set ``--reps`` run has enough resolution
#: to actually settle 1200 vs 12288; until one runs, this stays at 1200 as
#: the smallest already-measured, not-yet-contradicted point.
#:
#: T90/D32 ruling: this site is the "third unmarked trim" D32 asked a future
#: task to rule in or out. RULED IN, and already closed by the B3 work
#: above -- `_is_relevant` packs this excerpt through the identical
#: marked-omission `pack()` mechanism the corpus itself uses (D5), so a page
#: that does not fit is cut with a trace, never silently. T90 leaves the
#: SIZE at the dedicated, measured constant here rather than switching it to
#: `budget_chars_for_role(..., role="cheap")` like the other two T90 sites:
#: this excerpt was ALREADY measured against that exact alternative (D91,
#: D93, D97) and the wider budget bought no accept-rate improvement outside
#: noise while costing materially more wall-clock. Re-widening it under a
#: different name would silently re-open a settled, evidence-backed call.
RELEVANCE_EXCERPT_CHARS = 1200
#: Editorial: how much of the operator's request the gate sees to tell which sense of an
#: ambiguous subject is meant. A request is a sentence or two; this bounds a pasted essay.
REQUEST_CONTEXT_CHARS = 400

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
        # False when the excerpts do not answer the question. Such a note is a report
        # of failure, not evidence, and is dropped (T366).
        "answers": {"type": "boolean"},
    },
    "required": ["claim", "detail", "confidence", "answers"],
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
- The item or a LINKED PAGE in it DOES state the expansion? Then use it, in
  "entity" and in the queries: a page that opens "Next-Concept Modeling (NCM)
  is a framework for..." makes the entity "Next-Concept Modeling (NCM)", and a
  bare "NCM" query is the one that finds the wrong things.

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

"queries": 4-5 search strings, each the way a person types into a search box:
at most 6 words, no filler, no repeated words. The FIRST query is the bare subject
itself ("Next-Concept Modeling"). The others add ONE angle each, in a few words:
"Next-Concept Modeling vs LLM", "how Next-Concept Modeling works". Do not stack the
subject, its abbreviation and clause words into one string.

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

When the subject is an abbreviation, the REQUEST (if given) says which sense is
meant: a page about a different sense of the same letters (an association, a
company, a medical term) is not the same thing, whatever else it says.

When genuinely uncertain whether it is the same thing, accept."""

RESEARCH_SYSTEM = """You are a research assistant for a tech-news publisher.

Read the web excerpts and answer the research question with a single factual
finding.

Rules:
- Use ONLY what the excerpts support.
- If the excerpts do not answer the question, set "answers" to false (and confidence
  "low"); put what is missing in "detail". "answers" is true only when "claim" states
  something the excerpts actually say about the question. A claim that merely reports
  the excerpts' silence ("the excerpts do not explain...") is never a finding.
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


#: Most queries one research run searches: PLAN_SCHEMA's ``maxItems`` for "queries".
MAX_QUERIES = 6
#: A query is what a person types into a search box. Editorial: measured on the live
#: SearXNG, "next concept modeling" found the paper and the repo while the 12-word
#: pile-ups of subject + abbreviation + field + clause words found JVM and apartments.
MAX_QUERY_WORDS = 6
_PAREN = re.compile(r"\s*\([^)]*\)")


def bare_subject(entity: str) -> str:
    """The subject as one short query: its name without a parenthetical."""
    return _PAREN.sub("", entity or "").strip() or (entity or "").strip()


def _key(query: str) -> str:
    return " ".join(query.replace("-", " ").split()).lower()


def shape_queries(queries: list[str], bare: str) -> list[str]:
    """Search-box queries: whitespace collapsed, cut to MAX_QUERY_WORDS, deduplicated
    (hyphens and case ignored), the bare subject first, at most MAX_QUERIES. Enforced
    here, not left to the prompt."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in [bare, *queries]:
        query = " ".join(str(raw or "").replace('"', " ").split()[:MAX_QUERY_WORDS])
        if query and _key(query) not in seen:
            seen.add(_key(query))
            out.append(query)
    return out[:MAX_QUERIES]


#: Hosts that ARE the thing rather than coverage of it. Editorial: papers, code and
#: model hubs; a request about a method is best answered by its paper or repository.
PRIMARY_SOURCE_HOSTS = ("arxiv.org", "github.com", "huggingface.co")
#: Search results read per query, as a multiple of results_per_query. Editorial: the
#: primary source often ranks below the first page of news coverage (the NCM paper was
#: 6th), and ranking happens after this wider pool is fetched.
SEARCH_POOL_FACTOR = 2


def _is_primary(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in PRIMARY_SOURCE_HOSTS)


def prefer_primary(results: list[dict]) -> list[dict]:
    """Primary-source hosts first; every other result keeps its search order."""
    return sorted(results, key=lambda r: not _is_primary(str(r.get("url", ""))))


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


def _hint_block(hints: dict | None) -> str:
    """What the intent classifier already worked out, as planner input."""
    if not hints:
        return ""
    lines = []
    if hints.get("subject"):
        lines.append(f"SUBJECT (from triage): {hints['subject']}")
    if hints.get("clauses"):
        lines.append("THE REQUESTER ASKS (answer each; one clause apiece):")
        lines.extend(f"  - {c}" for c in hints["clauses"])
    return ("\n\n" + "\n".join(lines)) if lines else ""


async def plan_queries(
    item: Item, llm, settings, hints: dict | None = None,
) -> tuple[list[str], str, str, list[str]]:
    """Return disambiguated text queries, the subject description, an image
    query for the hook background, and the clauses the request asks for."""
    context_blob = _item_context(item)
    # T90/D5: `_item_context` concatenates the raw item text plus every
    # extracted image description and linked-page body -- uncapped, exactly
    # like triage.py's `judgeable_text` (search.py's MAX_TEXT_CHARS = 0), and
    # this is the SECOND of T61's originally-uncapped call sites (the first
    # was the note-writing corpus, closed by T70's pack() below in
    # _research_one). Feeding it straight to llm.cheap risks the same
    # silent, unmarked tail-drop against num_ctx_cheap that T70 closed for
    # the corpus. Reuses the identical mechanism -- one synthetic Source,
    # budgeted for the role this call actually uses (PLAN_SYSTEM is
    # hardcoded to `llm.cheap`, see test_llm_role_routing.py's frozen
    # census), cut with a trace (D5) rather than a silent slice.
    # ``id="item"`` is pipeline-generated, never attacker-controlled (D60) --
    # there is only one blob to pack here, so the id exists to satisfy
    # Source's shape, not to identify anything in a manifest.
    budget_chars = budget_chars_for_role(settings, role="cheap")
    packed = pack([Source(id="item", text=context_blob)], budget_chars)[0]
    if packed.omitted_chars:
        log.info(
            "plan_queries item %s: item context packed into %d/%d chars "
            "budget (%d chars omitted, %d kept)",
            item.id, len(packed.text), budget_chars,
            packed.omitted_chars, packed.kept_chars,
        )
    plan = await llm.cheap(
        PLAN_SYSTEM, f"NEWS ITEM:\n{packed.text}{_hint_block(hints)}",
        schema=PLAN_SCHEMA,
    )
    entity = str(plan.get("entity", ""))
    entity_context = str(plan.get("entity_context", ""))
    raw = [str(q) for q in plan.get("queries", [])]
    if item.source == "dm":  # an operator request is searched like a person would; channel posts as before
        raw = shape_queries(raw, bare_subject(entity))
    queries = list(dict.fromkeys(disambiguate(raw, entity, entity_context)))  # context can make two identical
    subject = f"{entity} ({entity_context})" if entity_context else entity
    image_query = str(plan.get("image_query", "")).strip() or entity
    clauses = [str(c).strip() for c in (plan.get("clauses") or []) if str(c).strip()]
    # The classifier read the request in the requester's words; where it split
    # the asks, keep its split so each one is answered and checked.
    clauses = [str(c).strip() for c in ((hints or {}).get("clauses") or [])
               if str(c).strip()] or clauses
    return queries, subject, image_query, clauses


def _thin_question(subject: str, have: list[dict]) -> str:
    """The honest question when research found too little. Offers /post only when
    there is something to post (handle_answer refuses it otherwise)."""
    if not have:
        return (
            f"I couldn't find a usable source on {subject}.\n\n"
            "Reply with a link or a better search term, or /drop."
        )
    hosts = ", ".join(dict.fromkeys(
        urlparse(u).netloc for n in have for u in n.get("sources", [])[:1]
    ))
    return (
        f"I found only {len(have)} solid source{'s' if len(have) != 1 else ''} on "
        f"{subject}: {hosts}.\n\nReply with another link or search term, /post to "
        "write from what I have, or /drop."
    )


async def research(item: Item, llm, http, settings, hints: dict | None = None) -> dict:
    """Fan out over queries and return the surviving research notes."""
    queries, subject, image_query, clauses = await plan_queries(
        item, llm, settings, hints,
    )
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

    # The operator's own link is the primary source: read it first, trust it
    # (they sent it), and let the web fill in around it (T366).
    primary = await _primary_notes(item, clauses, llm, settings, http=http)

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
        _find_background(image_query, subject, item, http, settings),
    )

    notes: list[dict] = []
    crashed = 0
    blocked: EnginesBlocked | None = None
    for query, result in zip(queries, results):
        if isinstance(result, EnginesBlocked):
            # One query's empty-with-a-suspended-engine is ambiguous: a
            # self-hosted engine can sit CAPTCHA-suspended for days while the
            # others answer fine. Count it as an empty query (it lands in
            # `starved` below) and defer only if nothing at all came back.
            blocked = result
            continue
        if isinstance(result, Retryforever):
            # Infrastructure, not this researcher. If Ollama or the network is
            # down it is down for all of them, so defer the whole item rather
            # than reporting a research shortfall that never happened.
            raise result
        if isinstance(result, BaseException):
            # One researcher dying is survivable; the others carry the item.
            log.warning("researcher failed for %r: %s", query[:60], result)
            # T194: counted for the Retryable breakdown below, never the
            # exception TEXT itself -- that stays in the log.warning above
            # only. An OpenRouter error body can carry provider-echoed
            # content (D166); `last_error` is exactly what the raised
            # message below becomes (worker.py's generic except-Exception
            # path stores ``str(exc)``), so nothing from `result` may reach
            # it -- only this count does.
            crashed += 1
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
    # One page the operator handed over is evidence enough to build on when the
    # web adds nothing; without it, MIN_NOTES independent findings are needed.
    # The operator chose "post from one good source" for an explainer they asked for
    # by DM; a news claim and any channel post still need independent corroboration.
    explainer_dm = item.source == "dm" and (hints or {}).get("intent") == INTENT_EXPLAIN
    min_notes = 1 if primary or explainer_dm else MIN_NOTES
    if blocked is not None and not notes and not primary:
        # Every researcher came back empty and at least one was blocked: the
        # shortfall is the search outage, not the web (T260).
        raise blocked
    have = [*primary, *real_notes]
    if len(have) < min_notes:
        if item.source == "dm" and not crashed:
            # The operator is in the loop: ask, keeping what was found, rather than
            # burn retries and end failed (T366). A crashed researcher is a transient
            # fault and still retries.
            raise NeedsInput(
                _thin_question(subject, have),
                resume_status=Status.TRIAGED,
                fields={"research": have, "clauses": clauses},
            )
        # T194/D164: "only N of M researchers produced notes" used to mean
        # any of three different things -- the web had nothing, the
        # relevance gate rejected everything, or a researcher never got to
        # render a verdict at all (an OpenRouter 503, an httpx timeout, a
        # provider error, unparseable JSON) -- and nothing downstream
        # (a human reading `last_error`, or T185/T186's hold) could tell
        # which. Every query landed in exactly one of four buckets by this
        # point: `crashed` (counted above -- any Retryforever already
        # escaped, so this is exhaustively "raised, and it was not
        # infrastructure-wide"), `starved` (_research_one returned
        # cleanly with no note -- an empty web and a fully-refused gate
        # both collapse to this identical signal today, since
        # _research_one does not report which one happened), `fail_open`
        # (a note came back, but the gate never actually cleared its one
        # source -- D90), and `real_notes` (independently gate-cleared).
        # The four always sum to `len(queries)`; test_research.py pins
        # that invariant directly, alongside each named scenario.
        fail_open = len(notes) - len(real_notes)
        starved = len(queries) - crashed - len(notes)
        raise Retryable(
            f"only {len(real_notes)} of {len(queries)} researchers produced "
            f"non-fail-open notes; need at least {min_notes} "
            f"({crashed} crashed, {starved} found nothing, "
            f"{fail_open} fail-open)"
        )

    notes = [*primary, *notes]

    # Every clause the request made needs something that speaks to it. A
    # researcher reporting "the excerpts do not contain information" is stored
    # like any other note, so counting notes proves nothing about coverage —
    # item 45 had four notes and three of them were reports of failure.
    supported = [
        f"{n.get('claim','')} {n.get('detail','')}"
        for n in notes if n.get("confidence") != "low"
    ] or [n.get("claim", "") for n in notes]
    uncovered = await unaddressed(llm, clauses, supported)
    # Ask only when nothing the request said was answered. A forwarded story makes
    # side claims the web often misses, and parking on one got /post every time
    # (items 639, 645, 658, 671); the gaps go to compose instead, as /post does.
    answered_nothing = len(uncovered) >= len(clauses)
    if uncovered and answered_nothing and not explainer_dm:
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

    # The operator's follow-up has been used. Left on the item it would colour every
    # later re-run or requeue of research with an answer to a question long closed.
    out: dict = {"research": notes, "clauses": clauses, "answer": None}
    if uncovered:
        # Posted from what was found; compose is told which parts the notes do not
        # cover (the same `gaps` the /post path sets).
        out["gaps"] = uncovered
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


_WORD = re.compile(r"[a-z0-9]+")


def shows_subject(title: str, subject: str) -> bool:
    """Whether an image's title names the subject. The first search hit was used
    whatever it showed: "Motivational Speaker Presenting at Conference" behind a
    Jensen Huang story (item 688). No background beats a random one."""
    words = {w for w in _WORD.findall(bare_subject(subject).lower()) if len(w) >= 3}
    return bool(words & set(_WORD.findall(title.lower())))


async def _find_background(
    query: str, subject: str, item: Item, http, settings
) -> dict | None:
    """Find one photograph of the subject for the hook background. Never raises."""
    if not query:
        return None
    try:
        candidates = await image_search(http, settings.searxng_url, query)
    except Exception as exc:
        log.info("background image search failed for %r: %s", query[:50], exc)
        return None

    target = Path(settings.media_dir) / "researched" / str(item.id)
    candidates = [c for c in candidates if shows_subject(c.get("title", ""), subject)]
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
    if item.answer:
        # The operator replied to a question about this item: their words are the newest.
        parts.append(f"[operator follow-up] {item.answer}")
    extracted = item.extracted or {}
    for described in extracted.get("image_descriptions", []):
        if described.get("description"):
            parts.append(f"[image] {described['description']}")
    for page in extracted.get("url_texts", []):
        if page.get("text"):
            parts.append(f"[link {page['url']}]\n{page['text']}")
    return "\n\n".join(p for p in parts if p.strip())


#: Linked pages read as primary sources. Editorial: a DM is one request, and a
#: third link is rarely the subject; each page costs a note call.
MAX_PRIMARY_PAGES = 2



async def _primary_notes(
    item: Item, clauses: list[str], llm, settings, http=None,
) -> list[dict]:
    """One note per page the operator linked, written from that page alone.

    Only for an operator request: a channel post's link is part of someone
    else's story, not a source the operator chose. The page is not put through
    the relevance gate; the operator sending it is the relevance judgement.
    """
    if item.source != "dm":
        return []
    pages = [
        p for p in ((item.extracted or {}).get("url_texts") or [])
        if str(p.get("text") or "").strip() and p.get("url")
    ]
    have = {p["url"] for p in pages}
    if http is not None and item.answer:
        # A link in the operator's reply to "send me another source" is a source they
        # chose: read it exactly like a link in the first message.
        for url in extract_urls(item.answer):
            if url in have or len(pages) >= MAX_PRIMARY_PAGES:
                continue
            text = await fetch_text(http, url)
            if text and text.strip():
                pages.append({"url": url, "text": text})
    pages = pages[:MAX_PRIMARY_PAGES]
    if not pages:
        return []
    question = "; ".join(clauses) or (item.raw_text or "")
    budget_chars = budget_chars_for_role(settings, role=settings.research_note_role)
    write_note = getattr(llm, settings.research_note_role)
    notes = []
    for page in pages:
        packed = pack([Source(id=page["url"], text=str(page["text"]))], budget_chars)[0]
        user = (
            f"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\n"
            f"RESEARCH QUESTION: {question}\n\n"
            f"WEB EXCERPTS:\n\n[SOURCE: {packed.id}]\n{packed.text}"
        )
        try:
            note = await write_note(RESEARCH_SYSTEM, user, schema=NOTE_SCHEMA)
        except Retryforever:
            raise  # infrastructure down: defer the item, do not skip the page
        except Exception as exc:
            # One bad page must not fail the item; the web research still runs.
            log.warning("primary page %s could not be read: %s",
                        urlparse(str(page["url"])).netloc, exc)
            continue
        if note.get("answers") is False:
            log.info("primary page %s does not answer the question; dropped",
                     urlparse(str(page["url"])).netloc)
            continue
        notes.append({
            "question": question,
            "claim": str(note.get("claim", "")),
            "detail": str(note.get("detail", "")),
            "confidence": str(note.get("confidence", "low")),
            "sources": [page["url"]],
            FAIL_OPEN_KEY: False,
        })
    return notes


async def _research_one(
    index: int, query: str, subject: str, item: Item, llm, http, settings
) -> dict | None:
    results = await searx(
        http, settings.searxng_url, query, settings.results_per_query * SEARCH_POOL_FACTOR,
        categories=getattr(settings, "searxng_categories", DEFAULT_CATEGORIES),
    )
    picked = dedupe_by_domain(prefer_primary(results), settings.docs_per_query)
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

        verdict = await _is_relevant(
            llm, subject, url, body, settings, request=item.raw_text or "",
        )
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
    # sized for the role this corpus is ACTUALLY fed to below (T71 made the
    # note call config-driven — see budget.py's module docstring for why
    # "whichever model this call's settings will reach first" is as precise
    # as the role alone can be). N1 (bounce 1, reviewer): this used to be
    # hardcoded to "cheap" even when ``research_note_role`` routed the note
    # call to "good", so a good-routed call got a corpus sized for cheap's
    # smaller num_ctx — silently dampening T71's own effect. Safe at every
    # shipped config only because NUM_CTX_GOOD >= NUM_CTX_CHEAP is a real
    # invariant, not an accident (budget.py exists precisely to prevent the
    # reverse from overflowing).
    budget_chars = budget_chars_for_role(settings, role=settings.research_note_role)
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
    if note.get("answers") is False:
        log.info("researcher %s: sources did not answer %r; note dropped", index, query[:60])
        return None
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


async def _is_relevant(
    llm, subject: str, url: str, body: str, settings, request: str = "",
) -> _GateVerdict:
    """Gate a single document.

    Returns ACCEPTED, REFUSED (a working verdict explicitly said no — never
    re-admitted, see ``_research_one``) or ERRORED (no *usable* verdict came
    back — the only one of the three ``_research_one`` may ever fail open
    on). ERRORED covers both the call itself failing AND a verdict that came
    back but does not parse into a real answer (not a dict, or a dict
    missing "relevant") — ``llm.parse_json`` returns whatever valid JSON the
    model emits, and nothing upstream yet validates it against
    ``RELEVANCE_SCHEMA`` (T81 does not exist yet), so a malformed-but-present
    verdict is reachable, not hypothetical. A model that cannot be reached
    means nothing about the page at all — treating that as a rejection turns
    a transient outage into a permanent item failure, reported as "every
    source rejected as irrelevant", which sends anyone debugging it after
    the research logic instead of the infrastructure.

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
            f"SUBJECT: {subject}\n\n"
            + (f"REQUEST: {request[:REQUEST_CONTEXT_CHARS]}\n\n" if request else "")
            + f"PAGE URL: {packed.id}\n\nPAGE EXCERPT:\n{packed.text}",
            schema=RELEVANCE_SCHEMA,
        )
    except Retryforever:
        # Infrastructure is down. Let this propagate so the worker defers the
        # item without spending an attempt, rather than silently discarding
        # perfectly good sources.
        raise
    except Exception as exc:
        # No verdict at all: a real gate failure, distinct from a working
        # "not relevant" — see _GateVerdict. Log only the domain: packed.id
        # is an attacker-influenced URL (reachable via the ``text or
        # result.get("content")`` fallback in ``_research_one``) and %s does
        # NOT escape embedded newlines the way repr() does — see the
        # corpus-manifest log's own comment above for exactly this hazard.
        log.warning(
            "relevance gate errored for %s: %s", urlparse(packed.id).netloc, exc
        )
        return _GateVerdict.ERRORED

    # N2/N3 (bounce 1, reviewer): ``llm.parse_json`` returns whatever valid
    # JSON the model emitted, and nothing upstream validates it against
    # RELEVANCE_SCHEMA yet (T81 does not exist). A non-dict verdict used to
    # raise out of this function on ``.get`` below, aborting the whole
    # facet loop instead of just this one candidate; a dict missing
    # "relevant" was silently binned REFUSED, which is not the same claim
    # as "the model said this source is not relevant". Both are really "no
    # usable verdict came back" — ERRORED, same as an outright call failure.
    if not isinstance(verdict, dict) or "relevant" not in verdict:
        log.warning(
            "relevance gate returned a malformed verdict for %s: %r",
            urlparse(packed.id).netloc, verdict,
        )
        return _GateVerdict.ERRORED

    if not verdict.get("relevant"):
        log.info(
            "rejected %s as irrelevant: %s",
            urlparse(packed.id).netloc, str(verdict.get("why", ""))[:120],
        )
        return _GateVerdict.REFUSED
    return _GateVerdict.ACCEPTED
