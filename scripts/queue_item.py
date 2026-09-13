"""CLI wrapper around pipeline.intake.manual.

    ./.venv/bin/python scripts/queue_item.py "Elon Musk vs chess.com"
    ./.venv/bin/python scripts/queue_item.py "10 github repos for rust" --source channel
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
from pipeline.intake.manual import TooThin, queue_request  # noqa: E402


async def main(text: str, source: str) -> int:
    settings = Settings.load()
    db = await Database(settings.db_path).connect()
    try:
        item_id = await queue_request(db, settings, text, source)
    except TooThin as exc:
        print(exc)
        return 1
    finally:
        await db.close()

    print(f"queued item {item_id} ({source}): {text}")
    print("the running worker will pick it up from ingested.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--source", default="dm", choices=["dm", "channel"])
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.text, args.source)))
