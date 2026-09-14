"""Run the media tunnel on its own, without the pipeline.

Normally the pipeline supervises this itself (MANAGE_TUNNEL=true) because
macOS blocks a newly added launchd job until someone approves it in System
Settings, and a second service that silently exits 78 is worse than no second
service. This wrapper exists for the times you want the tunnel up without the
rest of the app: testing media hosting, or re-pointing a stale R2_PUBLIC_BASE
by hand.

    ./.venv/bin/python scripts/tunnel.py

Prints the origin it publishes; Ctrl-C clears it again.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.publish.tunnel import supervise  # noqa: E402


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await supervise(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
