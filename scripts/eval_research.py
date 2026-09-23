"""Score the research stage against real items, with a frozen document cache.

D2 (project-context.md): ``eval_compose.py``'s ``load_golden()`` rebuilds
items at ``Status.SYNTHESIZED`` from a snapshot that already contains
``research`` and ``brief``, then calls ``compose()`` directly. That harness
starts AFTER research finishes, so every research-stage change -- T71
(route notes to ``good``), T72 (relevance gate fail-open), T70's packer --
is invisible to it. This is the harness that closes that gap. It gates
Phase 3: T71 and T72 are blocked behind it.

Like ``eval_compose.py``, everything here is measured mechanically. No model
judges another model's output (coding-standards.md rule 6) -- confidence
mix, gate accept rate and the failure-phrase detector are all string/regex/
set arithmetic, same as ``pipeline.quality``.

**Why a frozen document cache, not just a frozen item.** A live research run
re-searches the live web and re-fetches whatever page currently lives at a
URL. Two runs a day apart are not measuring the same corpus, so a quality
difference between them could be the pipeline changing OR the web changing
-- indistinguishable without a cache. ``CachingHTTP`` below memoizes every
SUCCESSFUL (2xx) GET (search results and fetched pages alike) to
``data/eval/corpus_cache/``, so a source that has already returned 2xx once
is replayed byte-identical, with zero network calls, on every later run.

**This is NOT "zero network calls, full stop", and the model is NOT the
only thing that can still vary between runs -- corrected, T73 bounce 2.**
B2 (bounce 1) deliberately never caches a non-2xx response (a bot-block, a
Cloudflare interstitial, a rate limit -- infrastructure noise, not content),
so any source still failing reaches the network LIVE on every single rep
(D84: measured 9 live fetches/rep against the corpus this task inherited --
``--reps`` buys reproducibility of what succeeds, not freedom from the
network). And because a non-2xx source is retried forever rather than
permanently cached as a failure, the corpus itself can change between two
runs with no pipeline or model change at all, if whatever blocked that
source clears up in between (D86 audited the corpus clean -- 51/51 2xx --
as of this task, but "clean today" is a property of the corpus at audit
time, not an invariant this cache enforces going forward).

**Why the query PLAN is frozen too (a deliberate extension of "cache URL ->
text", not a separate idea).** The plan step is itself a live, non-
deterministic model call, and it is what DECIDES which URLs get searched.
If the planner phrases the same facet differently between two runs -- a
real possibility even at a fixed temperature -- it mints a brand-new query,
which mints a brand-new URL, which is a legitimate ``CachingHTTP`` MISS even
though nothing about this item's config actually changed. That would make
"zero network fetches on the second run" flaky for a reason that has
nothing to do with the caching layer being broken. ``Recorder`` below
freezes just the plan call, keyed by the exact request text within a
config-namespaced (fingerprint) directory (T73 bounce 1, B3 nit -- see
``Recorder``'s own docstring for why content, not ``item.id``, is the key),
for exactly this reason -- and deliberately nothing else: the relevance
gate and note-writing calls stay live. Freezing THOSE would defeat the
reason this harness exists, in
the same way re-fetching the live web would.

Score with ``pipeline.quality``'s public functions where a research note
actually maps onto their contract, and name the mapping rather than
reimplementing the math (DRY; coding-standards.md rule 6 -- no re-derived
schema/scoring logic that could drift from the real one). Every one of
``quality``'s functions expects a composed ``doc`` -- ``{"slides": [...]}`` --
and a research note is not a slide. But a single note wraps cleanly as ONE
minimal slide (see ``_note_prose_doc``/``_note_code_doc``), which is legitimate
reuse through the module's real public contract, not reaching into its
private helpers:

  - ``grounding_ratio(note_as_doc, corpus)`` -- does the note's own language
    trace back to the exact web excerpts it was given? This is the note-
    level analogue of what ``eval_compose.py`` measures deck-to-brief; see
    the D14 caveat below before comparing the two numbers.
  - ``numeric_density`` / ``proper_noun_density`` -- is the note specific?
  - ``code_preserved(note_as_code_doc, corpus)`` -- RESEARCH_SYSTEM tells the
    model to copy a fenced artifact into ``detail`` VERBATIM; this checks
    whether it actually did, against the real corpus.
  - NOT used: ``bare_bullet_ratio``, ``hook_similarity``, ``substance_composite``.
    All three are deck-shaped ideas with no note-level meaning (a bare-
    bullet judgement needs several slide TYPES to compare against; a hook
    is a compose-stage concept; a composite would need placeholder values
    for the inapplicable terms, which is exactly the fabricated-number
    trap D23 warns about). Forcing them in would manufacture a number, not
    measure one.

Research-specific metrics that ``pipeline.quality`` has no equivalent for,
because they are direct measures of things named in project-context.md:

  - notes-per-facet **yield** -- the direct measure of the 209
    "researcher N: every source rejected as irrelevant" events in
    ``data/pipeline.log``.
  - relevance-gate **accept rate** -- how often ``_is_relevant`` admits a
    source.
  - **confidence mix** -- the distribution of ``note["confidence"]``.
  - **failure-phrase rate** -- a note reading "the excerpts do not contain
    information" is stored and counted toward ``MIN_NOTES`` today, exactly
    like a real finding. It is a non-answer occupying a slot. ``FAILURE_PHRASE_RE``
    is a mechanical detector, matched against real examples already in this
    codebase's own tests (see its docstring).

D23: never report a bare composite -- there isn't one here to report, by
design (see above). Report raw per-metric values, and name this file's
metric set (this docstring) alongside anything aggregated.

D14: ``grounding_ratio``'s denominator is deck-only in ``eval_compose.py``'s
use of it, so it is not valid across item sets there. Here the denominator
is the NOTE's own 3-grams against the CORPUS it was given -- a different
numerator and a different denominator. The two ``avg_grounding_ratio``
numbers (this file's summary vs. ``eval_compose.py --rescore``'s) are never
comparable to each other; only within this file's own runs.

Usage, mirroring ``eval_compose.py``'s CLI shape:

    ./.venv/bin/python scripts/eval_research.py                    # score current research stage
    ./.venv/bin/python scripts/eval_research.py --tag baseline     # save for comparison
    ./.venv/bin/python scripts/eval_research.py --compare baseline arm2
    ./.venv/bin/python scripts/eval_research.py --items 501,502    # a subset of the golden set
    ./.venv/bin/python scripts/eval_research.py --model X --temperature 0.7
    ./.venv/bin/python scripts/eval_research.py --reps 5           # B4: report this harness's
                                                                    # own noise floor before
                                                                    # trusting a single-run delta
    ./.venv/bin/python scripts/eval_research.py --no-plan-cache    # force every plan call live
                                                                    # (a future planner task)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.conversation import NeedsInput  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402
from pipeline.quality import (  # noqa: E402
    code_preserved,
    grounding_ratio,
    numeric_density,
    proper_noun_density,
)
from pipeline.stages.research import research  # noqa: E402

RESULTS = ROOT / "data" / "eval"
GOLDEN = RESULTS / "research_golden.json"
CACHE_DIR = RESULTS / "corpus_cache"

#: F2 (T73 bounce 2): eval_compose.py ALSO documents `--tag baseline` as its
#: first usage example and ALSO writes to `data/eval/{tag}.json` (same
#: RESULTS dir) -- so a research run and a compose run sharing a tag name
#: used to silently destroy whichever one was written second, and
#: `compare()` would then happily print a meaningless row against whatever
#: foreign-shaped file it found. Every filename this harness writes/reads
#: gets a "research-" prefix (see `_result_path`) so that collision cannot
#: happen going forward; this constant is the second, defense-in-depth
#: check `_load_harness_result` uses to refuse a file that reached the
#: prefixed path some other way (a manual copy, a stale pre-fix run).
HARNESS_NAME = "research"

#: llm.cheap()'s own default (llm.py). research.py never overrides it on any
#: of its calls (plan, relevance gate, note-writing), so this is the
#: temperature every research-stage call actually runs at unless --temperature
#: asks for a different one.
RESEARCH_TEMPERATURE = 0.3

#: Mechanical detector for a note that reports failure to find an answer
#: rather than an actual finding -- RESEARCH_SYSTEM's own fallback ("If the
#: excerpts do not answer the question, say so and set confidence low").
#: Stored today exactly like a real note and counted toward MIN_NOTES (the
#: 209-events log line in project-context.md), so a note occupying a slot
#: with no content is invisible unless something looks for the wording it is
#: written in. A regex, not one literal string: real examples already in
#: this codebase's own tests vary in phrasing --
#: tests/stages/test_research.py's
#: test_null_result_notes_do_not_count_as_support ("The provided web
#: excerpts do not contain information.") and
#: test_low_confidence_notes_are_excluded_when_real_findings_exist ("The
#: excerpts do not contain information about signs.").
FAILURE_PHRASE_RE = re.compile(
    r"(the\s+)?(provided\s+)?(web\s+)?excerpts?\s+(do|does)\s+not\s+"
    r"(contain|answer|provide|support)",
    re.IGNORECASE,
)

#: The exact literal research.py's _research_one() builds its note-writing
#: prompt with (see research.py: ``... "WEB EXCERPTS:\n\n" + "\n\n---\n\n".join(corpus)``).
#: Used to recover, from the note call's own recorded request, the exact
#: packed corpus text that call actually saw -- not a re-fetch, so this is
#: what the model was actually given, packing and all. If research.py ever
#: renames this heading, `_corpus_from_note_call` degrades to "" (grounding
#: 0.0, code_preserved None) rather than raising; test_failure_phrase_and_
#: corpus_marker_literals_still_match_research_py pins the literal so that
#: drift is caught loudly in CI instead of silently zeroing every run.
_EXCERPTS_MARKER = "WEB EXCERPTS:\n\n"


# --------------------------------------------------------------- HTTP cache


@dataclass
class CacheStats:
    """Hit/miss counters for BOTH caches this harness runs -- the HTTP page
    cache (``CachingHTTP``) and the plan-call cache (``Recorder``) -- kept
    on SEPARATE fields (T73 bounce 2 nit; previously pooled into one shared
    ``hits``/``misses`` pair). A plan-cache hit and an HTTP-cache hit answer
    different questions -- did we re-ask the model to plan queries, or did
    we re-fetch a page -- and a single shared counter could not tell "every
    page was a fresh fetch but the plan replayed" from the reverse.
    ``hits``/``misses`` below are HTTP-only; ``plan_hits``/``plan_misses``
    are the plan cache's own.
    """

    hits: int = 0
    misses: int = 0
    #: Recorder's plan-cache hits/misses -- see the class docstring above
    #: for why these are not folded into hits/misses.
    plan_hits: int = 0
    plan_misses: int = 0
    #: B2 (T73 bounce 1): how many freshly-fetched GETs came back non-2xx
    #: (a provider bot-block, a Cloudflare challenge page, a rate limit --
    #: infrastructure noise, not content) and were therefore NOT written to
    #: ``CachingHTTP``'s disk cache. Printed in the run summary so this is
    #: visible rather than a silent count buried in per-item output.
    non_2xx: int = 0


def _cache_key(url: str, params: dict | None) -> str:
    payload = json.dumps({"url": url, "params": params or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class _ReplayResponse:
    """A frozen HTTP response replayed from disk.

    Exposes exactly the surface search.py/research.py actually read off a
    real ``httpx.Response``: ``.status_code``, ``.text``, ``.json()``,
    ``.content``. Nothing else -- headers are not read by any call site this
    harness drives (``fetch_text``, ``searx``, ``image_search``,
    ``download_image``), so they are deliberately not modeled.
    """

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class CachingHTTP:
    """Wraps a real async HTTP client and memoizes every SUCCESSFUL (2xx)
    GET to disk by ``(url, params)``, so a second run against the same
    cache directory makes ZERO network calls for anything already fetched
    successfully. This is the mechanism that makes "search and fetch are
    deterministic" true regardless of what the live web or SearXNG's index
    looks like at the moment the run actually happens (see the module
    docstring).

    B2 (T73 bounce 1): a non-2xx response (a provider's bot-block, a
    Cloudflare interstitial, a rate limit -- infrastructure noise, not
    content) used to be cached exactly like a real page, because only a
    RAISED exception skipped the write. An audit of a shipped cache found
    ~19% of its entries were a permanently-replayed infrastructure failure
    served forever as a "hit" while the run reported zero misses -- the
    corpus, and every metric computed from it, was silently missing that
    fraction of its content on every subsequent "cached" run. Only a 2xx
    body is written now; anything else is never cached, so a later run gets
    a fresh chance at it, and ``CacheStats.non_2xx`` counts how often this
    happens so it is visible in the summary rather than silent.

    **Known limitation, recorded not fixed (open since bounce 0, T73 bounce
    2).** A cache entry -- 2xx or otherwise -- carries no URL of its own:
    the filename is a content hash of ``(url, params)`` and the cached JSON
    body never repeats them. So ``non_2xx`` says HOW MANY sources failed on
    a given run, never WHICH ones -- recovering "the 9 missing sources"'
    identity means re-deriving it from that run's own stdout (or
    re-instrumenting the write path), not reading it back off disk later.
    No AC has asked for a URL-keyed manifest, and doing it well is bigger
    than a nit (T73 bounce 2 also declined a body-size cap on this same
    class for the same reason -- noted, not built).

    GET only. Research's whole HTTP surface (``searx``, ``fetch_text``,
    ``image_search``, ``download_image`` -- all in ``search.py``) is GET;
    LLM traffic goes through ``LLMClient``'s own, separate, uncached
    ``http`` instance. A POST reaching this class would be a research-stage
    change this harness does not yet know about, so it fails loudly rather
    than silently bypassing the cache.

    The cache key deliberately ignores headers/timeout/follow_redirects: no
    call site in search.py varies those per item, so they cannot be the
    reason two requests to the same ``(url, params)`` should be treated as
    different. Documented here as a simplifying assumption, not an oversight.
    """

    def __init__(self, inner: Any, cache_dir: Path, stats: CacheStats) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.stats = stats
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str, params: dict | None) -> Path:
        return self.cache_dir / f"http-{_cache_key(url, params)}.json"

    async def get(self, url: str, params: dict | None = None, **kwargs: Any) -> Any:
        path = self._path(url, params)
        if path.exists():
            self.stats.hits += 1
            cached = json.loads(path.read_text())
            return _ReplayResponse(
                cached["status_code"], base64.b64decode(cached["body_b64"])
            )

        self.stats.misses += 1
        response = await self.inner.get(url, params=params, **kwargs)
        if 200 <= response.status_code < 300:
            path.write_text(json.dumps({
                "status_code": response.status_code,
                "body_b64": base64.b64encode(response.content).decode("ascii"),
            }))
        else:
            self.stats.non_2xx += 1
        return response

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "CachingHTTP only wraps GET -- research.py's HTTP surface "
            "(search.py: searx/fetch_text/image_search/download_image) is "
            "GET-only. LLM POST traffic never goes through this client "
            "(run() gives LLMClient the raw, uncached http instance)."
        )


# ------------------------------------------------------------ LLM recorder


@dataclass
class RecordedCall:
    role: str
    schema: dict | None
    request_user: str
    response: Any


def _schema_kind(schema: dict | None) -> str:
    """Which research-stage schema this call used, from its property set --
    PLAN_SCHEMA/RELEVANCE_SCHEMA/NOTE_SCHEMA (research.py) and
    COVERAGE_SCHEMA (pipeline.coverage) each have a distinct, non-overlapping
    signature. Classifying by shape, not by import identity, means this
    keeps working even if a caller builds an equivalent schema dict fresh
    (as tests routinely do)."""
    props = set((schema or {}).get("properties", {}))
    if {"entity", "queries"} <= props:
        return "plan"
    if "relevant" in props:
        return "relevance"
    if {"claim", "confidence"} <= props:
        return "note"
    if "uncovered" in props:
        return "coverage"
    return "other"


def _call_kind(call: RecordedCall) -> str:
    return _schema_kind(call.schema)


class Recorder:
    """Wraps an LLM client's ``cheap`` AND ``good`` roles. research.py calls
    only ``cheap`` today, but T71 ("route the research note to `good`") is
    the very task this harness exists to gate -- a note call that reached
    ``good`` and fell through unrecorded would silently zero out
    ``notes_produced`` for that arm while raising nothing (B1, T73 bounce 1).
    Every call is logged for scoring with the role it actually used
    (``RecordedCall.role``), and the query-PLAN call specifically is served
    from / written to a small disk cache keyed by the exact request text
    (``sha256(system + user)``) -- see the module docstring's "why the plan
    is frozen too". Every other call (relevance gate, note-writing, clause
    coverage), on whichever role carries it, is always live.

    Reviewer nit (T73 bounce 1): the plan cache used to be keyed by the
    CALLER-supplied ``(item.id, fingerprint)`` rather than by what was
    actually asked. Two problems followed from that: (1) editing an item's
    content in ``research_golden.json`` without also changing its ``id``
    would silently replay a plan cached for the item's OLD content --
    ``_item_context(item)`` (research.py) is what actually varies the
    plan's user text, and the old key never saw it; (2) ``item.id`` was
    interpolated unsanitised into a filename the caller built (a security
    nit on the same finding). Hashing the literal request text closes both:
    the cache key IS the content, so it can never go stale under a
    changed-but-same-id item, and no caller-supplied string reaches a path
    at all.
    """

    def __init__(
        self, inner: Any, *, plan_cache_dir: Path | None = None,
        stats: CacheStats | None = None, temperature: float | None = None,
    ) -> None:
        self.inner = inner
        self.plan_cache_dir = plan_cache_dir
        if self.plan_cache_dir is not None:
            self.plan_cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats = stats
        self.temperature = temperature
        self.calls: list[RecordedCall] = []

    async def cheap(self, system: str, user: str, schema: dict | None = None,
                    **kwargs: Any) -> Any:
        return await self._call("cheap", system, user, schema, **kwargs)

    async def good(self, system: str, user: str, schema: dict | None = None,
                   **kwargs: Any) -> Any:
        return await self._call("good", system, user, schema, **kwargs)

    def _plan_cache_path(self, system: str, user: str) -> Path:
        key = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()[:24]
        return self.plan_cache_dir / f"plan-{key}.json"

    async def _call(self, role: str, system: str, user: str,
                    schema: dict | None, **kwargs: Any) -> Any:
        """Shared recording path for both roles -- one place that decides
        "did this call happen, what role served it, what did it return" so
        ``cheap``/``good`` cannot drift into recording differently (the
        literal B1 bug: ``good`` had no path here at all)."""
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        kind = _schema_kind(schema)
        plan_path = (self._plan_cache_path(system, user)
                     if kind == "plan" and self.plan_cache_dir is not None else None)

        if plan_path is not None and plan_path.exists():
            response = json.loads(plan_path.read_text())
            if self.stats is not None:
                self.stats.plan_hits += 1
            self.calls.append(RecordedCall(role, schema, user, response))
            return response

        try:
            response = await getattr(self.inner, role)(system, user, schema=schema, **kwargs)
        except Exception as exc:
            self.calls.append(RecordedCall(role, schema, user, exc))
            raise

        if kind == "plan":
            if self.stats is not None:
                self.stats.plan_misses += 1
            if plan_path is not None:
                plan_path.write_text(json.dumps(response))
        self.calls.append(RecordedCall(role, schema, user, response))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# ------------------------------------------------------------ note scoring


def _note_prose_doc(note: dict) -> dict:
    """Wrap one research note as a minimal ``doc`` so ``pipeline.quality``'s
    mechanical metrics -- built for a composed deck's ``{"slides": [...]}``
    shape -- can be reused through their real public contract instead of
    reimplemented. A ``point`` slide's ``headline``/``sub`` fields are
    exactly ``claim``/``detail`` (see quality.py's ``_slide_text``, which
    reads both)."""
    return {"slides": [{"type": "point", "headline": note.get("claim", ""),
                         "sub": note.get("detail", "")}]}


def _note_code_doc(note: dict) -> dict:
    """Wrap one note's ``detail`` as a ``code`` slide so ``code_preserved`` --
    built to check whether a brief's fenced block survived into a composed
    ``code`` slide -- instead checks whether a fenced block PRESENT IN THE
    CORPUS survived verbatim into the note (RESEARCH_SYSTEM's own
    instruction: "copy it into detail VERBATIM inside a fenced block")."""
    return {"slides": [{"type": "code", "code": note.get("detail", "")}]}


def _corpus_from_note_call(call: RecordedCall) -> str:
    """The exact packed web corpus text ``_research_one`` handed the model
    for this note call, recovered from the call's own recorded request --
    not re-fetched, so this is exactly what the model actually saw."""
    _, _, tail = call.request_user.partition(_EXCERPTS_MARKER)
    return tail


def _score_note(call: RecordedCall) -> dict:
    note = call.response
    corpus = _corpus_from_note_call(call)
    prose_doc = _note_prose_doc(note)
    return {
        "confidence": str(note.get("confidence", "")),
        "is_failure_phrase": bool(FAILURE_PHRASE_RE.search(
            f"{note.get('claim', '')} {note.get('detail', '')}")),
        "numeric_density": numeric_density(prose_doc),
        "proper_noun_density": proper_noun_density(prose_doc),
        "grounding_ratio": grounding_ratio(prose_doc, corpus),
        "code_preserved": code_preserved(_note_code_doc(note), corpus),
    }


def score_item(item: Any, calls: list[RecordedCall]) -> dict:
    """Mechanical research-stage signals for one item, derived entirely from
    the recorded LLM call log -- robust to however research() ended (a full
    note set, a Retryable on too few notes, a NeedsInput on an uncovered
    clause), because it reads what was actually asked and answered along the
    way rather than depending on research()'s return value or which
    exception it raised.
    """
    note_calls = [c for c in calls if _call_kind(c) == "note" and isinstance(c.response, dict)]
    relevance_calls = [c for c in calls if _call_kind(c) == "relevance" and isinstance(c.response, dict)]
    plan_calls = [c for c in calls if _call_kind(c) == "plan" and isinstance(c.response, dict)]

    queries_planned = len(plan_calls[0].response.get("queries") or []) if plan_calls else 0
    gate_accepted = sum(1 for c in relevance_calls if c.response.get("relevant"))
    gate_checked = len(relevance_calls)

    note_scores = [_score_note(c) for c in note_calls]
    confidence_mix = dict(Counter(n["confidence"] for n in note_scores))
    failure_count = sum(1 for n in note_scores if n["is_failure_phrase"])

    def _avg(key: str) -> float | None:
        values = [n[key] for n in note_scores if isinstance(n.get(key), (int, float))]
        return round(statistics.mean(values), 4) if values else None

    code_applicable = [n["code_preserved"] for n in note_scores if n["code_preserved"] is not None]
    # Kept as separate ints (not just the formatted string below) so
    # summarise() can sum them across rows directly instead of re-parsing a
    # display string -- see summarise()'s "reviewer nit: code_preserved_rate
    # never reached the overall summary" fix.
    code_preserved_true = sum(code_applicable)
    code_preserved_applicable = len(code_applicable)

    return {
        "id": item.id,
        "queries_planned": queries_planned,
        "notes_produced": len(note_calls),
        "yield": round(len(note_calls) / queries_planned, 4) if queries_planned else None,
        "gate_accepted": gate_accepted,
        "gate_checked": gate_checked,
        "gate_accept_rate": round(gate_accepted / gate_checked, 4) if gate_checked else None,
        "confidence_mix": confidence_mix,
        # F1 (T73 bounce 2): T71 ("route the note call to `good`") is a CODE
        # change (research.py: llm.cheap -> llm.good), not a Settings one --
        # baseline and arm run the identical config, so prompt_fingerprint()
        # is byte-identical on both sides by construction (D87) and the two
        # saved artifacts were otherwise provenance-indistinguishable. This
        # records which role ACTUALLY served each note call, straight from
        # RecordedCall.role, so the saved row itself -- not just the
        # fingerprint -- can tell a good-routed arm from a cheap baseline.
        "note_roles": dict(Counter(c.role for c in note_calls)),
        "failure_phrase_count": failure_count,
        "failure_phrase_rate": (round(failure_count / len(note_calls), 4)
                                 if note_calls else None),
        "avg_numeric_density": _avg("numeric_density"),
        "avg_proper_noun_density": _avg("proper_noun_density"),
        "avg_grounding_ratio": _avg("grounding_ratio"),
        "code_preserved_rate": (f"{code_preserved_true}/{code_preserved_applicable}"
                                 if code_applicable
                                 else "0/0 (no fenced block in any corpus)"),
        "code_preserved_true": code_preserved_true,
        "code_preserved_applicable": code_preserved_applicable,
    }


async def score_one_item(item: Any, llm: Recorder, http: Any, settings: Settings) -> dict:
    """Run the real ``research()`` stage for one item and reduce the
    recorded calls to this item's metrics. Nothing about the pipeline is
    reimplemented here, only observed (``Recorder``) and made deterministic
    at the HTTP/plan boundary (wired in by the caller) -- the same
    ``research()`` entrypoint production calls.
    """
    t0 = time.perf_counter()
    error = None
    try:
        await research(item, llm, http, settings)
    except NeedsInput as exc:
        error = f"NeedsInput: gaps={exc.fields.get('gaps') or []}"
    except Exception as exc:  # noqa: BLE001 -- one bad item must not kill the
        # whole batch; same intentional per-item boundary eval_compose.py's
        # own run() uses (research()/its dependencies raise many distinct
        # exception types -- Retryable, Retryforever, NeedsInput, a bare
        # schema-validation error -- and scoring must survive all of them).
        error = f"{type(exc).__name__}: {exc}"
    row = score_item(item, llm.calls)
    row["error"] = error
    row["seconds"] = round(time.perf_counter() - t0, 2)
    return row


# --------------------------------------------------------------- run/report


def summarise(rows: list[dict], stats: CacheStats, fingerprint: str, set_version: Any) -> dict:
    total_queries = sum(r["queries_planned"] for r in rows)
    total_notes = sum(r["notes_produced"] for r in rows)
    total_gate_checked = sum(r["gate_checked"] for r in rows)
    total_gate_accepted = sum(r["gate_accepted"] for r in rows)
    total_failures = sum(r["failure_phrase_count"] for r in rows)
    # Reviewer nit (T73 bounce 1): score_item's per-row code_preserved_rate
    # never rolled up into the overall summary -- .get() with a 0 default so
    # a row built by an older code path (or a hand-built test row) without
    # these keys still aggregates cleanly rather than raising.
    total_code_true = sum(r.get("code_preserved_true", 0) for r in rows)
    total_code_applicable = sum(r.get("code_preserved_applicable", 0) for r in rows)

    confidence_totals: Counter = Counter()
    # F1 (T73 bounce 2): rolls score_item's per-row note_roles up to the
    # summary, same shape as confidence_mix_overall below -- .get() with a
    # {} default so an older-shaped row still aggregates cleanly.
    note_role_totals: Counter = Counter()
    for r in rows:
        confidence_totals.update(r["confidence_mix"])
        note_role_totals.update(r.get("note_roles", {}))

    def _avg(key: str) -> float | None:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(statistics.mean(values), 4) if values else None

    return {
        # F2 (T73 bounce 2): identifies which harness wrote this saved
        # artifact -- eval_compose.py's summarise() carries no such key, so
        # compare() (below) can refuse a foreign file by name instead of
        # printing a meaningless row against it. See HARNESS_NAME.
        "harness": HARNESS_NAME,
        "items": len(rows),
        "items_completed": sum(1 for r in rows if not r.get("error")),
        "prompt": fingerprint,
        # Renamed from "set_version" (T73 bounce 2 nit): this is the GOLDEN
        # SET's own curation version (research_golden.json's "set_version"
        # key, via load_golden()) -- unrelated to pipeline.quality's
        # METRIC_SET_VERSION/"metric_set_version" (D15/D23), which this key
        # sat next to in every printed summary and reads like a match for.
        "golden_set_version": set_version,
        "queries_planned_total": total_queries,
        "notes_produced_total": total_notes,
        "yield_overall": round(total_notes / total_queries, 4) if total_queries else None,
        "gate_accept_rate_overall": (round(total_gate_accepted / total_gate_checked, 4)
                                      if total_gate_checked else None),
        "confidence_mix_overall": dict(confidence_totals),
        # F1 (T73 bounce 2): which role (cheap/good) actually served each
        # note call, summed across every item -- see score_item's note_roles.
        "note_roles_overall": dict(note_role_totals),
        "failure_phrase_rate_overall": (round(total_failures / total_notes, 4)
                                         if total_notes else None),
        "avg_numeric_density": _avg("avg_numeric_density"),
        "avg_proper_noun_density": _avg("avg_proper_noun_density"),
        # D14: comparable within THIS file's runs only -- see the module
        # docstring. Never compare this to eval_compose.py's grounding_ratio.
        "avg_grounding_ratio": _avg("avg_grounding_ratio"),
        # T73 bounce 2 nit: D14's caveat lived only in the module docstring
        # and the on-screen print -- absent from the SAVED artifact, so a
        # reader loading an old data/eval/research-*.json later (via
        # --compare, long after the original run's stdout scrolled away)
        # never saw it. Same string both places, one source (see
        # GROUNDING_COMPARABILITY_CAVEAT below).
        "grounding_comparability_caveat": GROUNDING_COMPARABILITY_CAVEAT,
        "code_preserved_rate_overall": (f"{total_code_true}/{total_code_applicable}"
                                         if total_code_applicable
                                         else "0/0 (no fenced block in any corpus)"),
        "cache_hits": stats.hits,
        "cache_misses": stats.misses,
        # T73 bounce 2 nit: previously pooled with the HTTP cache above into
        # one shared counter -- see CacheStats' docstring for why they are
        # now tracked (and reported) separately.
        "plan_cache_hits": stats.plan_hits,
        "plan_cache_misses": stats.plan_misses,
        # B2 (T73 bounce 1): how many freshly-fetched responses were non-2xx
        # and therefore never cached -- see CachingHTTP's docstring. Printed
        # so an infrastructure failure (bot-block/Cloudflare/rate-limit) is
        # visible in the summary instead of a silent gap in the corpus.
        "non_2xx_responses": stats.non_2xx,
    }


def prompt_fingerprint(settings: Settings, *, model: str | None = None,
                        good_model: str | None = None,
                        temperature: float = RESEARCH_TEMPERATURE) -> str:
    """Identify the research-stage config a run scored.

    Mirrors ``eval_compose.py``'s ``prompt_fingerprint``, corrected for two
    findings logged against it (D6, D56): hashing only prompt text let two
    materially different runs share a fingerprint, because the model slug --
    and here, temperature -- is exactly what a research-stage arm actually
    varies, and neither was in the hash. Both are folded into the returned
    string explicitly rather than assumed matched.

    B3 (T73 bounce 1): the fingerprint used to fold in only the ``cheap``
    role's model, because that is the only role research.py calls today.
    T71 ("route the research note to `good`") is exactly the kind of change
    this harness exists to gate, and it would have shared a fingerprint with
    a `cheap`-only baseline even though a different model was now serving
    the note call -- the same D6/D56 defect closed for model+temperature,
    reopened for role. Both roles' effective models are folded in below so
    a config change on EITHER one changes the fingerprint, without this
    file needing to know in advance which role a future task routes a given
    call through.
    """
    from pipeline.coverage import COVERAGE_SYSTEM
    from pipeline.stages.research import PLAN_SYSTEM, RELEVANCE_SYSTEM, RESEARCH_SYSTEM

    if model is None:
        model = _effective_model(settings, role="cheap")
    if good_model is None:
        good_model = _effective_model(settings, role="good")
    digest = hashlib.sha256(
        f"{PLAN_SYSTEM}\n{RELEVANCE_SYSTEM}\n{RESEARCH_SYSTEM}\n{COVERAGE_SYSTEM}".encode()
    ).hexdigest()[:12]
    return f"{digest}+cheap:{model}+good:{good_model}+temp:{temperature}"


def _effective_model(settings: Settings, role: str = "cheap") -> str:
    """The model that will actually serve a given role's ``llm`` calls,
    given the configured provider (mirrors ``LLMClient._model``'s own
    role -> slug mapping). Defaults to "cheap" -- research.py's calls
    today -- but ``prompt_fingerprint`` also asks for "good" (B3) so a
    config change on either role a research-stage call might be routed
    through is visible in the fingerprint."""
    s = settings
    if s.llm_provider == "openrouter":
        return {"cheap": s.openrouter_model_cheap, "good": s.openrouter_model_good}[role]
    return {"cheap": s.model_cheap, "good": s.model_good}[role]


def _with_model(settings: Settings, model: str) -> Settings:
    field_name = "openrouter_model_cheap" if settings.llm_provider == "openrouter" else "model_cheap"
    return replace(settings, **{field_name: model})


def _safe_filename_component(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", text)


def _result_path(tag: str) -> Path:
    """Where one saved run for ``tag`` lives -- always ``research-``
    prefixed (F2, T73 bounce 2) so this harness can never collide with
    eval_compose.py's ``{tag}.json`` in the same ``RESULTS`` directory, and
    always run through ``_safe_filename_component`` (nit, T73 bounce 2:
    ``--tag``/``--compare`` used to reach a path as a raw, unsanitised CLI
    string while this exact helper sat unused for it)."""
    return RESULTS / f"research-{_safe_filename_component(tag)}.json"


def _load_harness_result(tag: str) -> dict:
    """Load one saved run's summary by tag, refusing a file this harness
    did not write (F2, T73 bounce 2). Reproduced live pre-fix: a
    compose-shaped ``baseline.json`` (no ``"harness"`` key -- that is
    eval_compose.py's real ``summarise()`` shape) reached ``compare()`` and
    printed one meaningless row (``set_version 1 1``), raising nothing.
    The ``research-`` filename prefix (``_result_path``) is what actually
    PREVENTS the collision; this check is the second, defense-in-depth
    layer for a foreign file that reaches the prefixed path some other way
    (a manual copy, a stale pre-fix artifact)."""
    path = _result_path(tag)
    summary = json.loads(path.read_text())["summary"]
    harness = summary.get("harness")
    if harness != HARNESS_NAME:
        raise ValueError(
            f"{path.name} was not produced by this harness (harness="
            f"{harness!r}, expected {HARNESS_NAME!r}) -- refusing to "
            f"compare a foreign eval file"
        )
    return summary


def load_golden(only: list[int] | None = None) -> tuple[list, dict[int, str], int]:
    """Rebuild items at PRE-RESEARCH state (``Status.EXTRACTED``) from the
    frozen snapshot, mirroring ``eval_compose.py``'s ``load_golden()`` -- see
    that function's docstring and D2 for why a frozen snapshot, not a live
    db, is what makes two runs comparable. There is no live production
    database in this dev environment (``data/`` is entirely gitignored
    runtime state), so ``data/eval/research_golden.json`` is hand-curated
    rather than snapshotted from one -- see that file's own ``"curation"``
    key and this task's handoff for what that means for the numbers below.
    """
    from pipeline.models import Item, Status

    data = json.loads(GOLDEN.read_text())
    items, reasons = [], {}
    for case in data["cases"]:
        fields = dict(case["item"])
        if only and fields["id"] not in only:
            continue
        item = Item(status=Status.EXTRACTED, **fields)
        reasons[item.id] = case["failure_mode"]
        items.append(item)
    return items, reasons, data["set_version"]


def _apply_model_config_guard(settings: Settings) -> None:
    """Call ``settings.validate_model_config()`` if this ``Settings`` object
    has one.

    TODO(T85): a devops branch (in review as of this task, per a mid-task
    coordinator note) splits ``validate_for_run()`` into
    ``validate_model_config()`` -- provider validity, API key presence, the
    T69 auto-router ban, the T28 paid-model ban, and NOTHING Telegram/
    publish-related -- specifically so a no-credentials eval script like
    this one can run the guard T69 exists to enforce. D16 (project-
    context.md): ``eval_compose.py``, ``smoke.py``, ``build_golden.py`` and
    ``dashboard.py`` all construct ``Settings`` and call a model with NO
    guard today; this script would have been a fifth unguarded entry point.

    ``validate_for_run()`` is deliberately NOT called here as a stand-in --
    it also requires ``TELEGRAM_API_ID``/``_API_HASH``/``_BOT_TOKEN``/
    ``OPERATOR_USER_ID``/``CHANNEL_IDS``, which this harness has no need for
    and, in a bare dev/test environment (this one has no ``.env`` at all),
    no way to supply. Calling it would make every invocation of a research-
    scoring script fail on missing Telegram credentials that have nothing
    to do with research scoring -- inventing that partial substitute is
    exactly what was asked not to do; one guard definition, not two.

    Dispatched via ``getattr`` rather than a version/import check, so this
    starts enforcing the guard automatically the moment T85 lands
    ``validate_model_config`` on ``Settings``, with no further change
    needed at this call site.
    """
    guard = getattr(settings, "validate_model_config", None)
    if guard is not None:
        guard()


#: D14 (project-context.md): this file's ``avg_grounding_ratio`` is a
#: NOTE-level measurement (a note's own language against the corpus IT was
#: given) -- a different numerator AND denominator from eval_compose.py's
#: DECK-level ``grounding_ratio``, so the two numbers are never comparable
#: to each other, only within this file's own runs. Reviewed and found
#: present in the docstring but absent from the actual printed output (T73
#: bounce 1) -- T125 (D79) has since measured deck-level content slides at
#: 0.221-0.275 and deck-level overall at 0.16-0.18; a reader seeing this
#: file's own baseline (~0.52) next to either of those with no caveat in
#: the output itself would conclude research notes are far better grounded
#: than composed decks, which these two differently-defined ratios cannot
#: support. Printed with every summary now, not just documented.
GROUNDING_COMPARABILITY_CAVEAT = (
    "  NOTE (D14): avg_grounding_ratio above is NOTE-level (a note's own\n"
    "  language vs. the corpus IT was given) -- NOT the same measurement as\n"
    "  eval_compose.py's DECK-level grounding_ratio (T125/D79 measured deck\n"
    "  content slides at 0.221-0.275, deck overall at 0.16-0.18). Do not\n"
    "  compare this number to that one; only compare this file's runs to\n"
    "  each other."
)


def _spread(values: list[Any]) -> dict:
    """Min/max/mean/stdev across repeated ``--reps`` runs against the SAME
    frozen corpus (B4, T73 bounce 1) -- the harness's own reported noise
    floor. Repeating identical config against a cache that never changes on
    the HTTP/plan side isolates live-model non-determinism (the relevance
    gate and note-writing calls, which always stay live -- see the module
    docstring) as the only thing that can move these numbers between reps,
    so the spread reported here is a direct answer to "how much of a change
    between two runs could just be noise."
    """
    clean = [v for v in values if isinstance(v, (int, float))]
    if not clean:
        return {"n": 0, "min": None, "max": None, "mean": None, "stdev": None}
    return {
        "n": len(clean),
        "min": round(min(clean), 4),
        "max": round(max(clean), 4),
        "mean": round(statistics.mean(clean), 4),
        "stdev": round(statistics.stdev(clean), 4) if len(clean) > 1 else 0.0,
    }


#: Aggregate summary keys B4's noise-floor report covers -- the exact three
#: the task named (yield, gate rate, failure-phrase rate) plus
#: items_completed, since T73 bounce 1's own review flagged item 502's
#: NeedsInput/complete flip as a structural (not incidental) source of
#: noise in that count specifically.
NOISE_FLOOR_KEYS = (
    "yield_overall", "gate_accept_rate_overall",
    "failure_phrase_rate_overall", "items_completed",
)


def _plan_cache_dir_for(fingerprint: str, use_plan_cache: bool) -> Path | None:
    """The plan-cache directory for one run's config -- namespaced by
    ``fingerprint`` (model + temperature + prompt digest), never a single
    fixed directory. F4 (T73 bounce 2 mutation survivor): a mutant that
    dropped the fingerprint from this path left every test in the suite
    green while silently making every config replay whichever config
    wrote the FIRST plan cache entry forever, since ``Recorder``'s own
    cache key is the request TEXT alone, not the config -- collapsing
    exactly the per-config isolation ``Recorder``'s docstring says this
    directory exists to provide. Extracted to its own function so that
    isolation is asserted directly rather than only implied by
    ``_score_all_items``'s wiring. ``use_plan_cache=False`` (``--no-plan-
    cache``) always returns ``None`` regardless of fingerprint.
    """
    if not use_plan_cache:
        return None
    return CACHE_DIR / f"plan-cache-{_safe_filename_component(fingerprint)}"


async def _score_all_items(
    items: list, reasons: dict[int, str], settings: Settings,
    effective_temp: float, fingerprint: str, stats: CacheStats,
    use_plan_cache: bool = True,
) -> list[dict]:
    """One full pass over the golden set -- the unit ``--reps`` repeats.

    The plan cache directory is namespaced by ``fingerprint`` (model +
    temperature + prompt digest), so two configs never share a cached plan
    even though ``Recorder`` now keys the FILE within that directory by the
    request content itself (``sha256(system+user)``, not ``item.id`` --
    see ``Recorder``'s docstring for why). ``--no-plan-cache`` (a future
    planner task's escape hatch, reviewer nit) forces every plan call live
    by passing no directory at all.
    """
    rows = []
    plan_cache_dir = _plan_cache_dir_for(fingerprint, use_plan_cache)
    async with httpx.AsyncClient() as raw_http:
        http = CachingHTTP(raw_http, CACHE_DIR, stats)
        for item in items:
            llm = Recorder(LLMClient(settings, raw_http), plan_cache_dir=plan_cache_dir,
                           stats=stats, temperature=effective_temp)
            row = await score_one_item(item, llm, http, settings)
            row["why"] = reasons.get(item.id)
            print(f"  item {item.id:4}  queries={row['queries_planned']:2}  "
                  f"notes={row['notes_produced']:2}  yield={row['yield']}  "
                  f"gate={row['gate_accept_rate']}  fail_phrase={row['failure_phrase_rate']}  "
                  f"{row['seconds']:6.1f}s"
                  + (f"  ERROR: {row['error']}" if row["error"] else ""))
            rows.append(row)
    return rows


async def run(tag: str | None, only: list[int] | None = None,
              model: str | None = None, temperature: float | None = None,
              reps: int = 1, no_plan_cache: bool = False) -> None:
    settings = Settings.load()
    if model:
        settings = _with_model(settings, model)
    _apply_model_config_guard(settings)
    effective_temp = RESEARCH_TEMPERATURE if temperature is None else temperature
    effective_model = _effective_model(settings)
    fingerprint = prompt_fingerprint(settings, model=effective_model, temperature=effective_temp)

    items, reasons, set_version = load_golden(only)
    print(f"provider={settings.llm_provider}  cheap-model={effective_model[:52]}")
    print(f"prompt={fingerprint}  golden set v{set_version}")
    print(f"scoring {len(items)} frozen pre-research items"
          + (f" x {reps} reps" if reps > 1 else "")
          + (" (plan cache disabled)" if no_plan_cache else "") + "\n")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    for rep in range(1, reps + 1):
        if reps > 1:
            print(f"--- rep {rep}/{reps} ---")
        stats = CacheStats()
        rows = await _score_all_items(items, reasons, settings, effective_temp, fingerprint, stats,
                                       use_plan_cache=not no_plan_cache)

        summary = summarise(rows, stats, fingerprint, set_version)
        print("\n" + "=" * 58)
        for key, value in summary.items():
            print(f"  {key:28} {value}")
        print("\n" + GROUNDING_COMPARABILITY_CAVEAT)

        if reasons:
            print("\n  by failure mode:")
            for mode in sorted(set(reasons.values())):
                sub = [r for r in rows if r.get("why") == mode]
                ok = [r for r in sub if not r.get("error")]
                print(f"    {mode:28} {len(ok)}/{len(sub)} completed")

        if tag:
            RESULTS.mkdir(parents=True, exist_ok=True)
            rep_tag = f"{tag}-rep{rep}" if reps > 1 else tag
            result_path = _result_path(rep_tag)
            result_path.write_text(
                json.dumps({"summary": summary, "rows": rows}, indent=2)
            )
            print(f"\nsaved -> data/eval/{result_path.name}")

        summaries.append(summary)

    if reps > 1:
        print("\n" + "=" * 58)
        print(f"  noise floor across {reps} reps (frozen corpus + plan cache; "
              "relevance gate and note-writing stayed live and repeated):")
        for key in NOISE_FLOOR_KEYS:
            print(f"    {key:28} {_spread([s.get(key) for s in summaries])}")


def compare(a: str, b: str) -> None:
    left = _load_harness_result(a)
    right = _load_harness_result(b)
    print(f"{'metric':28} {a:>14} {b:>14}   change")
    print("-" * 70)
    for key in left:
        lv, rv = left[key], right.get(key)
        if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
            delta = rv - lv
            mark = "" if abs(delta) < 1e-9 else f"  {delta:+g}"
            print(f"{key:28} {lv:>14} {rv:>14}{mark}")
    # T73 bounce 2 nit: D14's caveat used to live only in the module
    # docstring and in run()'s own per-rep stdout -- a reader running
    # --compare later, against artifacts from a run they never watched,
    # never saw it. Now read back off the saved file itself (either side
    # carries the same string; either is sufficient).
    caveat = left.get("grounding_comparability_caveat") or right.get("grounding_comparability_caveat")
    if caveat:
        print("\n" + caveat)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--items", help="comma-separated golden-set ids")
    ap.add_argument("--model", help="override the cheap-role model slug for this run")
    ap.add_argument("--temperature", type=float,
                    help=f"override llm.cheap()'s default ({RESEARCH_TEMPERATURE}) for this run")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--reps", type=int, default=1,
                    help="repeat the scoring N times against the same frozen "
                         "corpus and report the spread (B4: this harness's own "
                         "noise floor for yield/gate-rate/failure-phrase-rate/"
                         "items_completed) -- only the live model calls "
                         "(relevance gate, note-writing) actually repeat")
    ap.add_argument("--no-plan-cache", action="store_true",
                    help="force every plan call live -- for a future task that "
                         "changes the planner itself and needs a fresh plan on "
                         "every rep instead of the frozen one (reviewer nit)")
    args = ap.parse_args()

    if args.compare:
        compare(*args.compare)
    else:
        if args.reps < 1:
            ap.error("--reps must be at least 1")
        only = [int(x) for x in args.items.split(",")] if args.items else None
        asyncio.run(run(args.tag, only, model=args.model, temperature=args.temperature,
                        reps=args.reps, no_plan_cache=args.no_plan_cache))
