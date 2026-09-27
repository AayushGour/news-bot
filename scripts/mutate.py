"""Break the code on purpose and check the tests notice.

A green suite proves the tests run, not that they would catch anything. The
check is to change the code so it is wrong and confirm something goes red.

This exists because doing that in ad-hoc shell gave a false pass three times in
one session, each looking exactly like a well-tested codebase:

  - a replacement string that never matched, because the source used double
    quotes and the command used single ones;
  - a multi-line replacement mangled by shell quoting before python saw it;
  - a reverted file whose stale __pycache__ outlived the revert, so the test
    ran against the mutant that was no longer on disk.

So every mutation here asserts it applied, every revert asserts the file came
back byte-identical, and bytecode is cleared around each run. A mutation that
cannot be applied is an error, never a pass.

    ./.venv/bin/python scripts/mutate.py                    # run every mutation
    ./.venv/bin/python scripts/mutate.py --only closing     # one, by name
    ./.venv/bin/python scripts/mutate.py --list
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    tests: str
    why: str


#: Each entry breaks one guarantee and names the test that must catch it.
MUTATIONS: list[Mutation] = [
    Mutation(
        "closing-slide", "src/pipeline/stages/compose.py",
        'if "follow" not in types:', 'if False:',
        "tests/stages/test_compose.py",
        "every deck must close on a follow slide",
    ),
    Mutation(
        "closing-attribution", "src/pipeline/stages/compose.py",
        "if not is_dm and source_urls and not any(", "if source_urls and not any(",
        "tests/stages/test_compose.py",
        "a DM has no channel to credit, so it gets no sources slide",
    ),
    Mutation(
        "hashtag-inline", "src/pipeline/stages/compose.py",
        "for tag in [*inline, *hashtags]:", "for tag in hashtags:",
        "tests/stages/test_compose.py",
        "tags written into the prose count against the platform limit",
    ),
    Mutation(
        "few-shot-scope", "src/pipeline/config.py",
        'return (intent or "news") == "list"', "return True",
        "tests/test_config.py tests/stages/test_compose.py",
        "list mode must not show examples to news items",
    ),
    Mutation(
        "free-models", "src/pipeline/config.py",
        'return slug.endswith(":free") or slug in FREE_MODEL_ALIASES', "return True",
        "tests/test_config.py",
        "a paid model must be refused at startup",
    ),
    Mutation(
        "auto-router", "src/pipeline/config.py",
        'return (slug or "").strip().lower().startswith(AUTO_ROUTER_PREFIX)',
        "return False",
        "tests/test_config.py",
        "openrouter/free must be refused at startup — it re-routes to a "
        "different upstream model on every call, so no run is reproducible",
    ),
    Mutation(
        "requeue-table", "src/pipeline/requeue.py",
        '(Status.EXTRACTED, {"triage_score": None, "triage_reason": None}),',
        '(Status.EXTRACTED, {"extracted": {}}),',
        "tests/test_requeue.py",
        "requeuing must clear the next stage's output, not the status's own",
    ),
    Mutation(
        "relevance-gate", "src/pipeline/stages/enumerate_items.py",
        "if not is_relevant(candidate, terms):", "if False:",
        "tests/stages/test_enumerate.py",
        "an off-subject candidate cannot be rescued by popularity",
    ),
    Mutation(
        "catalogue-index", "src/pipeline/stages/enumerate_items.py",
        '**({"engines": REPO_ENGINES} if repos', '**({"engines": REPO_ENGINES} if True',
        "tests/stages/test_enumerate.py",
        "a web enumeration must not be pinned to github",
    ),
    Mutation(
        "catalogue-scoring", "src/pipeline/stages/enumerate_items.py",
        "score_candidate(candidate, repos)", "score_candidate(candidate, True)",
        "tests/stages/test_enumerate.py",
        "repo signals reject good web pages and empty the set",
    ),
    Mutation(
        "search-parallel", "src/pipeline/search.py",
        "concurrency: int = 4", "concurrency: int = 1",
        "tests/test_search_many.py",
        "callers that pass no concurrency must still fan out",
    ),
    Mutation(
        "search-outage", "src/pipeline/search.py",
        "if isinstance(batch, Retryforever):", "if False:",
        "tests/test_search_many.py",
        "an outage must propagate, not flatten into no-results",
    ),
    Mutation(
        "expansion-scope", "src/pipeline/expand.py",
        'original = known.get(str(entry.get("query", "")).strip().lower())',
        'original = str(entry.get("query", "")).strip()',
        "tests/test_expand.py",
        "variants for a query nobody asked would search off topic",
    ),
    Mutation(
        "coverage-unknown", "src/pipeline/coverage.py",
        "match = known.get(str(entry).strip().lower())", "match = str(entry).strip()",
        "tests/test_coverage.py",
        "a paraphrased clause must not be able to halt an item",
    ),
    Mutation(
        "coverage-fail-open", "src/pipeline/coverage.py",
        'log.warning("coverage check unavailable (%s); treating as covered", exc)\n'
        "        return []",
        "raise",
        "tests/test_coverage.py",
        "a safety net that fails closed halts the pipeline on an outage",
    ),
    Mutation(
        "clause-gate-dm-only", "src/pipeline/stages/research.py",
        '    if item.source != "dm":\n        clauses = []',
        "    if False:\n        clauses = []",
        "tests/stages/test_research.py",
        "a channel post asks for nothing and must never be parked on clauses",
    ),
    Mutation(
        "ask-records-first", "src/pipeline/conversation.py",
        '    await db.add_message(item.id, "pipeline", question, "telegram")\n\n    try:',
        "    try:",
        "tests/test_conversation.py",
        "a question must reach the thread even when Telegram is down",
    ),
    Mutation(
        "ask-survives-outage", "src/pipeline/conversation.py",
        "    except Exception as exc:\n        # The item is already parked",
        "    except ZeroDivisionError as exc:\n        # The item is already parked",
        "tests/test_conversation.py",
        "a delivery failure must not escape and kill the worker pass",
    ),
    Mutation(
        "parked-work-carried", "src/pipeline/conversation.py",
        '        {**(fields or {}),',
        "        {",
        "tests/test_conversation.py",
        "research already paid for must survive being parked",
    ),
    Mutation(
        "clause-gate", "src/pipeline/stages/research.py",
        "if uncovered:", "if False:",
        "tests/stages/test_research.py",
        "a clause with no support must ask the operator",
    ),
    Mutation(
        "closing-trim", "src/pipeline/stages/compose.py",
        "    body = [slide for slide in slides if slide.get(\"type\") not in CLOSING_TYPES]",
        "    body = list(slides)",
        "tests/stages/test_compose.py",
        "trimming for a closing slide must not discard the links index",
    ),
    Mutation(
        "links-index", "src/pipeline/stages/compose.py",
        'if "links" in types:', "if True:",
        "tests/stages/test_compose.py",
        "an enumeration must carry an index the reader can screenshot",
    ),
    Mutation(
        "enumeration-clauses", "src/pipeline/stages/enumerate_items.py",
        "    if uncovered:\n        raise NeedsInput(\n            \"I found items",
        "    if False:\n        raise NeedsInput(\n            \"I found items",
        "tests/stages/test_enumerate.py",
        "an enumeration answering half the request must ask, not ship",
    ),
    Mutation(
        "web-extraction", "src/pipeline/stages/enumerate_items.py",
        "        notes = await _extract_things(kept, plan, item, llm, http)",
        "        notes = []",
        "tests/stages/test_enumerate.py",
        "a web enumeration lists things, not the pages describing them",
    ),
    Mutation(
        "word-boundary", "src/pipeline/stages/enumerate_items.py",
        "    return bool(prefix) and not any(prefix.endswith(n) for n in _NEGATING)",
        "    return True",
        "tests/stages/test_enumerate.py",
        "\"discontent\" must not satisfy a search for \"content\"",
    ),
    Mutation(
        "web-ranking", "src/pipeline/stages/enumerate_items.py",
        '    score = authority(candidate.get("url", ""))', "    score = 0",
        "tests/stages/test_enumerate.py",
        "web results must rank, not all tie at the same score",
    ),
    Mutation(
        "pending-guard", "src/pipeline/approval/bot.py",
        "    if text != SKIP and looks_like_a_new_request(text):",
        "    if False:",
        "tests/approval/test_bot.py",
        "a new request must not be stored as a caption",
    ),
    Mutation(
        "call-deadline", "src/pipeline/llm.py",
        '                    f"openrouter exceeded {CALL_DEADLINE_S}s"',
        '                    f"ignored"',
        "tests/test_llm.py",
        "a response that never finishes must be cut off",
    ),
    Mutation(
        "container-wait", "src/pipeline/publish/instagram.py",
        "    await _await_container(carousel_id, http, settings)",
        "    pass",
        "tests/publish/test_publish.py",
        "publishing a container still being built loses the post",
    ),
    Mutation(
        "container-error-terminal", "src/pipeline/publish/instagram.py",
        '        if state == "ERROR":',
        "        if False:",
        "tests/publish/test_publish.py",
        "a container Meta failed to build must not be retried forever",
    ),
    Mutation(
        "priority-ordering", "src/pipeline/db.py",
        "                 ORDER BY priority DESC, id LIMIT ?\"\"\",",
        "                 ORDER BY id LIMIT ?\"\"\",",
        "tests/test_db.py",
        "a bumped item must jump the queue",
    ),
    Mutation(
        "priority-tiebreak", "src/pipeline/db.py",
        "                 ORDER BY priority DESC, id LIMIT ?\"\"\",",
        "                 ORDER BY priority DESC, id DESC LIMIT ?\"\"\",",
        "tests/test_db.py",
        "items of equal priority run oldest first",
    ),
    Mutation(
        "priority-vs-backoff", "src/pipeline/db.py",
        "                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
        "                   AND (next_attempt_at IS NULL OR next_attempt_at > ?)",
        "tests/test_db.py",
        "priority must not override backoff and retry a failing item in a loop",
    ),
    Mutation(
        "manual-negative-ids", "src/pipeline/intake/manual.py",
        "        source_msg_id=lowest - 1,",
        "        source_msg_id=abs(lowest) + 1,",
        "tests/intake/test_manual.py",
        "hand-queued ids must never collide with a real Telegram message",
    ),
    Mutation(
        "manual-too-thin", "src/pipeline/intake/manual.py",
        "    if len(text) < MIN_REQUEST_CHARS:",
        "    if False:",
        "tests/intake/test_manual.py",
        "a bare prompt yields a confident post about whatever it stumbled on",
    ),
    Mutation(
        "dm-continuation", "src/pipeline/intake/bot_intake.py",
        "        previous = await db.continuable_dm(chat_id, CONTINUATION_WINDOW_S)",
        "        previous = None",
        "tests/intake/test_intake.py",
        "a split message must continue the previous item, not start a new one",
    ),
    Mutation(
        "dm-duplicate-first", "src/pipeline/intake/bot_intake.py",
        "    if await db.already_ingested(chat_id, msg_id):",
        "    if False:",
        "tests/intake/test_intake.py",
        "a redelivered message is a duplicate, not a continuation",
    ),
    Mutation(
        "caption-warning", "src/pipeline/intake/bot_intake.py",
        "    if caption and len(caption.strip()) >= TELEGRAM_CAPTION_LIMIT:",
        "    if False:",
        "tests/intake/test_intake.py",
        "a caption cut at Telegram's ceiling must say so",
    ),
    Mutation(
        "continuation-resets", "src/pipeline/db.py",
        "            (joined, text_hash(joined), Status.INGESTED.value, now_iso(), item_id),",
        "            (joined, text_hash(joined), Status.TRIAGED.value, now_iso(), item_id),",
        "tests/intake/test_intake.py",
        "stages that ran on half the request must run again",
    ),
    Mutation(
        "answer-routing", "src/pipeline/conversation.py",
        "    return waiting[0] if len(waiting) == 1 else None",
        "    return waiting[0]",
        "tests/test_conversation.py",
        "with several waiting, an unaddressed reply must ask, not take the first",
    ),
    Mutation(
        "answer-reply-to", "src/pipeline/conversation.py",
        "            if item.question_msg_id == replied:",
        "            if False:",
        "tests/test_conversation.py",
        "a swipe-reply must route to the item it replied to",
    ),
    Mutation(
        "question-msg-id", "src/pipeline/conversation.py",
        'await db.update_fields(item.id, {"question_msg_id": int(message_id)})',
        "pass",
        "tests/test_conversation.py",
        "the question's message id must be recorded or nothing can route",
    ),
    Mutation(
        "null-notes", "src/pipeline/stages/research.py",
        'for n in notes if n.get("confidence") != "low"', "for n in notes",
        "tests/stages/test_research.py",
        "a note reporting nothing found is not evidence of coverage",
    ),
    Mutation(
        "note-role-config", "src/pipeline/stages/research.py",
        "write_note = getattr(llm, settings.research_note_role)",
        "write_note = llm.cheap",
        "tests/stages/test_research.py",
        "T71: the note call must route through config, not a hardcoded role",
    ),
    Mutation(
        "gate-fail-open-fires", "src/pipeline/stages/research.py",
        "if not errored:", "if True:",
        "tests/stages/test_research.py",
        "B1 (bounce 1): a facet where every candidate genuinely ERRORED "
        "must fail open, not vanish",
    ),
    Mutation(
        "gate-fail-open-bounded", "src/pipeline/stages/research.py",
        "url, body = errored[0]", "url, body = errored[-1]",
        "tests/stages/test_research.py",
        "B1 (bounce 1): fail-open re-admits the TOP-ranked ERRORED source, "
        "not any other one",
    ),
    Mutation(
        "gate-fail-open-errored-only", "src/pipeline/stages/research.py",
        "elif verdict is _GateVerdict.ERRORED:",
        "elif verdict is _GateVerdict.REFUSED:",
        "tests/stages/test_research.py",
        "B1 (bounce 1, D88): only a gate call that returned NO verdict may "
        "ever fail open -- a source the gate explicitly refused must never "
        "be re-admitted",
    ),
    Mutation(
        "relevance-excerpt-dedicated-constant", "src/pipeline/stages/research.py",
        "    packed = pack([Source(id=url, text=body)], RELEVANCE_EXCERPT_CHARS)[0]",
        "    packed = pack([Source(id=url, text=body)], 1_000_000)[0]",
        "tests/stages/test_research.py",
        "B3 (bounce 1): the relevance excerpt must be capped at the small, "
        "dedicated, measured RELEVANCE_EXCERPT_CHARS -- not the whole "
        "note-writing corpus budget T72 borrowed it from",
    ),
    Mutation(
        "note-corpus-budget-follows-role", "src/pipeline/stages/research.py",
        'budget_chars = budget_chars_for_role(settings, role=settings.research_note_role)',
        'budget_chars = budget_chars_for_role(settings, role="cheap")',
        "tests/stages/test_research.py",
        "N1 (bounce 1, reviewer): a good-routed note call must get good's "
        "larger context budget, not one hardcoded to cheap -- a hardcoded "
        "cheap silently dampens T71's own effect whenever the role is good",
    ),
    Mutation(
        "relevance-gate-malformed-verdict-errors", "src/pipeline/stages/research.py",
        '    if not isinstance(verdict, dict) or "relevant" not in verdict:',
        "    if False:",
        "tests/stages/test_research.py",
        "N2/N3 (bounce 1, reviewer): a non-dict verdict or a dict missing "
        "'relevant' must come back ERRORED, not raise out of the function "
        "or get silently binned REFUSED",
    ),
    Mutation(
        "numeric-density", "src/pipeline/quality.py",
        "figures = sum(1 for w in words if any(c.isdigit() for c in w))",
        "figures = 0",
        "tests/test_quality.py",
        "a deck full of figures must not score the same as one with none",
    ),
    Mutation(
        "proper-noun-mid-sentence", "src/pipeline/quality.py",
        "elif i > 0 and tok[0].isupper() and not tok.isupper():",
        "elif i >= 0 and tok[0].isupper() and not tok.isupper():",
        "tests/test_quality.py",
        "every sentence starts capitalised -- counting position 0 measures "
        "sentence count, not named entities",
    ),
    Mutation(
        "grounding-ratio", "src/pipeline/quality.py",
        "return round(len(deck_grams & source_grams) / len(deck_grams), 4)",
        "return 0.0",
        "tests/test_quality.py",
        "grounding_ratio must actually distinguish sourced text from generic text",
    ),
    Mutation(
        "hook-similarity", "src/pipeline/quality.py",
        "best = max(best, len(a & b) / len(union))",
        "best = 0.0",
        "tests/test_quality.py",
        "a run of identical hooks must not score the same as a varied run",
    ),
    Mutation(
        "bare-bullet-ratio", "src/pipeline/quality.py",
        "bare = sum(1 for t in content if t not in RICH_SLIDE_TYPES)",
        "bare = sum(1 for t in content if t in RICH_SLIDE_TYPES)",
        "tests/test_quality.py",
        "an all-bullets deck must score higher bareness than a rich-type deck, not lower",
    ),
    Mutation(
        "code-preserved-threshold", "src/pipeline/quality.py",
        "if overlap >= _CODE_OVERLAP_THRESHOLD:",
        "if overlap > 1.0:",
        "tests/test_quality.py",
        "a fenced block that did survive into a code slide must be detected",
    ),
    Mutation(
        "composite-na-as-zero", "src/pipeline/quality.py",
        '"code_preserved": None if code is None else (1.0 if code else 0.0),',
        '"code_preserved": 0.0,',
        "tests/test_quality.py",
        "code_preserved N/A must be excluded and renormalised, not treated as a fail",
    ),
    Mutation(
        "filler-count-empty-check", "src/pipeline/quality.py",
        "    if not phrases:\n        return 0",
        "    if phrases:\n        return 0",
        "tests/test_quality.py",
        "filler_count must count hits when a phrase list is given, not only when it is empty",
    ),
    Mutation(
        "slides-malformed-guard", "src/pipeline/quality.py",
        "    return [s for s in raw if isinstance(s, dict)]",
        "    return raw",
        "tests/test_quality.py",
        "a malformed slide (raw completion from a free model) must not crash every metric",
    ),
    Mutation(
        "grounding-ratio-joined-blob", "src/pipeline/quality.py",
        '    deck_grams: set[tuple[str, ...]] = set()\n'
        '    for slide in _slides(doc):\n'
        '        for fragment in _slide_text(slide):\n'
        '            deck_grams |= _ngrams(str(fragment))',
        '    deck_grams: set[tuple[str, ...]] = _ngrams(deck_body_text(doc))',
        "tests/test_quality.py",
        "joining fragments before tokenising forges 3-grams at the boundary between "
        "adjacent fragments that exist nowhere in the deck's actual prose",
    ),
    Mutation(
        "grounding-ratio-fragment-union", "src/pipeline/quality.py",
        "            deck_grams |= _ngrams(str(fragment))",
        "            deck_grams = _ngrams(str(fragment))",
        "tests/test_quality.py",
        "each fragment's grams must accumulate into the running set, not overwrite "
        "it with only the last fragment scored",
    ),
    Mutation(
        "grounding-ratio-apostrophe-fold", "src/pipeline/quality.py",
        "words = [w.lower() for w in _WORD_RE.findall(_fold_apostrophes(text))]",
        "words = [w.lower() for w in _WORD_RE.findall(text)]",
        "tests/test_quality.py",
        "a curly apostrophe in LLM output must tokenise the same as the ASCII one in "
        "the source, or an identical sentence scores partial credit",
    ),
    Mutation(
        "slides-doc-guard", "src/pipeline/quality.py",
        '    if not isinstance(doc, dict):\n'
        "        return []\n"
        '    raw = doc.get("slides")',
        '    raw = doc.get("slides")',
        "tests/test_quality.py",
        "score_substance has no _deck_from_row shield when called on a fresh compose "
        "result -- a non-dict doc must degrade to no slides, not raise",
    ),
    Mutation(
        "slide-text-bullets-guard", "src/pipeline/quality.py",
        '    bullets = slide.get("bullets")\n'
        "    if isinstance(bullets, list):",
        '    bullets = slide.get("bullets")\n'
        "    if bullets:",
        "tests/test_quality.py",
        "a malformed bullets field (a string, not a list) must not be iterated "
        "character by character",
    ),
    Mutation(
        "filler-count-composite-exclusion", "src/pipeline/quality.py",
        '        "filler_count": (1.0 - _saturate(filler_density, FILLER_DENSITY_TARGET)\n'
        "                          if FILLER_COUNT_IN_COMPOSITE else None),",
        '        "filler_count": 1.0 - _saturate(filler_density, FILLER_DENSITY_TARGET),',
        "tests/test_quality.py",
        "filler_count must stay excluded/renormalised (v3, D74) -- it has no "
        "COMPOSITE_WEIGHTS entry any more, so an unguarded lookup must break "
        "loudly, not silently credit it",
    ),
    Mutation(
        "rescore-refuses-live-db", "scripts/eval_compose.py",
        "    if run_version is None:",
        "    if run_version is not None:",
        "tests/test_eval_compose_rescore.py",
        "a live-db run (summary.set_version None) must be refused, not silently "
        "paired against the golden set by id",
    ),
    Mutation(
        "rescore-refuses-version-mismatch", "scripts/eval_compose.py",
        "    if run_version != golden_version:",
        "    if run_version == golden_version:",
        "tests/test_eval_compose_rescore.py",
        "a run scored against a different golden-set version must be refused, not "
        "paired against whatever version happens to be on disk",
    ),
    Mutation(
        "deck-body-text-default-scope", "src/pipeline/quality.py",
        "def deck_body_text(doc: dict, *, exclude_types: frozenset[str] = frozenset()) -> str:",
        "def deck_body_text(doc: dict, *, exclude_types: frozenset[str] = "
        "frozenset({'links', 'sources', 'follow'})) -> str:",
        "tests/test_quality.py",
        "B3 (T74 bounce 1): the default exclude_types must exclude nothing -- "
        "every caller but filler_count relies on the unnarrowed default, and "
        "this one-token flip moved avg_substance_composite 39.570->40.415 / "
        "42.542->43.398 with the rest of the suite staying green",
    ),
    Mutation(
        "score-deck-empty-check-inverted", "scripts/eval_compose.py",
        '    return not any(slide.get(f) for f in REQUIRED_BODY_FIELDS.get(slide.get("type"), ()))',
        '    return any(slide.get(f) for f in REQUIRED_BODY_FIELDS.get(slide.get("type"), ()))',
        "tests/test_eval_compose_score_deck.py",
        "T100: a slide carrying its own type's required body field must score "
        "as NOT empty, not the other way around -- an inverted check would "
        "rate every populated deck as all-empty and the B4 links/sub slide "
        "as not-empty, exactly the bug REQUIRED_BODY_FIELDS was adopted to fix",
    ),
    Mutation(
        "headline-count-mismatch-inverted", "src/pipeline/integrity.py",
        "        if stated not in delivered:",
        "        if stated in delivered:",
        "tests/test_integrity.py tests/test_eval_compose_score_deck.py",
        "T104: a headline whose stated count agrees with what the deck "
        "delivers must NOT be flagged -- an inverted comparison would flag "
        "every consistent deck and wave through every real mismatch, exactly "
        "the blindness the user's '7 best / 4 delivered' report exists to close",
    ),
    Mutation(
        "headline-count-qualifier-gap-closed", "src/pipeline/integrity.py",
        r"""    rf"|\b(\d{{1,2}})(?!\.\d)\b(?:\s+[\w'’-]+){{0,3}}?\s+(?:{_COUNT_WORDS})\b",""",
        r"""    rf"|\b(\d{{1,2}})(?!\.\d)\b(?:\s+[\w'’-]+){{0,0}}?\s+(?:{_COUNT_WORDS})\b",""",
        "tests/test_integrity.py",
        "T104 bounce 1: closing the qualifier gap back to zero words is exactly "
        "the pre-bounce defect -- this detector returned [] on all three real "
        "decks (golden_current ids 26/29/30) that are the user's own complaint "
        "shape, because a qualifier ('GitHub', 'curated', ...) always sits "
        "between the digit and the listicle noun; a mutation that closes the "
        "gap again must be caught, not wave through the same blindness a "
        "second time",
    ),
    Mutation(
        "headline-count-receipt-gate", "src/pipeline/stages/compose.py",
        "lambda m: replacement if m.group(1) == target else m.group(0),",
        "lambda m: replacement,",
        "tests/stages/test_compose.py",
        "B7 (T102 bounce 1): reconcile_headline_counts must rewrite ONLY the "
        "digit equal to the receipt -- an unconditional replace rewrites "
        "every number in the headline ('7 tools, 2 that matter' -> "
        "'4 tools, 4 that matter'), the reconciler's single most important "
        "safety property and the one T102 shipped with zero anchored "
        "coverage for",
    ),
]


def clear_bytecode() -> None:
    """A reverted file whose .pyc survives is the mutant still running.

    Every top-level dir any `Mutation.path` targets -- DERIVED from
    `MUTATIONS` itself, not a second, independently-typed list (D78: this
    function used to hardcode `("src", "scripts")`, which is exactly the
    "the anchor drifts out from under a restated fact" failure T74 fixed one
    level down in `FILLER_PHRASES`/CTA text -- re-typing the same fact here
    was the identical mistake one file up). `scripts/eval_compose.py` needs
    its own clear because `tests/test_eval_compose_rescore.py` loads it via
    `importlib` (scripts/ has no `__init__.py`, so it is never imported as
    `pipeline.*` and its cache was invisible to this function until it
    cleared its own directory too) -- a stale
    `scripts/__pycache__/eval_compose.cpython-*.pyc` reproduced the exact
    false-result failure mode this function exists to prevent, on the
    unmutated tree, with no mutation running at all. Deriving the dir list
    means the NEXT mutation against a new top-level dir (e.g. `tests/`)
    clears correctly by construction instead of under-clearing silently
    while the run still reports clean.
    """
    root_dirs = sorted({m.path.split("/")[0] for m in MUTATIONS})
    for root_dir in root_dirs:
        for cache in ROOT.joinpath(root_dir).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)


#: pytest's own documented exit-code contract (docs.pytest.org/en/stable/
#: reference/exit-codes.html): 0 every collected test passed; 1 pytest ran
#: and at least one test failed; 2 the run was interrupted; 3 an internal
#: pytest error; 4 a usage error (bad CLI invocation); 5 no tests were
#: collected. Only 0 and 1 are a VERDICT on the code under test — 2-5 mean
#: pytest never finished evaluating it. `run_tests` used to collapse all six
#: into `result.returncode == 0`, so every one of 2-5 read as "not 0" ->
#: "caught" in `check()` below (T151: a reviewer reproduced this by removing
#: `.venv` — pytest could not even launch, and every mutation still reported
#: caught, so the sweep reported full coverage while testing nothing).
#: N1 (bounce 1, reviewer): exit 2 is pytest's own `INTERRUPTED` code, but in
#: practice it is measurably dominated by a COLLECTION error — a syntax or
#: import error in the file pytest tried to load — not a Ctrl-C. Measured
#: directly: a syntax-broken test file and an import-broken one both exit 2;
#: a genuine interrupt is the same code but the rarer real-world cause here,
#: since mutations routinely break syntax and never send SIGINT. The old
#: wording ("interrupted before it finished") sent the reader hunting for a
#: Ctrl-C on what is almost always their own mutation failing to import.
_PYTEST_INCONCLUSIVE_REASONS = {
    2: "pytest could not collect the tests — usually a syntax/import error "
       "in the mutated file (or, rarely, the run was interrupted)",
    3: "pytest hit an internal error",
    4: "pytest usage error — bad invocation",
    5: "no tests were collected",
}


def run_tests(paths: str) -> tuple[bool | None, str]:
    """Run pytest against `paths` and classify the result honestly.

    Returns ``(verdict, detail)``:
      - ``(True, "passed")``  — exit 0, every collected test passed.
      - ``(False, "failed")`` — exit 1, pytest ran and something failed.
        This is the only case that says "this specific behaviour broke."
      - ``(None, reason)``    — anything else: pytest exit 2-5 (it could not
        finish evaluating the code), or pytest could not even be launched
        (e.g. a missing `.venv` — `subprocess.run` raises `OSError`, caught
        here rather than crashing the sweep). `None` is deliberately not a
        bool: a caller doing `if run_tests(...)` on the old two-value
        contract would silently treat this as falsy/failed, reintroducing
        the exact bug this exists to close. Every caller must branch on it
        explicitly — `check()` below does, as a third "inconclusive" verdict.
    """
    try:
        result = subprocess.run(
            [str(PYTHON), "-m", "pytest", *paths.split(), "-q", "-x", "--no-header"],
            cwd=ROOT, capture_output=True, text=True,
        )
    except OSError as exc:
        return None, f"pytest could not be launched ({exc})"
    if result.returncode == 0:
        return True, "passed"
    if result.returncode == 1:
        return False, "failed"
    reason = _PYTEST_INCONCLUSIVE_REASONS.get(
        result.returncode, f"pytest exited {result.returncode}"
    )
    tail = _diagnostic_tail(getattr(result, "stdout", ""), getattr(result, "stderr", ""))
    return None, f"{reason} — {tail}"


#: N5 (bounce 1, reviewer, "worth it"): captured stdout/stderr used to be
#: discarded outright, so an INCONCLUSIVE verdict said only "fix the
#: environment and rerun" with zero diagnostics and never named which test
#: or import actually broke. `getattr(result, ..., "")` in `run_tests`
#: keeps this safe against test doubles that only set `.returncode`.
_DIAGNOSTIC_TAIL_CHARS = 200


def _diagnostic_tail(stdout: str, stderr: str) -> str:
    """The one captured line most likely to say WHAT broke.

    Prefers pytest's own summary lines (``ERROR ...`` / ``FAILED ...``,
    which name the file pytest choked on) over the literal last line, which
    for a collection error is usually just a timing summary like
    "1 error in 2.55s" — true, but useless to a human deciding what to fix.
    """
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return "no output captured"
    named = [ln for ln in lines if ln.startswith(("ERROR ", "FAILED "))]
    line = named[-1] if named else lines[-1]
    if len(line) > _DIAGNOSTIC_TAIL_CHARS:
        line = line[:_DIAGNOSTIC_TAIL_CHARS] + "…"
    return line


#: Memoizes the unmutated tree's own `run_tests` result per DISTINCT
#: `Mutation.tests` string, for the lifetime of one `main()` sweep — cleared
#: at the top of `main()`. The unmutated tree does not change between
#: mutations within a single sweep (`check()` always reverts before
#: returning), so re-running the same `paths` argument's baseline for every
#: mutation that shares it would only re-answer a question already
#: answered; several `Mutation`s share one `tests` value. Cheap by
#: construction: one pytest run per distinct test-target string per sweep,
#: not one per mutation (T151 AC).
_BASELINE_CACHE: dict[str, tuple[bool | None, str]] = {}


def _baseline(paths: str) -> tuple[bool | None, str]:
    """The unmutated tree's own verdict for `paths` (T151, T140's family).

    `check()`'s old `caught = not run_tests(...)` could not tell "the
    mutation broke this" from "this was already broken before the mutation
    ever ran." T140 proved the second case is not hypothetical: a
    pre-existing red test voided 16 of 68 verdicts, all reported as caught.
    If the unmutated tree is not cleanly green for `paths`, no exit code the
    MUTATED run produces means anything — the baseline has to be checked,
    once, before any of it can be trusted.

    B1 (bounce 1, reviewer): this was the only pytest invocation in the
    module not preceded by `clear_bytecode()`, despite the module docstring's
    promise that "bytecode is cleared around each run." Python's default
    import cache is invalidated on (mtime, size); a same-second, same-size
    edit to the source on disk (`git stash`/`pop`, a branch switch, or a
    hand-restored file after a killed sweep) leaves a stale `.pyc` that
    still looks valid, so an uncleared baseline run can silently reuse
    bytecode compiled from BEFORE that edit and report the pre-edit verdict
    for code that is actually red on disk right now -- T127's exact shape,
    reaching through this guard instead of `check()`'s own mutated-run call.
    """
    if paths not in _BASELINE_CACHE:
        clear_bytecode()
        _BASELINE_CACHE[paths] = run_tests(paths)
    return _BASELINE_CACHE[paths]


#: Written before a file is mutated and removed after it is restored. A run
#: killed mid-flight (SIGTERM skips `finally`) otherwise leaves the mutation on
#: disk, and the next thing to read that file sees deliberately broken code as
#: if it were the real source — which cost an hour of debugging a test failure
#: that was a leftover `if False:`.
#:
#: D92/D98: this used to be written with `write_text()` (silently overwrites)
#: and unlinked *unconditionally* at every `main()` startup via
#: `restore_any_leftover()`. A background sweep run concurrently with another
#: invocation in the same worktree raced on exactly that: the second run's
#: startup destroyed the first run's only recovery record before the first
#: run's own `finally` ever used it, and the first run's mutation — in an
#: unrelated file — was left uncommitted on disk with nothing left to recover
#: it. Caught only via a manual `git status --porcelain`. `check()` below now
#: claims this file with an *exclusive* create (`open(..., "x")`), which
#: fails loudly instead of overwriting a live record, and `main()` refuses to
#: start at all — no more auto-restore-and-continue — when the file already
#: exists or the tree is already dirty. Recovering a genuine crash leftover is
#: now a deliberate human step, not a startup side effect.
INFLIGHT = ROOT / "data" / ".mutate-inflight"


def _dirty_tree() -> str | None:
    """`git status --porcelain` output if the tree is dirty, else None.

    `check()`'s `finally` restores from its own snapshot — against a tree
    that is already dirty, that revert silently clobbers whatever a manual
    edit put there mid-run (D98). Checked before the sweep starts, same as
    after (the coding-standards.md Tests section says both).

    Deliberately coarse: this blocks on ANY dirty file in the whole repo,
    not only files a `Mutation` might touch — re-deriving "the exact set of
    paths this run could clobber" would mean matching `clear_bytecode`'s
    `MUTATIONS`-derived dir set (and re-deriving it on every `--only` run
    too), for a gate whose failure mode if it's ever wrong is "refuses to
    start" — a false refusal costs a rerun; a false permit costs a clobbered
    file. Erring coarse is the safe direction.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            "FATAL: could not determine whether the tree is clean — `git "
            f"status --porcelain` failed ({exc}). mutate.py refuses to "
            "guess; run it from inside a git checkout with git installed."
        ) from exc
    return result.stdout if result.stdout.strip() else None


