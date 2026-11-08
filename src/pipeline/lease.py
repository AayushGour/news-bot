"""Worker lease (T348): at most one worker owns Telethon, the bot and the DB.

A heartbeat file, ``data/worker.lease``, names the live worker (host or
container). Two workers on one Telegram auth key risk a revoked session, and two
bot pollers fight over ``getUpdates``, so the lease is taken before either
starts. A lease not refreshed for ``LEASE_STALE_S`` is dead and may be taken
over. The owner re-reads the file every ``LEASE_HEARTBEAT_S``; if it no longer
names it, it stops itself (fencing). On the host, while a *container* holds a
fresh lease, any read-write ``Database.connect`` refuses (``host_fence``).

``python -m pipeline.lease --check`` is the container healthcheck.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import in_container, lease_path_from_env

log = logging.getLogger(__name__)

LEASE_HEARTBEAT_S = 30
LEASE_STALE_S = 120
#: A heartbeat further ahead of our clock than this is clock skew, not freshness.
LEASE_MAX_SKEW_S = 300
OWNER_HOST = "host"
OWNER_CONTAINER = "container"
EXIT_OK = 0
EXIT_HELD = 1
TMP_SUFFIX = ".tmp"


class LeaseHeld(RuntimeError):
    """Another worker holds a fresh lease."""


@dataclass(frozen=True)
class LeaseRecord:
    owner_kind: str
    instance_id: str
    hostname: str
    pid: int
    heartbeat_at: float


def read_lease(path: Path) -> LeaseRecord | None:
    """The lease on disk, or None if absent/unreadable/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LeaseRecord(
            owner_kind=str(data["owner_kind"]), instance_id=str(data["instance_id"]),
            hostname=str(data["hostname"]), pid=int(data["pid"]),
            heartbeat_at=float(data["heartbeat_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_fresh(record: LeaseRecord | None, now: float) -> bool:
    return record is not None and now - record.heartbeat_at < LEASE_STALE_S


def lease_is_live(path: Path, record: LeaseRecord | None, now: float) -> bool:
    """Fail-safe liveness for a lease written by *another* clock.

    heartbeat_at is stamped by the writer's clock; after a Mac sleep the Docker
    VM clock can lag, making a live container's lease look stale to the host.
    The file's mtime is stamped on the host side of the bind mount, so the lease
    is live if EITHER is fresh. A heartbeat more than ``LEASE_MAX_SKEW_S`` in the
    future is suspicious; it counts as live (never steal on doubt) and is logged.
    """
    if record is None:
        return False
    if record.heartbeat_at - now > LEASE_MAX_SKEW_S:
        log.warning("worker lease %s has a heartbeat %.0fs in the future (clock skew); "
                    "treating it as live - if its owner is gone, remove the file",
                    path, record.heartbeat_at - now)
        return True
    if is_fresh(record, now):
        return True
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < LEASE_STALE_S


def host_fence(path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
    """Refuse a read-write open on the host while a container's lease is fresh."""
    if in_container():
        return
    lease_file = path or lease_path_from_env()
    record = read_lease(lease_file)
    if lease_is_live(lease_file, record, clock()) and record.owner_kind == OWNER_CONTAINER:
        raise LeaseHeld(
            "the pipeline runs in Docker (a container worker holds a fresh lease); "
            "use `docker compose exec app ...` instead of opening the database on the host"
        )


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process in this pid namespace? (signal 0 probes only)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class WorkerLease:
    def __init__(self, path: Path | None = None, *,
                 clock: Callable[[], float] = time.time,
                 pid: int | None = None, hostname: str | None = None) -> None:
        self.path = path or lease_path_from_env()
        self._clock = clock
        self.instance_id = uuid.uuid4().hex
        self.owner_kind = OWNER_CONTAINER if in_container() else OWNER_HOST
        self._pid = os.getpid() if pid is None else pid
        self._hostname = socket.gethostname() if hostname is None else hostname

    def _payload(self) -> str:
        record = LeaseRecord(self.owner_kind, self.instance_id, self._hostname,
                             self._pid, self._clock())
        return json.dumps(asdict(record))

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp-then-rename: a reader never sees half a lease. The temp name is
        # per instance - the host and a container share ./data and must not
        # clobber each other's temp file.
        tmp = self.path.with_name(f"{self.path.name}.{self.instance_id}{TMP_SUFFIX}")
        try:
            tmp.write_text(self._payload(), encoding="utf-8")
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _create_exclusive(self) -> None:
        """Create the lease only if no file exists (O_EXCL): of two workers
        starting on an empty slot exactly one succeeds."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, self._payload().encode("utf-8"))
        finally:
            os.close(fd)

    def _is_dead_predecessor(self, current: LeaseRecord) -> bool:
        """A fresh lease whose owner cannot be alive: same kind and host, and its
        pid is gone (SIGKILL, launchd restart) or equals ours (a restarted
        container reuses its hostname and pid namespace, so the old incarnation
        is gone). Without this a hard-killed worker blocks its own restart
        for LEASE_STALE_S."""
        return (current.owner_kind == self.owner_kind
                and current.hostname == self._hostname
                and (current.pid == self._pid or not pid_alive(current.pid)))

    def acquire(self) -> None:
        """Take the lease or raise ``LeaseHeld`` (a fresh lease names someone else).

        Race note: an absent lease is claimed with O_EXCL (one winner). Taking
        over a stale or dead lease is a rename, so two simultaneous takers can
        both write; the re-read below makes the loser refuse, leaving a window of
        milliseconds that is closed before Telethon or the bot start.
        """
        current = read_lease(self.path)
        if (lease_is_live(self.path, current, self._clock()) and current.instance_id != self.instance_id
                and not self._is_dead_predecessor(current)):
            raise LeaseHeld(
                f"worker lease {self.path} is held by {current.owner_kind} "
                f"{current.hostname} pid {current.pid} (heartbeat "
                f"{self._clock() - current.heartbeat_at:.0f}s ago); refusing to start a second worker"
            )
        if self.path.exists():
            self._write()
        else:
            try:
                self._create_exclusive()
            except FileExistsError:
                raise LeaseHeld(f"lost the race for worker lease {self.path}") from None
        if not self.holds():
            raise LeaseHeld(f"lost the race for worker lease {self.path}")

    def holds(self) -> bool:
        current = read_lease(self.path)
        return current is not None and current.instance_id == self.instance_id

    def heartbeat(self) -> bool:
        """Refresh the lease. False = fenced: the file now names another worker.
        A missing file is not another owner, so it is rewritten."""
        current = read_lease(self.path)
        if current is not None and current.instance_id != self.instance_id:
            log.error("worker lease now held by %s %s; this worker must stop",
                      current.owner_kind, current.hostname)
            return False
        self._write()
        return True

    def release(self) -> None:
        """Remove the lease, but only if it still names this worker."""
        if self.holds():
            self.path.unlink(missing_ok=True)


SCRIPT_REFUSAL = ("the pipeline runs in Docker \u2014 use: "
                  "docker compose exec app python scripts/{name}.py \u2026")


def run_host_script(name: str, run: Callable[[], int | None]) -> int | None:
    """Run a host script's entry point; if the DB fence refuses it, print one
    clear line (no traceback) and return 1. The one place every script that
    opens the database wraps its ``asyncio.run`` / ``run_app`` call."""
    try:
        return run()
    except LeaseHeld:
        print(SCRIPT_REFUSAL.format(name=name))
        return EXIT_HELD


def check(path: Path | None = None, *, clock: Callable[[], float] = time.time) -> bool:
    """Healthcheck: a fresh lease naming this very container."""
    record = read_lease(path or lease_path_from_env())
    return (is_fresh(record, clock()) and record.owner_kind == OWNER_CONTAINER
            and record.hostname == socket.gethostname())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.lease")
    parser.add_argument("--check", action="store_true", required=True,
                        help="exit 0 if this container holds a fresh lease")
    parser.parse_args(argv)
    ok = check()
    print("lease ok" if ok else "lease missing, stale or held elsewhere")
    return EXIT_OK if ok else EXIT_HELD


if __name__ == "__main__":
    sys.exit(main())
