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
import sys
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
]


def clear_bytecode() -> None:
    """A reverted file whose .pyc survives is the mutant still running."""
    for cache in ROOT.joinpath("src").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run_tests(paths: str) -> bool:
    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", *paths.split(), "-q", "-x", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


#: Written before a file is mutated and removed after it is restored. A run
#: killed mid-flight (SIGTERM skips `finally`) otherwise leaves the mutation on
#: disk, and the next thing to read that file sees deliberately broken code as
#: if it were the real source — which cost an hour of debugging a test failure
#: that was a leftover `if False:`.
INFLIGHT = ROOT / "data" / ".mutate-inflight"


def restore_any_leftover() -> None:
    """Undo a mutation from a previous run that was killed before reverting."""
    if not INFLIGHT.exists():
        return
    saved = json.loads(INFLIGHT.read_text())
    target = ROOT / saved["path"]
    if target.read_text() != saved["original"]:
        target.write_text(saved["original"])
        print(f"restored {saved['path']} — a previous run was killed mid-mutation")
    INFLIGHT.unlink()
    clear_bytecode()


def check(mutation: Mutation) -> tuple[bool, str]:
    """Apply, test, revert. True when the tests caught it."""
    target = ROOT / mutation.path
    original = target.read_text()

    if mutation.old not in original:
        return False, "ANCHOR NOT FOUND — the mutation never applied"
    mutated = original.replace(mutation.old, mutation.new, 1)
    if mutated == original:
        return False, "MUTATION WAS A NO-OP"

    INFLIGHT.parent.mkdir(parents=True, exist_ok=True)
    INFLIGHT.write_text(json.dumps({"path": mutation.path, "original": original}))
    try:
        target.write_text(mutated)
        clear_bytecode()
        caught = not run_tests(mutation.tests)
    finally:
        target.write_text(original)
        clear_bytecode()
        INFLIGHT.unlink(missing_ok=True)
        if target.read_text() != original:
            raise SystemExit(f"FATAL: could not revert {mutation.path}")

    return caught, "caught" if caught else "SURVIVED — nothing tests this"


def main(only: str | None, listing: bool) -> int:
    restore_any_leftover()
    chosen = [m for m in MUTATIONS if only is None or m.name == only]
    if not chosen:
        print(f"no mutation named {only!r}")
        return 2

    if listing:
        for m in chosen:
            print(f"  {m.name:22} {m.path.split('/')[-1]:22} {m.why}")
        return 0

    print(f"running {len(chosen)} mutations\n")
    survived = []
    for m in chosen:
        caught, detail = check(m)
        mark = "ok  " if caught else "FAIL"
        print(f"  {mark} {m.name:22} {detail}")
        if not caught:
            survived.append(m)

    print()
    if survived:
        print(f"{len(survived)} mutation(s) survived — those guarantees are untested:")
        for m in survived:
            print(f"  - {m.name}: {m.why}")
        return 1
    print(f"all {len(chosen)} mutations caught")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single mutation by name")
    ap.add_argument("--list", action="store_true", help="show mutations without running")
    args = ap.parse_args()
    raise SystemExit(main(args.only, args.list))
