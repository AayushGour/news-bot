"""Retention: reclaim disk without losing what the pipeline still needs (T241).

Two halves, kept apart on purpose:

* **Selection** (pure): given a snapshot of items, a clock and a policy, say
  which items to auto-reject and which to purge files for, and why. No I/O.
* **Execution**: delete files, delete bucket objects, move the DB row. Every
  delete is contained to ``settings.media_dir`` / the item's own bucket prefix.

Files go, rows stay. The row is what dedupe (``text_hash``) and publish history
(``publish_log``, ``ig_post_id``) rely on; deleting it would let the same post
be ingested again.

The 120-day clock is ``status_updated_at``. ``Database.transition`` writes it in
the same transaction as the event row on every status change, and nothing else
touches it while an item sits in awaiting_approval, so it is exactly "since the
item last entered awaiting_approval". A regen cycle that returns the item there
restarts it - the operator is looking at a new preview, so that is honest.
(``events`` would give the same answer at the cost of a join.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import RETENTION_S3_CONNECT_TIMEOUT_S, RETENTION_S3_READ_TIMEOUT_S
from .models import TERMINAL, Status
from .publish.media_host import _client, item_prefix

log = logging.getLogger(__name__)

AUTO_REJECT_REASON = "auto-rejected: awaiting approval > {days} days"
#: Shared flat directories under media_dir. Never deletable as a whole - only
#: an item's own entries inside them.
SHARED_MEDIA_DIRS = frozenset({"channel", "inbox", "researched", "logos"})
#: Per-item directories under media_dir (render output at media/<id>, plus the
#: research and logo downloads keyed by item id). These item dirs are rmtree'd
#: without the shared-dir check in ``contained`` on purpose: only the owning
#: item ever writes there (render.py, research.py, enumerate_items.py), and
#: ``contained`` still pins them inside media_dir.
ITEM_DIR_PARENTS = ("", "researched", "logos")
#: S3 DeleteObjects accepts at most 1000 keys per call (platform limit).
S3_DELETE_BATCH = 1000
#: Statuses that are never selected for anything: in flight, or waiting on a human.
NEVER_TOUCH = frozenset(set(Status) - TERMINAL - {Status.AWAITING_APPROVAL})


# ------------------------------------------------------------------ selection


@dataclass(frozen=True)
class Policy:
    awaiting_max_days: int
    terminal_grace_days: int
    failed_grace_days: int
    purge_published: bool
    purge_remote: bool

    @classmethod
    def from_settings(cls, s: Any) -> Policy:
        return cls(
            awaiting_max_days=s.awaiting_approval_max_days,
            terminal_grace_days=s.terminal_media_grace_days,
            failed_grace_days=s.failed_media_grace_days,
            purge_published=s.cleanup_published_media,
            purge_remote=s.cleanup_minio,
        )


@dataclass(frozen=True)
class Candidate:
    """The slice of an item retention needs - a snapshot, not a live row."""

    id: int
    status: Status
    status_updated_at: str | None
    ig_post_id: str | None = None
    raw_media_paths: tuple[str, ...] = ()
    rendered_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    item_id: int
    status: Status
    status_updated_at: str | None
    reject_reason: str | None  # set => auto-reject first, then purge
    purge_local: bool
    purge_remote: bool
    why: str


def parse_ts(value: str | None) -> datetime | None:
    """Unparseable or missing means "unknown age" - and unknown is never eligible."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def plan_item(c: Candidate, now: datetime, policy: Policy) -> Plan | None:
    """What (if anything) retention may do to one item. Pure."""
    if c.status in NEVER_TOUCH:
        return None  # approved/publishing/needs_input/in-flight stages
    since = parse_ts(c.status_updated_at)
    if since is None:
        return None
    age = now - since

    if c.status is Status.AWAITING_APPROVAL:
        limit = policy.awaiting_max_days
        if limit <= 0 or age <= timedelta(days=limit):
            return None
        return Plan(
            c.id, c.status, c.status_updated_at,
            AUTO_REJECT_REASON.format(days=limit), True, policy.purge_remote,
            f"awaiting approval {age.days}d > {limit}d",
        )

    if c.status is Status.FAILED:
        grace, local, remote = policy.failed_grace_days, True, policy.purge_remote
    elif c.status is Status.PUBLISHED:
        # Only once publishing is confirmed complete: Instagram fetches the
        # bucket objects asynchronously until the post exists.
        if not c.ig_post_id:
            return None
        grace = policy.terminal_grace_days
        local, remote = policy.purge_published, policy.purge_remote
    else:  # rejected, dropped
        grace, local, remote = policy.terminal_grace_days, True, policy.purge_remote

    if age < timedelta(days=grace) or not (local or remote):
        return None
    return Plan(c.id, c.status, c.status_updated_at, None, local, remote,
                f"{c.status.value} {age.days}d >= grace {grace}d")


def select(cands: Iterable[Candidate], now: datetime, policy: Policy) -> list[Plan]:
    return [p for c in cands if (p := plan_item(c, now, policy))]


