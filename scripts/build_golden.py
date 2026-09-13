"""Snapshot a fixed evaluation set from real items, tagged by failure mode.

`eval_compose.py` used to score "the first 16 items that have a brief". That set
changes whenever the database does — items get requeued, research is rewritten,
new items arrive — so two runs a day apart were not measuring the same thing.
Every comparison made that way is worth less than it looks.

This writes a self-contained snapshot: every field compose() reads, copied out
of the database and frozen. Requeue an item afterwards, delete it, rewrite its
research, and the golden set is unaffected.

Cases are chosen for the failure they represent, not for being representative
traffic — a set of easy cases scores well forever and tells you nothing.

    ./.venv/bin/python scripts/build_golden.py            # write data/eval/golden.json
    ./.venv/bin/python scripts/build_golden.py --list     # show the cases only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402

GOLDEN = ROOT / "data" / "eval" / "golden.json"

#: Bump when the case list changes, so a saved run names the set it scored.
SET_VERSION = 1

#: (item id, failure mode, why this case is here).
#:
#: Failure modes come from what actually went wrong in production, which is the
#: only source of edge cases nobody thought to invent.
CASES: list[tuple[int, str, str]] = [
    # --- enumeration: the path that broke most often -----------------------
    (26, "enumeration-ok",
     "8 repos with real urls and logos; the shape a list request should produce"),
    (29, "enumeration-no-tail",
     "9 slides ending on a repo — no links index, no follow slide"),
    (30, "enumeration-no-tail",
     "same failure on a second run, so it is not a one-off sample"),
    (34, "enumeration-misclassified",
     "a philosophy question routed to GitHub enumeration; composed dev tools"),

    # --- closing slides ----------------------------------------------------
    (14, "missing-closing-slide",
     "ended on a body slide: no sources, no follow, nothing to attribute to"),
    (17, "missing-closing-slide",
     "second instance, channel source rather than dm"),

    # --- explainer requests, where prose rules were ignored most -----------
    (12, "explainer",
     "research request that wants teaching, not a news verdict"),
    (21, "explainer",
     "how-does-it-work request; the type that should reach for flow and code"),
    (16, "explainer-duplicate",
     "same request as 19, so run-to-run variance is visible within one set"),
    (19, "explainer-duplicate",
     "the other half of the pair"),

    # --- thin material -----------------------------------------------------
    (22, "thin-research",
     "2 notes and 4 slides; the floor where a deck stops being worth posting"),
    (10, "thin-research",
     "2 notes from a channel post, to check thin handling is not dm-only"),

    # --- ordinary news, the bulk of real traffic ---------------------------
    (3, "news", "plain channel news item"),
    (5, "news", "channel news with an attached image"),
    (8, "news", "quote-led channel post"),
    (9, "news", "9 slides; the long end of normal"),
    (15, "news", "channel post whose subject is a person"),
    (20, "news", "numbers-heavy post; should reach for kpi or chart"),
    (31, "news", "has a usable screenshot, exercises the photo slide"),
    (35, "news", "composed after the follow-slide fix"),
    (36, "news", "second post-fix channel item"),
    (38, "news", "research-paper subject rather than a product launch"),
    (33, "news-dm", "direct request, so it must close on follow not sources"),
    (37, "news-dm", "direct request on a subject with no obvious imagery"),
]

#: Fields compose() reads. Snapshotting only these keeps the file small and
#: makes it obvious that render/publish state is deliberately excluded.
SNAPSHOT_FIELDS = (
    "id", "source", "raw_text", "brief", "research", "intent",
    "extracted", "answer", "regen_note", "confidence",
)


async def main(list_only: bool) -> int:
    settings = Settings.load()
    db = await Database(settings.db_path).connect()
    try:
        cases, missing = [], []
        for item_id, mode, why in CASES:
            item = await db.get_item(item_id)
            if item is None or not (item.brief or "").strip():
                missing.append(item_id)
                continue
            full = asdict(item)
            cases.append({
                "failure_mode": mode,
                "why": why,
                "item": {k: full.get(k) for k in SNAPSHOT_FIELDS},
            })

        by_mode: dict[str, int] = {}
        for case in cases:
            by_mode[case["failure_mode"]] = by_mode.get(case["failure_mode"], 0) + 1

        print(f"{len(cases)} cases, {len(by_mode)} failure modes")
        for mode, n in sorted(by_mode.items()):
            print(f"  {mode:28} {n}")
        if missing:
            print(f"\nskipped (absent or no brief): {missing}")

        if list_only:
            return 0

        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(
            {"set_version": SET_VERSION, "cases": cases}, indent=1, ensure_ascii=False
        ))
        size = GOLDEN.stat().st_size
        print(f"\nwrote {GOLDEN.relative_to(ROOT)} (v{SET_VERSION}, {size // 1024}KB)")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show cases without writing")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.list)))
