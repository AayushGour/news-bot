"""Report reclaimable disk, and delete only what this project clearly owns.

The pipeline writes a directory of PNGs per item and never removes them, so
media grows without bound while most of it belongs to items that were
published, dropped or rejected months ago.

Deletion here is deliberately narrow: media directories for items in a
terminal state, and only with ``--apply``. Everything else — Docker, Ollama,
browser caches — is reported with its size and left alone, because this script
cannot know which of those you still need. SearXNG in particular lives in
Docker, and pruning Docker without checking would take the search backend down
with it.

    ./.venv/bin/python scripts/cleanup.py            # report only
    ./.venv/bin/python scripts/cleanup.py --apply    # delete terminal media
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.models import TERMINAL  # noqa: E402


def human(size: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def run(cmd: list[str]) -> str:
    """Best effort — a missing tool is a blank section, not a crash."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


async def terminal_media(settings: Settings) -> tuple[list[tuple[Path, int]], int]:
    """Media directories belonging to items nothing will ever read again."""
    db = await Database(settings.db_path).connect()
    try:
        rows = await db.conn.execute_fetchall("SELECT id, status FROM items")
        terminal_ids = {
            str(r["id"]) for r in rows if r["status"] in {s.value for s in TERMINAL}
        }
    finally:
        await db.close()

    found: list[tuple[Path, int]] = []
    media_root = Path(settings.media_dir)
    if media_root.is_dir():
        for child in sorted(media_root.iterdir()):
            if child.is_dir() and child.name in terminal_ids:
                found.append((child, dir_size(child)))
    return found, sum(size for _, size in found)


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

    dirs, total = await terminal_media(settings)
    print(f"--- media for terminal items ({len(dirs)} dirs, {human(total)}) ---")
    for path, size in dirs:
        print(f"  {human(size):>8}  {path}")
    if not dirs:
        print("  nothing to reclaim")

    if apply and dirs:
        for path, _ in dirs:
            shutil.rmtree(path, ignore_errors=True)
        print(f"\ndeleted {len(dirs)} directories, reclaimed {human(total)}")
    elif dirs:
        print("\nreport only — pass --apply to delete these")

    report_elsewhere()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete terminal-item media")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