# ---------------------------------------------------------------- containment


def contained(path: str | Path, root: str | Path) -> Path | None:
    """The resolved path if it is safely inside ``root``, else None.

    Refuses: relative paths (ambiguous), symlinks (we never follow one out),
    anything resolving outside root (``..``, a symlinked parent), root itself,
    and the shared directories as a whole.
    """
    root_r = Path(root).resolve()
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        return None
    r = p.resolve()
    if r == root_r or not r.is_relative_to(root_r):
        return None
    if r.parent == root_r and r.name in SHARED_MEDIA_DIRS:
        return None
    return r


def item_dirs(item_id: int, media_root: Path) -> list[Path]:
    return [Path(media_root, parent, str(int(item_id))) for parent in ITEM_DIR_PARENTS]


def _measure(path: Path) -> tuple[int, int]:
    """(files, bytes) under path without following links."""
    if path.is_symlink() or not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    files = size = 0
    for dirpath, _, names in os.walk(path, followlinks=False):
        for n in names:
            try:
                st = os.lstat(os.path.join(dirpath, n))
            except OSError:
                continue
            files += 1
            size += st.st_size
    return files, size


# ------------------------------------------------------------------ execution


class RemoteStore(Protocol):
    def purge_item(self, item_id: int, apply: bool) -> tuple[int, int]:
        """(objects, bytes) under the item's prefix; deletes them when ``apply``."""


class RemoteDeleteError(RuntimeError):
    """The bucket accepted the request but refused some keys."""


class S3Store:
    """The pipeline's bucket, addressed only through ``items/<id>/``."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client, self.bucket = client, bucket

    def purge_item(self, item_id: int, apply: bool) -> tuple[int, int]:
        prefix = item_prefix(item_id)
        keys: list[dict] = []
        failed: list[str] = []
        total = 0
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                if not obj["Key"].startswith(prefix):
                    raise RuntimeError(f"listing returned {obj['Key']!r} outside {prefix!r}")
                keys.append({"Key": obj["Key"]})
                total += obj.get("Size", 0)
        if apply:
            for i in range(0, len(keys), S3_DELETE_BATCH):
                resp = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": keys[i:i + S3_DELETE_BATCH], "Quiet": True})
                # S3/MinIO report per-key failures in the body without raising.
                failed.extend(f"{e.get('Key')}: {e.get('Code')}"
                              for e in (resp or {}).get("Errors", []))
        if failed:
            raise RemoteDeleteError(
                f"{len(failed)} of {len(keys)} object(s) not deleted: {'; '.join(failed[:3])}")
        return len(keys), total


def build_store(settings: Any) -> RemoteStore | None:
    if not (settings.r2_bucket and settings.r2_access_key and settings.r2_secret_key
            and (settings.s3_endpoint or settings.r2_account_id)):
        return None
    return S3Store(_client(settings, connect_timeout=RETENTION_S3_CONNECT_TIMEOUT_S,
                           read_timeout=RETENTION_S3_READ_TIMEOUT_S), settings.r2_bucket)


@dataclass
class Report:
    apply: bool
    rejected: list[int] = field(default_factory=list)
    purged_items: int = 0
    files: int = 0
    local_bytes: int = 0
    objects: int = 0
    remote_bytes: int = 0
    refused: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raced: list[int] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    needs_input: int = 0
    awaiting: int = 0
    oldest_awaiting_days: int | None = None

    @property
    def total_bytes(self) -> int:
        return self.local_bytes + self.remote_bytes

    @property
    def did_something(self) -> bool:
        return bool(self.rejected or self.files or self.objects)

    def summary(self) -> str:
        verb = "deleted" if self.apply else "would delete"
        rej = "auto-rejected" if self.apply else "would auto-reject"
        return (f"retention: {rej} {len(self.rejected)}; {verb} {self.files} file(s) "
                f"for {self.purged_items} item(s) ({human_size(self.local_bytes)}) and "
                f"{self.objects} bucket object(s) ({human_size(self.remote_bytes)}); "
                f"refused {len(self.refused)}, errors {len(self.errors)}")


def human_size(size: int) -> str:
    n = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def _json_list(raw: str | None) -> tuple[str, ...]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    return tuple(str(x) for x in v) if isinstance(v, list) else ()


async def load_candidates(db: Any) -> list[Candidate]:
    rows = await db.conn.execute_fetchall(
        "SELECT id, status, status_updated_at, ig_post_id, raw_media_paths,"
        " rendered_paths FROM items")
    out = []
    for r in rows:
        try:
            status = Status(r["status"])
        except ValueError:
            continue  # an unknown status is not ours to judge
        out.append(Candidate(
            r["id"], status, r["status_updated_at"], r["ig_post_id"],
            _json_list(r["raw_media_paths"]), _json_list(r["rendered_paths"])))
    return out


def _targets(c: Candidate, media_root: Path, protected: set[str],
             report: Report) -> list[Path]:
    """Deletable local paths for one item, containment-checked and de-shared."""
    out: list[Path] = []
    for d in item_dirs(c.id, media_root):
        ok = contained(d, media_root)
        if ok is None:
            if d.exists() or d.is_symlink():
                report.refused.append(f"item {c.id}: {d}")
        else:
            out.append(ok)
    for raw in (*c.raw_media_paths, *c.rendered_paths):
        ok = contained(raw, media_root)
        if ok is None:
            report.refused.append(f"item {c.id}: {raw}")
        elif str(ok) in protected:
            continue  # another live item points at the same file
        elif any(ok.is_relative_to(d) for d in out):
            continue  # already covered by an item dir; counting it twice lies
        elif ok.is_dir():
            report.refused.append(f"item {c.id}: {raw} (a directory)")
        else:
            out.append(ok)
    return out


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        import shutil

        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


async def _moved_since_snapshot(db: Any, plan: Plan) -> bool:
    """True if the item's status or clock changed after the plan was made."""
    rows = await db.conn.execute_fetchall(
        "SELECT status, status_updated_at FROM items WHERE id=?", (plan.item_id,))
    return not rows or (rows[0]["status"], rows[0]["status_updated_at"]) != (
        plan.status.value, plan.status_updated_at)