def _refuse_reason() -> str | None:
    """Why this run must not start, or ``None`` when it is safe to (D92/D98).

    A crashed run leaves BOTH signals true at once: `INFLIGHT` (the recovery
    record) and a dirty tree (the mutation it describes, still applied). The
    two branches below are checked in that order on purpose — `INFLIGHT` is
    tested first — because a dirty tree caused by a genuine crash is exactly
    the case the recovery message exists for; a dirty-tree-only message in
    that case would never tell the human the record exists to read. A dirty
    tree with no `INFLIGHT` (a manual edit in progress, no crash) still gets
    the plain dirty-tree refusal.
    """
    if INFLIGHT.exists():
        return (
            f"{INFLIGHT} already exists — either another mutate.py run is "
            "active in this worktree, or a previous one was killed "
            "mid-mutation and never cleaned up. Read the file and `git "
            "status --porcelain` by hand: if a mutation it describes is "
            "still applied, restore that file's content yourself, then "
            f"remove {INFLIGHT} before retrying."
        )
    dirty = _dirty_tree()
    if dirty:
        return (
            "the working tree is dirty — a mutation's revert can silently "
            f"clobber whatever this already contains:\n{dirty}"
        )
    return None


def check(mutation: Mutation) -> tuple[str, str]:
    """Apply, test, revert. Returns ``(verdict, detail)``.

    `verdict` is one of three DISTINCT outcomes (T151 — the previous
    two-value `caught: bool` could only express two of them, and silently
    misfiled the third as whichever was left):
      - ``"caught"``       — baseline green, mutated run failed (pytest
                              exit 1). The tests actually noticed the
                              mutation.
      - ``"survived"``     — baseline green, mutated run also passed
                              (pytest exit 0). A real coverage gap.
      - ``"inconclusive"`` — nothing above could be established: the
                              target path does not exist (a typo in
                              `Mutation.path` — N6, bounce 1, reviewer), the
                              anchor never applied / was a no-op, the
                              BASELINE itself was not clean green (a
                              pre-existing red test — T140's family — or
                              the environment can't run pytest at all), or
                              the MUTATED run itself could not be evaluated
                              (pytest exit 2-5, or pytest could not be
                              launched). Reporting this as "caught" is
                              T151 itself; reporting it as "survived" would
                              cry wolf over nothing real.
    """
    target = ROOT / mutation.path
    try:
        original = target.read_text()
    except FileNotFoundError:
        # N6: a typo'd `Mutation.path` used to raise here, unhandled,
        # aborting every mutation still queued behind it in the sweep. Same
        # treatment as ANCHOR NOT FOUND two lines down — this run proves
        # nothing, so it reports that honestly instead of crashing.
        return "inconclusive", f"TARGET NOT FOUND — {mutation.path} does not exist"

    if mutation.old not in original:
        return "inconclusive", "ANCHOR NOT FOUND — the mutation never applied"
    mutated = original.replace(mutation.old, mutation.new, 1)
    if mutated == original:
        return "inconclusive", "MUTATION WAS A NO-OP"

    baseline_ok, baseline_detail = _baseline(mutation.tests)
    if baseline_ok is not True:
        return "inconclusive", (
            f"BASELINE NOT GREEN for {mutation.tests} ({baseline_detail}) — "
            "this mutation's verdict would be meaningless"
        )

    INFLIGHT.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(INFLIGHT, "x") as f:
            f.write(json.dumps({"path": mutation.path, "original": original}))
    except FileExistsError:
        raise SystemExit(
            f"FATAL: {INFLIGHT} already exists — another mutate.py run is "
            "active in this worktree, or a previous one was killed "
            "mid-mutation and never cleaned up (D92/D98). Inspect it and "
            "`git status --porcelain` by hand, restore any live mutation "
            f"yourself, then remove {INFLIGHT} before retrying."
        ) from None
    try:
        target.write_text(mutated)
        clear_bytecode()
        passed, detail = run_tests(mutation.tests)
    finally:
        target.write_text(original)
        clear_bytecode()
        INFLIGHT.unlink(missing_ok=True)
        if target.read_text() != original:
            raise SystemExit(f"FATAL: could not revert {mutation.path}")

    if passed is None:
        return "inconclusive", f"MUTATED RUN COULD NOT BE EVALUATED ({detail})"
    if passed:
        return "survived", "SURVIVED — nothing tests this"
    return "caught", "caught"


