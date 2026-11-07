"""Report reclaimable disk, and delete only what this project clearly owns.

The selection and deletion logic lives in ``pipeline.retention`` - the worker
runs the same code on a timer (see RETENTION_* in .env.example). This is the
manual front end: report-only by default, ``--apply`` to act.

What ``--apply`` does, per the same policy the worker uses: files (never DB
rows) of terminal items past their grace period, bucket objects under
``items/<id>/``, and the auto-reject of items that waited too long in
awaiting_approval. Docker, Ollama and browser caches are reported with their
size and left alone - SearXNG lives in Docker and pruning it blind would take
the search backend down.

    ./.venv/bin/python scripts/cleanup.py            # report only
    ./.venv/bin/python scripts/cleanup.py --apply    # do it
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.retention import run_retention  # noqa: E402


def run(cmd: list[str]) -> str:
    """Best effort — a missing tool is a blank section, not a crash."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def report_elsewhere() -> None:
    """Everything this script will not touch, so the decision stays yours."""
    print("\n--- not touched, reported only ---")

    df = run(["df", "-h", "/System/Volumes/Data"]).splitlines()
    if len(df) > 1:
        print(f"  disk: {' '.join(df[-1].split()[2:5])} used/avail/capacity")

    docker = run(["docker", "system", "df"])
    if docker:
        print("\n  docker (SearXNG lives here — do not prune blindly):")
        for line in docker.splitlines()[1:]:
            print(f"    {line}")
        print("    reclaim with: docker system prune -a --volumes   # STOPS SearXNG")

    ollama = run(["ollama", "list"])
    if ollama:
        lines = ollama.splitlines()[1:]
        print(f"\n  ollama: {len(lines)} models")
        for line in lines:
            # NAME  ID  SIZE UNIT  MODIFIED... — size is two tokens, so it has
            # to be joined rather than indexed from either end.
            parts = line.split()
            size = f"{parts[2]} {parts[3]}" if len(parts) >= 4 else "?"
            print(f"    {size:>9}  {parts[0][:70]}")
        print("    remove one with: ollama rm <name>")


async def main(apply: bool) -> int:
    settings = Settings.load()
    db = await Database(settings.db_path).connect()
    try:
        report = await run_retention(db, settings, apply=apply)
    finally:
        await db.close()

    print(f"--- retention ({'APPLIED' if apply else 'report only'}) ---")
    for line in report.lines:
        print(f"  {line}")
    if not report.lines:
        print("  nothing to reclaim")
    for line in report.refused:
        print(f"  REFUSED (outside media dir / symlink): {line}")
    for line in report.errors:
        print(f"  ERROR: {line}")
    print(f"\n{report.summary()}")
    print(f"awaiting_approval: {report.awaiting} "
          f"(oldest {report.oldest_awaiting_days}d, limit "
          f"{settings.awaiting_approval_max_days}d); "
          f"needs_input: {report.needs_input} (never touched)")
    if not apply and report.did_something:
        print("report only - pass --apply to do this")

    report_elsewhere()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete / auto-reject (default: report only)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