async def run_retention(
    db: Any, settings: Any, *, apply: bool, now: datetime | None = None,
    store: RemoteStore | None = None,
) -> Report:
    """Select, then (when ``apply``) act. Idempotent: a second run finds nothing."""
    now = now or datetime.now(timezone.utc)
    policy = Policy.from_settings(settings)
    media_root = Path(settings.media_dir)
    report = Report(apply=apply)
    cands = await load_candidates(db)
    plans = select(cands, now, policy)
    by_id = {c.id: c for c in cands}
    if store is None and policy.purge_remote:
        store = build_store(settings)

    awaiting = [parse_ts(c.status_updated_at) for c in cands
                if c.status is Status.AWAITING_APPROVAL]
    ages = [(now - t).days for t in awaiting if t]
    report.awaiting, report.oldest_awaiting_days = len(awaiting), max(ages, default=None)
    report.needs_input = sum(c.status is Status.NEEDS_INPUT for c in cands)

    planned = {p.item_id for p in plans}
    protected = {str(r) for c in cands if c.id not in planned
                 for raw in (*c.raw_media_paths, *c.rendered_paths)
                 if (r := contained(raw, media_root))}

    for plan in plans:
        c = by_id[plan.item_id]
        files = nbytes = objs = obytes = 0
        try:
            if plan.reject_reason:
                if apply and not await db.transition_if(
                        c.id, plan.status, plan.status_updated_at, Status.REJECTED,
                        {"last_error": plan.reject_reason}, detail=plan.reject_reason):
                    report.raced.append(c.id)  # moved under us: leave it entirely alone
                    continue
                report.rejected.append(c.id)

            elif await _moved_since_snapshot(db, plan):
                # Another process (the dashboard) requeued/changed it mid-sweep.
                report.raced.append(c.id)
                continue

            if plan.purge_local:
                for path in _targets(c, media_root, protected, report):
                    f, b = _measure(path)
                    if apply:
                        await asyncio.to_thread(_delete, path)
                    files, nbytes = files + f, nbytes + b
        except Exception as exc:  # one item's failure must not stop the sweep
            report.errors.append(f"item {plan.item_id}: {type(exc).__name__}: {exc}")
            log.exception("retention failed for item %s", plan.item_id)
            continue
        if plan.purge_remote and store is not None:
            try:
                objs, obytes = await asyncio.to_thread(store.purge_item, c.id, apply)
            except Exception as exc:  # local work above is still reported
                report.errors.append(f"item {plan.item_id}: {type(exc).__name__}: {exc}")
                log.exception("retention bucket purge failed for item %s", plan.item_id)

        if files or objs or plan.reject_reason:
            report.purged_items += bool(files or objs)
            report.files += files
            report.local_bytes += nbytes
            report.objects += objs
            report.remote_bytes += obytes
            report.lines.append(
                f"item {c.id} [{plan.status.value}] {plan.why}: {files} file(s) "
                f"{human_size(nbytes)}, {objs} object(s) {human_size(obytes)}")
    return report


async def retention_job(bot: Any, db: Any, settings: Any) -> Report | None:
    """The worker's periodic task. Logs every run that acts; one Telegram summary."""
    if not settings.retention_enabled:
        return None
    report = await run_retention(db, settings, apply=not settings.retention_dry_run)
    for line in report.lines:
        log.info("%s", line)
    for line in report.refused:
        log.warning("retention refused (outside media_dir / symlink): %s", line)
    for line in report.errors:
        log.error("retention error: %s", line)
    if report.did_something or report.errors or report.refused:
        log.info("%s", report.summary())
    if report.did_something and report.apply and bot is not None:
        try:
            await bot.send_message(settings.operator_user_id, report.summary())
        except Exception:
            log.exception("could not send the retention summary")
    return report