#: Distinct from EXIT_BAD_ARGS: a survived mutation (1) is a test-coverage
#: finding, an unknown --only name (2) is a usage mistake, and a start-time
#: safety refusal (3) is neither — it means nothing ran at all, which a
#: caller scripting around this exit code needs to tell apart from "ran and
#: found a gap" (nit: bounce 1, reviewer — both used to return 2).
#: EXIT_INCONCLUSIVE (4) is a FOURTH, separate thing again (T151): mutations
#: DID run, but at least one verdict cannot be trusted — a red baseline or
#: an environment pytest could not evaluate. It takes priority over
#: EXIT_SURVIVED when both occur in the same sweep: an inconclusive result
#: means the survived/caught counts themselves are not fully trustworthy
#: either, so "fix the environment and rerun" has to outrank "here is a
#: coverage gap" in what the caller sees. This EXTENDS the devops bundle's
#: 0/1/2/3 contract; it does not renumber any of it.
EXIT_SURVIVED = 1
EXIT_BAD_ARGS = 2
EXIT_REFUSED_TO_START = 3
EXIT_INCONCLUSIVE = 4


def main(only: str | None, listing: bool) -> int:
    _BASELINE_CACHE.clear()
    chosen = [m for m in MUTATIONS if only is None or m.name == only]
    if not chosen:
        print(f"no mutation named {only!r}")
        return EXIT_BAD_ARGS

    if listing:
        for m in chosen:
            print(f"  {m.name:22} {m.path.split('/')[-1]:22} {m.why}")
        return 0

    refuse = _refuse_reason()
    if refuse:
        print(f"refusing to start: {refuse}")
        return EXIT_REFUSED_TO_START

    print(f"running {len(chosen)} mutations\n")
    survived = []
    inconclusive = []
    marks = {"caught": "ok  ", "survived": "FAIL", "inconclusive": "WARN"}
    for m in chosen:
        verdict, detail = check(m)
        print(f"  {marks[verdict]} {m.name:22} {detail}")
        if verdict == "survived":
            survived.append(m)
        elif verdict == "inconclusive":
            # N4 (bounce 1, reviewer): carry `detail` (the REASON this run
            # could not be trusted -- ANCHOR NOT FOUND, BASELINE NOT GREEN,
            # a pytest exit code) alongside the mutation. `m.why` is the
            # guarantee the mutation was meant to prove and says nothing
            # about why THIS run was inconclusive -- right for survivors,
            # wrong here.
            inconclusive.append((m, detail))

    print()
    if inconclusive:
        print(
            f"{len(inconclusive)} mutation(s) INCONCLUSIVE — not evidence of "
            "anything either way; the environment or a baseline test is "
            "broken, fix that and rerun:"
        )
        for m, detail in inconclusive:
            print(f"  - {m.name}: {detail}")
    if survived:
        print(f"{len(survived)} mutation(s) survived — those guarantees are untested:")
        for m in survived:
            print(f"  - {m.name}: {m.why}")

    if inconclusive:
        return EXIT_INCONCLUSIVE
    if survived:
        return EXIT_SURVIVED
    print(f"all {len(chosen)} mutations caught")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single mutation by name")
    ap.add_argument("--list", action="store_true", help="show mutations without running")
    args = ap.parse_args()
    raise SystemExit(main(args.only, args.list))
