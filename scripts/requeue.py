"""CLI wrapper around pipeline.requeue.

    ./.venv/bin/python scripts/requeue.py 26                  # back to research
    ./.venv/bin/python scripts/requeue.py 26 --to composed    # just re-render
    ./.venv/bin/python scripts/requeue.py 26 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.models import Status  # noqa: E402
from pipeline.requeue import TARGETS, fields_to_clear, requeue  # noqa: E402


async def main(item_id: int, target: Status, dry_run: bool) -> int:
    settings = Settings.load()
    db = await Database(settings.db_path).connect()
    try:
        item = await db.get_item(item_id)
        if item is None:
            print(f"item {item_id} does not exist")
            return 1

        cleared = fields_to_clear(target)
        print(f"item {item_id}: {item.status} -> {target.value}")
        print(f"  intent={item.intent} source={item.source}")
        print("  clearing: " + ", ".join(sorted(cleared)))

        if dry_run:
            print("\ndry run — nothing written")
            return 0

        await requeue(db, item_id, target, detail="manual requeue")
        print(f"\nrequeued. the running worker will pick it up from {target.value}.")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("item_id", type=int)
    ap.add_argument("--to", default=Status.TRIAGED.value, choices=sorted(TARGETS),
                    help="stage to resume from (default: triaged = re-research)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.item_id, Status(args.to), args.dry_run)))
