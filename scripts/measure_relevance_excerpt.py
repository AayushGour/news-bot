"""Measure how large a relevance-gate excerpt actually needs to be.

B3 (T71/T72 bounce 1): ``_is_relevant`` used to slice a flat, uncited
``body[:1200]`` (the original defect coding-standards.md rule 2 names), then
T72 widened it to ``budget_chars_for_role(role="cheap")`` -- the WHOLE
note-writing corpus budget (12,288 chars local / 131,072 hosted). That number
answers "how much corpus can this call's context window hold", which is not
the question a single binary yes/no relevance judgement asks. Reviewed
finding: the wider excerpt bought +0.0232 gate-accept-rate (one extra
accepted source in 43 -- inside this codebase's own established
``gate_accept_rate_overall`` noise floor of +/-0.023, D83) while tripling
per-item wall-clock on item 503 at identical gate/note counts. house rule 4
requires a governing number to be either cited from a platform limit or
produced by a re-runnable measurement -- this script is that measurement.

**Method.** Reuse ``eval_research.py``'s own concurrent ``research()`` run
(the SAME frozen golden-set items, the SAME real relevance gate, the SAME
``research_concurrency`` fan-out that makes that harness's own per-item
wall-clock tractable) rather than re-implementing a slower, sequential
fetch+judge loop. For each candidate ``RELEVANCE_EXCERPT_CHARS`` value,
monkeypatch the module constant, run one pass over the requested item(s),
and read ``gate_accept_rate_overall`` and per-item ``seconds`` straight off
``eval_research``'s own summary/rows -- no metric is re-derived here.

The chosen size is the SMALLEST one whose accept rate sits within D83's own
+/-0.023 noise floor of the full-local-budget accept rate -- reusing an
already-established threshold from this codebase rather than inventing a
new one.

Live model + live SearXNG required (same as ``eval_research.py``); nothing
here is faked. Not wired into CI -- like ``measure_slide_capacity.py``, this
is a standalone, re-runnable measurement whose OUTPUT is what gets cited,
not a test that runs on every commit.

Usage:
    ./.venv/bin/python scripts/measure_relevance_excerpt.py
    ./.venv/bin/python scripts/measure_relevance_excerpt.py --items 501,503
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import pipeline.stages.research as research_mod  # noqa: E402
from pipeline.budget import budget_chars_for_role  # noqa: E402
from pipeline.config import Settings  # noqa: E402

import eval_research  # noqa: E402  (reuse the real harness, do not reimplement)

#: Sizes swept below the full local budget, plus the full budget itself
#: (appended at runtime from the live settings) as the upper reference point
#: T72 shipped. 1200 is the ORIGINAL pre-T72 guess, kept as a comparison
#: point precisely because rule 2 flagged it as uncited, not because it is
#: assumed correct.
CANDIDATE_SIZES = [1200]

#: D83 (project-context.md): three same-config reps of the real research
#: harness put gate_accept_rate_overall's own noise floor at +/-0.023.
#: Reused here as the stabilisation threshold rather than inventing a new
#: one -- an excerpt size within this band of the full-budget accept rate is
#: indistinguishable from "the whole corpus" by this codebase's own
#: established measure of noise.
NOISE_FLOOR = 0.023


async def _one_pass(settings: Settings, only: list[int] | None) -> dict:
    items, reasons, set_version = eval_research.load_golden(only)
    fingerprint = eval_research.prompt_fingerprint(settings)
    stats = eval_research.CacheStats()
    rows = await eval_research._score_all_items(
        items, reasons, settings, eval_research.RESEARCH_TEMPERATURE,
        fingerprint, stats, use_plan_cache=True,
    )
    summary = eval_research.summarise(rows, stats, fingerprint, set_version)
    return {"summary": summary, "rows": rows}


async def run(only: list[int] | None) -> None:
    settings = Settings.load()
    settings.validate_model_config()
    full_budget = budget_chars_for_role(settings, role="cheap")
    sizes = [*CANDIDATE_SIZES, full_budget]

    print(f"provider={settings.llm_provider}  cheap-model={settings.model_cheap}  "
          f"items={only or 'all'}  full_local_budget={full_budget}\n")

    accept_rates: dict[int, float] = {}
    wall_clock: dict[int, float] = {}
    for size in sizes:
        research_mod.RELEVANCE_EXCERPT_CHARS = size
        result = await _one_pass(settings, only)
        rate = result["summary"]["gate_accept_rate_overall"]
        seconds = round(sum(r["seconds"] for r in result["rows"]), 1)
        accept_rates[size] = rate
        wall_clock[size] = seconds
        print(f"  size={size:6}  gate_accept_rate_overall={rate}  "
              f"wall_clock={seconds}s  "
              f"notes={result['summary']['notes_produced_total']}  "
              f"gate_checked={sum(r['gate_checked'] for r in result['rows'])}")

    reference = accept_rates[full_budget]
    # N6 (bounce 1, reviewer): reference is None on a zero-gate run (no
    # candidate was ever checked, so eval_research.summarise leaves
    # gate_accept_rate_overall undefined) -- accept_rates[s] is not None
    # alone does not stop `abs(accept_rates[s] - reference)` from still
    # raising TypeError against a None reference.
    in_band = [s for s in sizes if reference is not None
               and accept_rates[s] is not None
               and abs(accept_rates[s] - reference) <= NOISE_FLOOR]
    chosen = min(in_band) if in_band else full_budget
    print(f"\nfull-budget ({full_budget}) gate_accept_rate_overall = {reference}, "
          f"wall_clock={wall_clock[full_budget]}s")
    print(f"smallest size within +/-{NOISE_FLOOR} of it (D83 noise floor): {chosen}, "
          f"wall_clock={wall_clock[chosen]}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", help="comma-separated golden-set ids")
    args = ap.parse_args()
    only = [int(x) for x in args.items.split(",")] if args.items else None
    asyncio.run(run(only))
