"""Where the media host is reachable from the public internet, right now.

Instagram fetches slides from a URL it resolves itself, so that URL has to be
public. Local storage sits behind a Cloudflare quick tunnel, and a quick
tunnel's hostname is regenerated every time cloudflared starts. Pinning it in
R2_PUBLIC_BASE meant the config was stale from the next restart onward: item
100 was uploaded against a hostname that no longer resolved, Instagram fetched
nothing, and the failure surfaced as "Only photo or video can be accepted as
media type" — a message about file formats, for a DNS problem.

A hostname that changes on its own schedule is not configuration. The tunnel
supervisor writes the live origin to a file as soon as cloudflared reports it,
and uploads read that file at the moment they need it. R2_PUBLIC_BASE stays as
the fallback so a stable host — a named tunnel, a real bucket, a CDN — still
works by being set the ordinary way, and nothing here has to know which it is.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

#: Written by scripts/tunnel.py, read at upload time. Deliberately a file
#: rather than an environment variable: the pipeline is a long-lived process
#: and the tunnel can be restarted underneath it, which an env var read at
#: startup could never reflect.
ORIGIN_FILE = Path(__file__).resolve().parents[3] / "data" / "tunnel-origin.txt"


def live_origin(path: Path | None = None) -> str:
    """The origin the tunnel currently answers on, or "" if unknown."""
    target = path or ORIGIN_FILE
    try:
        origin = target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    # A half-written file, or a leftover from a failed start, must not become
    # the base of every media URL in a post.
    if not origin.startswith("https://"):
        if origin:
            log.warning("ignoring malformed tunnel origin %r in %s", origin[:60], target)
        return ""
    return origin.rstrip("/")


def public_base(settings, path: Path | None = None) -> str:
    """The base every media URL is built from.

    The live tunnel wins over the configured value, because the configured
    value cannot track a hostname that changes without anyone editing it. When
    no tunnel is running this returns R2_PUBLIC_BASE unchanged, so a hosted
    bucket or a named tunnel needs no special case.
    """
    origin = live_origin(path)
    if not origin:
        return (settings.r2_public_base or "").rstrip("/")
    bucket = (settings.r2_bucket or "").strip("/")
    return f"{origin}/{bucket}" if bucket else origin


# --------------------------------------------------------------- supervision

#: cloudflared prints the hostname it was assigned once, in a startup banner —
#: but it also mentions https://api.trycloudflare.com in ordinary log lines,
#: and a pattern that accepted any subdomain published THAT as the origin,
#: which would have sent Instagram to Cloudflare's API for every slide.
#: An assigned quick-tunnel hostname is always a hyphenated slug
#: ("talks-protective-columnists-copper"); the service endpoints are single
#: words. Requiring at least one hyphen separates them.
_ORIGIN_RE = re.compile(rb"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")

#: What the tunnel fronts: MinIO's S3 port, which serves the bucket over HTTP.
LOCAL_TARGET = "http://localhost:9000"

#: Wait before restarting a tunnel that exited. Short — every second down is a
#: publish that cannot run — but not zero, so a persistent failure cannot spin.
RESTART_DELAY_S = 5

#: How much of cloudflared's output to keep for a failure report.
TAIL_LINES = 12


def _write_origin(origin: str) -> None:
    ORIGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename. An upload reading this file mid-write would otherwise
    # build every media URL in a post out of half a hostname.
    temp = ORIGIN_FILE.with_suffix(".tmp")
    temp.write_text(origin + "\n", encoding="utf-8")
    temp.replace(ORIGIN_FILE)
    log.info("tunnel origin published: %s", origin)


def clear_origin() -> None:
    """Drop the origin so uploads fall back to the configured base.

    A stale origin is worse than none: it names a host that resolves to
    nothing, which Instagram reports as a media-type error.
    """
    try:
        ORIGIN_FILE.unlink()
        log.info("tunnel origin cleared")
    except FileNotFoundError:
        pass


async def _run_once(stop: asyncio.Event) -> None:
    """Run one cloudflared process until it or ``stop`` ends."""
    process = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--url", LOCAL_TARGET, "--no-autoupdate",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    log.info("cloudflared started (pid %s) fronting %s", process.pid, LOCAL_TARGET)
    seen = False

    # cloudflared's own output is the only account of why it failed, and
    # discarding it left "exited without ever printing a hostname" as the
    # entire diagnosis. Keep a short tail to log if that happens.
    tail: list[str] = []

    async def read() -> None:
        nonlocal seen
        assert process.stdout is not None
        async for line in process.stdout:
            text = line.decode(errors="replace").rstrip()
            tail.append(text)
            del tail[:-TAIL_LINES]
            match = _ORIGIN_RE.search(line)
            if match and not seen:
                seen = True
                _write_origin(match.group(0).decode())

    reader = asyncio.create_task(read())
    waiter = asyncio.create_task(process.wait())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            process.terminate()
            await waiter
    finally:
        for task in (reader, waiter, stopper):
            task.cancel()
        clear_origin()
    if not seen:
        log.error("cloudflared exited without ever printing a hostname; last "
                  "output:\n%s", "\n".join(tail) or "(nothing)")


async def supervise(stop: asyncio.Event) -> None:
    """Keep a quick tunnel up for the life of the process.

    Runs inside the pipeline rather than as its own launchd job: macOS blocks
    newly added background items until a human approves them in System
    Settings, so a second service silently exits 78 until someone notices.
    Tying the tunnel to the process that depends on it also means it cannot be
    running while the pipeline is not, or the reverse.
    """
    while not stop.is_set():
        try:
            await _run_once(stop)
        except FileNotFoundError:
            log.error("cloudflared is not installed; media URLs will use "
                      "R2_PUBLIC_BASE unchanged")
            return
        except asyncio.CancelledError:
            clear_origin()
            raise
        except Exception:
            log.exception("tunnel supervisor failed")
        if stop.is_set():
            break
        log.warning("tunnel down; restarting in %ss", RESTART_DELAY_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=RESTART_DELAY_S)
        except TimeoutError:
            pass
    clear_origin()
