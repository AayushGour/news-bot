"""T187 -- measure `item_echo_ratio` on the BEFORE arm. MEASURE ONLY.

Sequencing is the whole point of this script: T188 is about to let
`synthesize.SYSTEM` draw the brief from `item.raw_text` as an attributed
source. Once that lands, `item_echo_ratio` moves, and the pre-T188 number
becomes unrecoverable. This script exists to pin that number BEFORE any
T188 diff exists.

`item_echo_ratio` (`pipeline.quality`) is pure string/set arithmetic --
reused unmodified from there (house rule 3: derive, don't restate) -- so
this script adds only orchestration around it, exactly the same shape as
`scripts/measure_grounding_provenance.py`.

The BEFORE arm costs +0 model calls: `data/eval/golden.json` carries a
pre-baked `brief` on every case (`item.brief`), and every case carries
`raw_text` (`item.raw_text`) -- both written once, when the golden set was
curated, long before this script ever runs. Scoring them is pure arithmetic
over a frozen corpus: no LLM call, no network call, no API key needed, zero
account-wide quota spent (house rule 9).

This script deliberately has NO `--resynthesize` (or equivalent) arm. That
is T188's AFTER measurement -- a different task, run once `synthesize.SYSTEM`
has actually changed -- and building it here would let this task's own
diff move the very number it exists to protect.

    ./.venv/bin/python scripts/measure_item_echo.py   # if a venv exists
    python3 scripts/measure_item_echo.py              # stdlib only otherwise

Zero dependencies beyond the stdlib and `pipeline.quality` (stdlib-only per
house rule 6), so this runs with no `uv sync` and no API key configured.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.quality import item_echo_ratio  # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"
GOLDEN_CASES_FILE = EVAL_DIR / "golden.json"

#: `item.source` is one of these two (`pipeline.models.Item.source`
#: docstring: `'channel' | 'dm'`) -- reported separately per AC2 because
#: `raw_text` means something different for each: a channel post is the
#: unedited article the item is ABOUT, while a DM is the operator's own
#: request TO the pipeline, which routinely quotes or names the same subject
#: the brief ends up covering for an unrelated reason (the operator is
#: describing what they want researched, not the source material itself).
SOURCE_ORDER: tuple[str, ...] = ("channel", "dm")


def load_golden_cases() -> tuple[list[dict], int]:
    """`golden.json`'s cases, plus the file's own `set_version`.

    Every case already carries both fields this script needs directly on
    `case["item"]` -- `brief` (pre-baked at golden-set curation time) and
    `raw_text` -- so no `doc`/saved-run file, no `Item` reconstruction, and
    no deck at all is needed to compute this metric (unlike
    `measure_grounding_provenance.py`, which scores the DECK's language
    against the source; this scores the BRIEF against `raw_text` directly).
    """
    data = json.loads(GOLDEN_CASES_FILE.read_text())
    return data["cases"], data["set_version"]


def scored_rows(cases: list[dict]) -> list[dict]:
    """One row per case: id, source, the two texts' lengths, and the ratio.

    Guards each field with `.get(...)` rather than assuming golden.json's
    shape -- a malformed or hand-edited corpus entry should show up as a
    visibly wrong row (e.g. `brief_chars=0`), not crash the whole script.
    """
    rows = []
    for case in cases:
        item = case.get("item", {})
        brief = item.get("brief") or ""
        raw_text = item.get("raw_text") or ""
        rows.append(
            {
                "id": item.get("id"),
                "source": item.get("source", "?"),
                "brief_chars": len(brief),
                "raw_text_chars": len(raw_text),
                "item_echo_ratio": item_echo_ratio(brief, raw_text),
            }
        )
    return rows


def _mean(values: list[float]) -> float:
    return round(statistics.mean(values), 4) if values else 0.0


def _stdev(values: list[float]) -> float:
    return round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0


def print_config_header(rows: list[dict], set_version: int) -> None:
    print("=" * 72)
    print("CONFIGURATION (house rule 4 -- a measurement without its")
    print("configuration is not reproducible)")
    print("=" * 72)
    print(f"  date                    {datetime.now(tz=UTC).date().isoformat()}")
    print("  script                  scripts/measure_item_echo.py")
    print("  arm                     BEFORE (T187) -- pre-T188 baseline")
    print(f"  file read               data/eval/golden.json (set_version {set_version})")
    print("  metric                  pipeline.quality.item_echo_ratio(brief, raw_text)")
    print("                          -- imported unmodified, not re-derived here")
    print("  provider / model        N/A -- zero model calls, zero network calls;")
    print("                          brief and raw_text are both already on disk")
    print("  cases scored            " f"{len(rows)} (every golden.json case; none excluded)")
    by_source = {s: sum(1 for r in rows if r["source"] == s) for s in SOURCE_ORDER}
    other = len(rows) - sum(by_source.values())
    print(
        "  cases by source         "
        + ", ".join(f"{s}={n}" for s, n in by_source.items())
        + (f", other={other}" if other else "")
    )
    print()


def print_per_case_table(rows: list[dict]) -> None:
    print("--- per-case item_echo_ratio ---")
    header = f"  {'id':>4} {'source':8} {'brief_chars':>12} {'raw_text_chars':>15} {'item_echo_ratio':>16}"
    print(header)
    for row in sorted(rows, key=lambda r: (r["source"], r["id"] if r["id"] is not None else -1)):
        print(
            f"  {row['id']!s:>4} {row['source']:8} {row['brief_chars']:>12} "
            f"{row['raw_text_chars']:>15} {row['item_echo_ratio']:>16.4f}"
        )
    print()


def print_aggregate(rows: list[dict]) -> None:
    print("--- aggregate (AC2: distribution, not just one mean) ---")
    all_values = [r["item_echo_ratio"] for r in rows]
    print(
        f"  {'group':10} {'n':>4} {'mean':>8} {'stdev':>8} {'min':>8} {'max':>8}"
    )
    print(
        f"  {'ALL':10} {len(all_values):>4} {_mean(all_values):>8.4f} "
        f"{_stdev(all_values):>8.4f} {min(all_values, default=0.0):>8.4f} "
        f"{max(all_values, default=0.0):>8.4f}"
    )
    for source in SOURCE_ORDER:
        values = [r["item_echo_ratio"] for r in rows if r["source"] == source]
        if not values:
            print(f"  {source:10}    0        -        -        -        -  (no cases)")
            continue
        print(
            f"  {source:10} {len(values):>4} {_mean(values):>8.4f} "
            f"{_stdev(values):>8.4f} {min(values):>8.4f} {max(values):>8.4f}"
        )
    print()


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()

    if not GOLDEN_CASES_FILE.exists():
        raise SystemExit(
            f"ERROR: {GOLDEN_CASES_FILE} does not exist -- this is the gitignored "
            "golden-set item corpus (item briefs + raw_text), generated/curated "
            "locally and never committed. Nothing to measure without it."
        )

    cases, set_version = load_golden_cases()
    rows = scored_rows(cases)
    if not rows:
        raise SystemExit(f"ERROR: {GOLDEN_CASES_FILE} contains zero cases.")

    print_config_header(rows, set_version)
    print_per_case_table(rows)
    print_aggregate(rows)


if __name__ == "__main__":
    main()
