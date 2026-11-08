"""Hourly consistent database backups (T348; repurposed from the T340 snapshot).

SQLite's online backup API copies the live WAL database into a rollback-journal
(DELETE) file built under a temporary name and renamed into place, so a reader
or a restore sees a whole backup or none. The newest ``BACKUP_KEEP`` are kept.
Writing a DELETE-journal file onto the bind mount is fine: one writer, and
nothing opens it across the kernel boundary while live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger("pipeline.backup")

BACKUP_KEEP = 48
BACKUP_PREFIX = "app-"
BACKUP_SUFFIX = ".db"
BACKUP_TS_FORMAT = "%Y%m%dT%H%M"
#: First backup shortly after boot, so a worker that restarts often still backs up.
BACKUP_FIRST_RUN_S = 120
#: Seconds the backup waits on a locked source before giving up.
SOURCE_LOCK_TIMEOUT_S = 30
TMP_SUFFIX = ".tmp"
#: SQLite header bytes 18/19 (file format write/read version).
HEADER_VERSION_OFFSET = 18
HEADER_BYTES_NEEDED = 20
VERSION_ROLLBACK = 1
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class BackupError(RuntimeError):
    """A backup failed a post-condition. Internal; never shown to the operator."""


def _header_is_rollback(path: Path) -> bool:
    with open(path, "rb") as fh:
        header = fh.read(HEADER_BYTES_NEEDED)
    return len(header) == HEADER_BYTES_NEEDED and (
        header[HEADER_VERSION_OFFSET] == VERSION_ROLLBACK
        and header[HEADER_VERSION_OFFSET + 1] == VERSION_ROLLBACK
    )


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in SIDECAR_SUFFIXES]


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_backup(live: Path, out: Path) -> bool:
    """Copy ``live`` to ``out`` atomically. Never raises: a failure is logged
    (one line, no exception text) and ``out`` is not created. True on success."""
    tmp = out.with_name(out.name + TMP_SUFFIX)
    try:
        if live.resolve() == out.resolve():
            raise BackupError("backup path equals the live database")
        if not live.is_file():
            raise BackupError("live database missing")
        out.parent.mkdir(parents=True, exist_ok=True)
        for stale in (tmp, *_sidecars(tmp)):
            stale.unlink(missing_ok=True)

        with closing(sqlite3.connect(live, timeout=SOURCE_LOCK_TIMEOUT_S)) as src, \
                closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst, pages=-1)
            # The copied page 1 still says WAL; this rewrites it to rollback mode.
            mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise BackupError("could not leave WAL mode")
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupError("quick_check failed")

        # The backup connection opened the copy as WAL before we switched it, which
        # leaves an orphan -shm behind. With no -wal and a rollback header it holds
        # nothing; a surviving -wal would hold pages, so that stays a hard failure.
        tmp.with_name(tmp.name + "-shm").unlink(missing_ok=True)
        if not _header_is_rollback(tmp):
            raise BackupError("backup header is not rollback mode")
        if any(p.exists() for p in _sidecars(tmp)):
            raise BackupError("backup left a sidecar file")
        _fsync_path(tmp)
        os.replace(tmp, out)
        _fsync_path(out.parent)
        return True
    except Exception as exc:  # noqa: BLE001 - a periodic job must not die
        log.warning("database backup failed (%s); keeping the previous ones", type(exc).__name__)
        for stale in (tmp, *_sidecars(tmp)):
            with suppress(OSError):
                stale.unlink(missing_ok=True)
        return False


def prune_backups(backup_dir: Path, keep: int = BACKUP_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` ``app-<ts>.db`` files; return the deleted.
    Timestamps sort lexically, so the name is the age. Other files are never touched."""
    found = sorted(
        p for p in backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file())
    doomed = found[:-keep] if keep > 0 else found
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


def run_backup(db_path: Path, backup_dir: Path, *, now: datetime | None = None,
               keep: int = BACKUP_KEEP) -> Path | None:
    stamp = (now or datetime.now(UTC)).strftime(BACKUP_TS_FORMAT)
    out = backup_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    if not write_backup(db_path, out):
        return None
    prune_backups(backup_dir, keep)
    return out


async def backup_job(settings: Settings) -> None:
    """The ``_periodic`` body: run the blocking copy off the event loop."""
    await asyncio.to_thread(run_backup, settings.db_path, settings.backup_dir)
