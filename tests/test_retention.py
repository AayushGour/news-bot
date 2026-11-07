"""Retention (T241): what may be deleted, when, and that nothing else is.

The pure selection tests pin the policy per status and the 120-day boundary;
the execution tests run against a tmp media dir and a real sqlite file, with a
fake bucket, and check containment, idempotence and that live items survive.
"""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import Settings
from pipeline.models import Status
from pipeline.retention import (
    Candidate,
    Policy,
    contained,
    plan_item,
    retention_job,
    run_retention,
    select,
)
from tests.conftest import TEST_ENV

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
POLICY = Policy(awaiting_max_days=120, terminal_grace_days=1, failed_grace_days=30,
                purge_published=True, purge_remote=True)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def cand(status, since, **kw) -> Candidate:
    return Candidate(1, status, since, **kw)


# ------------------------------------------------------- selection (pure)


def test_awaiting_approval_boundary_is_strictly_over_120_days():
    at = plan_item(cand(Status.AWAITING_APPROVAL, ago(days=120)), NOW, POLICY)
    over = plan_item(cand(Status.AWAITING_APPROVAL, ago(days=120, seconds=1)), NOW, POLICY)
    under = plan_item(cand(Status.AWAITING_APPROVAL, ago(days=119)), NOW, POLICY)
    assert at is None and under is None
    assert over and over.reject_reason == "auto-rejected: awaiting approval > 120 days"
    assert over.purge_local


def test_awaiting_rule_off_when_max_days_is_zero():
    off = Policy(0, 1, 30, True, True)
    assert plan_item(cand(Status.AWAITING_APPROVAL, ago(days=999)), NOW, off) is None


@pytest.mark.parametrize("status", [
    Status.INGESTED, Status.TRIAGED, Status.EXTRACTED, Status.RESEARCHED,
    Status.SYNTHESIZED, Status.COMPOSED, Status.RENDERED, Status.NEEDS_INPUT,
    Status.APPROVED, Status.PUBLISHING,
])
def test_in_flight_and_waiting_statuses_are_never_touched(status):
    """approved/publishing: Instagram may still be fetching. needs_input: a human."""
    assert plan_item(cand(status, ago(days=9999), ig_post_id="x"), NOW, POLICY) is None


def test_published_needs_ig_post_id_and_grace():
    assert plan_item(cand(Status.PUBLISHED, ago(days=5)), NOW, POLICY) is None
    assert plan_item(cand(Status.PUBLISHED, ago(hours=23), ig_post_id="9"), NOW, POLICY) is None
    p = plan_item(cand(Status.PUBLISHED, ago(days=1), ig_post_id="9"), NOW, POLICY)
    assert p and p.purge_local and p.purge_remote and not p.reject_reason


def test_published_local_and_remote_toggles_are_independent():
    keep_local = Policy(120, 1, 30, False, True)
    p = plan_item(cand(Status.PUBLISHED, ago(days=3), ig_post_id="9"), NOW, keep_local)
    assert p and not p.purge_local and p.purge_remote
    nothing = Policy(120, 1, 30, False, False)
    assert plan_item(cand(Status.PUBLISHED, ago(days=3), ig_post_id="9"), NOW, nothing) is None


@pytest.mark.parametrize("status", [Status.REJECTED, Status.DROPPED])
def test_rejected_and_dropped_purge_after_grace(status):
    assert plan_item(cand(status, ago(hours=1)), NOW, POLICY) is None
    assert plan_item(cand(status, ago(days=2)), NOW, POLICY)


def test_failed_keeps_inputs_for_the_requeue_grace_period():
    """A failed item can be requeued to ingested, which re-reads the download."""
    assert plan_item(cand(Status.FAILED, ago(days=29)), NOW, POLICY) is None
    assert plan_item(cand(Status.FAILED, ago(days=30)), NOW, POLICY)


def test_requeued_item_is_judged_by_its_current_status_and_clock():
    """A requeue resets status_updated_at AND moves the status: an old failed
    item requeued to composed is in flight and untouched; one that comes back
    to awaiting_approval starts a fresh 120 days."""
    assert plan_item(cand(Status.COMPOSED, ago(days=400)), NOW, POLICY) is None
    assert plan_item(cand(Status.AWAITING_APPROVAL, ago(days=2)), NOW, POLICY) is None


@pytest.mark.parametrize("bad", [None, "", "not-a-date"])
def test_unknown_age_is_never_eligible(bad):
    assert plan_item(cand(Status.AWAITING_APPROVAL, bad), NOW, POLICY) is None
    assert plan_item(cand(Status.REJECTED, bad), NOW, POLICY) is None


def test_naive_timestamp_is_read_as_utc():
    naive = (NOW - timedelta(days=121)).replace(tzinfo=None).isoformat()
    assert plan_item(cand(Status.AWAITING_APPROVAL, naive), NOW, POLICY)


def test_select_returns_only_eligible():
    cs = [Candidate(1, Status.REJECTED, ago(days=3)),
          Candidate(2, Status.APPROVED, ago(days=300)),
          Candidate(3, Status.AWAITING_APPROVAL, ago(days=121))]
    assert [p.item_id for p in select(cs, NOW, POLICY)] == [1, 3]


# ------------------------------------------------------------- config


def test_config_defaults_and_overrides(tmp_path):
    d = Settings.load(env=TEST_ENV)
    assert (d.retention_enabled, d.retention_dry_run) == (True, False)
    assert d.awaiting_approval_max_days == 120
    assert d.cleanup_interval_s == 6 * 3600
    assert (d.terminal_media_grace_days, d.failed_media_grace_days) == (1, 30)
    assert d.cleanup_published_media and d.cleanup_minio
    o = Settings.load(env={
        **TEST_ENV, "RETENTION_ENABLED": "false", "RETENTION_DRY_RUN": "true",
        "AWAITING_APPROVAL_MAX_DAYS": "30", "CLEANUP_INTERVAL_S": "900",
        "TERMINAL_MEDIA_GRACE_DAYS": "7", "FAILED_MEDIA_GRACE_DAYS": "60",
        "CLEANUP_PUBLISHED_MEDIA": "false", "CLEANUP_MINIO": "false"})
    assert (o.retention_enabled, o.retention_dry_run) == (False, True)
    assert o.awaiting_approval_max_days == 30 and o.cleanup_interval_s == 900
    assert (o.terminal_media_grace_days, o.failed_media_grace_days) == (7, 60)
    assert not o.cleanup_published_media and not o.cleanup_minio


def test_config_clamps_nonsense():
    s = Settings.load(env={**TEST_ENV, "CLEANUP_INTERVAL_S": "1",
                           "AWAITING_APPROVAL_MAX_DAYS": "-5", "FAILED_MEDIA_GRACE_DAYS": "x"})
    assert s.cleanup_interval_s >= 60
    assert s.awaiting_approval_max_days == 0
    assert s.failed_media_grace_days == 30


# ------------------------------------------------------------ containment


def test_contained_accepts_inside_and_refuses_the_rest(tmp_path):
    root = tmp_path / "media"
    (root / "channel").mkdir(parents=True)
    f = root / "channel" / "a.jpg"
    f.write_bytes(b"x")
    outside = tmp_path / "secret.txt"
    outside.write_text("keep")
    assert contained(f, root) == f.resolve()
    assert contained(root / "channel" / ".." / ".." / "secret.txt", root) is None
    assert contained(outside, root) is None
    assert contained(root, root) is None
    assert contained(root / "channel", root) is None  # a shared dir, never whole
    assert contained("relative/path.jpg", root) is None


def test_contained_refuses_symlink_file_and_symlinked_parent(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.jpg").write_bytes(b"x")
    (root / "link.jpg").symlink_to(outside / "x.jpg")
    (root / "42").symlink_to(outside, target_is_directory=True)
    assert contained(root / "link.jpg", root) is None
    assert contained(root / "42", root) is None
    assert contained(root / "42" / "x.jpg", root) is None


# --------------------------------------------------------- execution


class FakeStore:
    def __init__(self, objects: dict[int, int] | None = None):
        self.objects = dict(objects or {})
        self.deleted: list[int] = []

    def purge_item(self, item_id, apply):
        n = self.objects.get(item_id, 0)
        if apply and n:
            self.deleted.append(item_id)
            self.objects[item_id] = 0
        return n, n * 100


@pytest.fixture
def env(tmp_path, db):
    media = tmp_path / "media"
    (media / "channel").mkdir(parents=True)
    s = Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "t.db")})
    return replace(s, media_dir=media), db, media


async def add(db, media, n, status, since, *, ig=None, with_files=True):
    """An item with a raw download in channel/, a render dir and a research dir."""
    item_id = await db.insert_item(source="channel", source_chat_id=1,
                                   source_msg_id=n, raw_text=f"t{n}")
    raw = media / "channel" / f"raw{n}.jpg"
    render = media / str(item_id) / "slide_01.png"
    research = media / "researched" / str(item_id) / "bg.jpg"
    if with_files:
        for p in (raw, render, research):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 10)
    await db.conn.execute(
        "UPDATE items SET status=?, status_updated_at=?, ig_post_id=?,"
        " raw_media_paths=?, rendered_paths=? WHERE id=?",
        (status.value, since, ig, json.dumps([str(raw)]), json.dumps([str(render)]),
         item_id))
    await db.conn.commit()
    return item_id, raw, render, research


async def test_terminal_items_purged_rows_kept_live_items_untouched(env):
    s, db, media = env
    rej, *rej_files = await add(db, media, 1, Status.REJECTED, ago(days=3))
    pub, *pub_files = await add(db, media, 2, Status.PUBLISHED, ago(days=3), ig="ig1")
    live, *live_files = await add(db, media, 3, Status.PUBLISHING, ago(days=300))
    appr, *appr_files = await add(db, media, 4, Status.APPROVED, ago(days=300))
    need, *need_files = await add(db, media, 5, Status.NEEDS_INPUT, ago(days=300))
    store = FakeStore({rej: 2, pub: 3, live: 4, appr: 4})

    rep = await run_retention(db, s, apply=True, now=NOW, store=store)

    assert all(not p.exists() for p in rej_files + pub_files)
    assert all(p.exists() for p in live_files + appr_files + need_files)
    assert sorted(store.deleted) == sorted([rej, pub])  # never the live ones
    assert rep.files == 6 and rep.local_bytes == 60
    assert rep.objects == 5 and rep.remote_bytes == 500
    assert (media / "channel").is_dir()
    rows = {r["id"] for r in await db.conn.execute_fetchall("SELECT id FROM items")}
    assert rows == {rej, pub, live, appr, need}  # rows survive


async def test_report_only_changes_nothing_but_counts_the_same(env):
    s, db, media = env
    rej, *files = await add(db, media, 1, Status.REJECTED, ago(days=3))
    store = FakeStore({rej: 2})
    rep = await run_retention(db, s, apply=False, now=NOW, store=store)
    assert all(p.exists() for p in files)
    assert store.deleted == []
    assert rep.files == 3 and rep.objects == 2


async def test_auto_reject_after_120_days_keeps_row_and_records_reason(env):
    s, db, media = env
    old, *old_files = await add(db, media, 1, Status.AWAITING_APPROVAL, ago(days=121))
    fresh, *fresh_files = await add(db, media, 2, Status.AWAITING_APPROVAL, ago(days=119))
    rep = await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert rep.rejected == [old]
    item = await db.get_item(old)
    assert item.status is Status.REJECTED
    assert item.last_error == "auto-rejected: awaiting approval > 120 days"
    assert all(not p.exists() for p in old_files)
    assert (await db.get_item(fresh)).status is Status.AWAITING_APPROVAL
    assert all(p.exists() for p in fresh_files)
    ev = await db.events_for(old)
    assert ev[-1]["to_status"] == "rejected" and "auto-rejected" in ev[-1]["detail"]


async def test_report_only_does_not_reject(env):
    s, db, media = env
    old, *files = await add(db, media, 1, Status.AWAITING_APPROVAL, ago(days=500))
    rep = await run_retention(db, s, apply=False, now=NOW, store=FakeStore())
    assert rep.rejected == [old]
    assert (await db.get_item(old)).status is Status.AWAITING_APPROVAL
    assert all(p.exists() for p in files)


async def test_operator_approving_mid_run_wins_the_race(env):
    """The decision is made on a snapshot; transition_if must not clobber an approval."""
    s, db, media = env
    old, *files = await add(db, media, 1, Status.AWAITING_APPROVAL, ago(days=500))
    snapshot_ts = (await db.get_item(old)).status_updated_at
    await db.transition(old, Status.APPROVED)
    ok = await db.transition_if(old, Status.AWAITING_APPROVAL, snapshot_ts,
                                Status.REJECTED, {"last_error": "x"})
    assert ok is False
    assert (await db.get_item(old)).status is Status.APPROVED
    assert len(await db.events_for(old)) == 1  # no phantom event


async def test_run_retention_skips_an_item_approved_after_the_snapshot(env, monkeypatch):
    """Sweep-level: stale snapshot says 500d awaiting, operator approves meanwhile."""
    import pipeline.retention as ret

    s, db, media = env
    old, *files = await add(db, media, 1, Status.AWAITING_APPROVAL, ago(days=500))
    store = FakeStore({old: 2})
    real = ret.load_candidates

    async def stale(d):
        snap = await real(d)
        await d.transition(old, Status.APPROVED)  # after the snapshot was taken
        return snap

    monkeypatch.setattr(ret, "load_candidates", stale)
    rep = await run_retention(db, s, apply=True, now=NOW, store=store)
    assert rep.raced == [old] and rep.rejected == []
    assert all(p.exists() for p in files)
    assert store.deleted == [] and store.objects[old] == 2
    assert (await db.get_item(old)).status is Status.APPROVED


async def test_run_retention_keeps_files_of_a_failed_item_requeued_mid_sweep(env, monkeypatch):
    import pipeline.retention as ret

    s, db, media = env
    f, *files = await add(db, media, 1, Status.FAILED, ago(days=40))
    store = FakeStore({f: 2})
    real = ret.load_candidates

    async def stale(d):
        snap = await real(d)
        await d.transition(f, Status.INGESTED)  # dashboard requeue, other process
        return snap

    monkeypatch.setattr(ret, "load_candidates", stale)
    rep = await run_retention(db, s, apply=True, now=NOW, store=store)
    assert rep.raced == [f] and rep.errors == [] and rep.files == 0
    assert all(p.exists() for p in files)
    assert store.deleted == []


async def test_regen_cycle_back_to_awaiting_is_a_different_state(env):
    s, db, media = env
    old, *_ = await add(db, media, 1, Status.AWAITING_APPROVAL, ago(days=500))
    stale = ago(days=500)
    await db.transition(old, Status.AWAITING_APPROVAL)  # fresh clock
    assert not await db.transition_if(old, Status.AWAITING_APPROVAL, stale,
                                      Status.REJECTED)


async def test_idempotent_second_run_deletes_nothing(env):
    s, db, media = env
    await add(db, media, 1, Status.REJECTED, ago(days=3))
    await add(db, media, 2, Status.AWAITING_APPROVAL, ago(days=200))
    store = FakeStore({1: 1, 2: 1})
    first = await run_retention(db, s, apply=True, now=NOW, store=store)
    second = await run_retention(db, s, apply=True, now=NOW, store=store)
    assert first.did_something
    assert not second.did_something
    assert second.files == 0 and second.objects == 0 and second.rejected == []


async def test_missing_files_are_fine(env):
    s, db, media = env
    await add(db, media, 1, Status.DROPPED, ago(days=3), with_files=False)
    rep = await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert rep.errors == [] and rep.files == 0


async def test_failed_item_keeps_files_inside_grace_then_loses_them(env):
    s, db, media = env
    f, *files = await add(db, media, 1, Status.FAILED, ago(days=10))
    await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert all(p.exists() for p in files)
    await db.conn.execute("UPDATE items SET status_updated_at=? WHERE id=?",
                          (ago(days=31), f))
    await db.conn.commit()
    await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert all(not p.exists() for p in files)


async def test_path_outside_media_dir_in_the_row_is_refused(env, tmp_path):
    s, db, media = env
    outside = tmp_path / "precious.txt"
    outside.write_text("keep")
    link_target = tmp_path / "elsewhere"
    link_target.mkdir()
    (link_target / "v.png").write_text("keep")
    rid, *_ = await add(db, media, 1, Status.REJECTED, ago(days=3))
    (media / "channel" / "sym.jpg").symlink_to(outside)
    # render dir is a symlink to a directory outside media
    import shutil
    shutil.rmtree(media / str(rid))
    (media / str(rid)).symlink_to(link_target, target_is_directory=True)
    await db.conn.execute(
        "UPDATE items SET raw_media_paths=?, rendered_paths=? WHERE id=?",
        (json.dumps([str(outside), str(media / "channel" / "sym.jpg"),
                     str(media / "channel" / ".." / ".." / "precious.txt"),
                     str(media / "channel")]),
         json.dumps([str(link_target / "v.png")]), rid))
    await db.conn.commit()
    rep = await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert outside.read_text() == "keep"
    assert (link_target / "v.png").read_text() == "keep"
    assert (media / "channel").is_dir()
    assert len(rep.refused) >= 5


async def test_file_shared_with_a_live_item_is_not_deleted(env):
    s, db, media = env
    rid, raw, *_ = await add(db, media, 1, Status.REJECTED, ago(days=3))
    live, *_ = await add(db, media, 2, Status.RENDERED, ago(days=3))
    await db.conn.execute("UPDATE items SET raw_media_paths=? WHERE id=?",
                          (json.dumps([str(raw)]), live))
    await db.conn.commit()
    await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert raw.exists()


async def test_published_local_kept_when_toggle_off(env):
    s, db, media = env
    s = replace(s, cleanup_published_media=False)
    pub, *files = await add(db, media, 1, Status.PUBLISHED, ago(days=3), ig="1")
    store = FakeStore({pub: 2})
    await run_retention(db, s, apply=True, now=NOW, store=store)
    assert all(p.exists() for p in files)
    assert store.deleted == [pub]


async def test_one_bad_item_does_not_stop_the_sweep(env):
    s, db, media = env
    a, *_ = await add(db, media, 1, Status.REJECTED, ago(days=3))
    b, *bfiles = await add(db, media, 2, Status.REJECTED, ago(days=3))

    class Flaky(FakeStore):
        def purge_item(self, item_id, apply):
            if item_id == a:
                raise RuntimeError("bucket down")
            return super().purge_item(item_id, apply)

    store = Flaky({b: 1})
    rep = await run_retention(db, s, apply=True, now=NOW, store=store)
    assert len(rep.errors) == 1 and "bucket down" in rep.errors[0]
    assert store.deleted == [b] and all(not p.exists() for p in bfiles)


# ------------------------------------------------------------- the job


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text):
        self.sent.append((chat, text))


async def test_job_logs_notifies_once_and_respects_enabled(env, caplog):
    s, db, media = env
    await add(db, media, 1, Status.REJECTED, ago(days=3))
    s = replace(s, cleanup_minio=False)
    bot = FakeBot()
    with caplog.at_level(logging.INFO, logger="pipeline.retention"):
        await retention_job(bot, db, s)
    assert len(bot.sent) == 1 and bot.sent[0][0] == s.operator_user_id
    assert "deleted 3 file(s)" in bot.sent[0][1]
    assert any("bytes" not in r.message and "30B" in r.message for r in caplog.records)
    await retention_job(bot, db, s)  # nothing left -> no second message
    assert len(bot.sent) == 1
    s = replace(s, retention_enabled=False)
    await add(db, media, 2, Status.REJECTED, ago(days=3))
    assert await retention_job(bot, db, s) is None
    assert len(bot.sent) == 1


async def test_job_dry_run_deletes_and_notifies_nothing(env):
    s, db, media = env
    s = replace(s, cleanup_minio=False)
    s = replace(s, retention_dry_run=True)
    _, *files = await add(db, media, 1, Status.REJECTED, ago(days=3))
    bot = FakeBot()
    rep = await retention_job(bot, db, s)
    assert rep.files == 3 and all(p.exists() for p in files) and bot.sent == []


async def test_telegram_failure_does_not_propagate(env):
    s, db, media = env
    s = replace(s, cleanup_minio=False)
    await add(db, media, 1, Status.REJECTED, ago(days=3))

    class Broken:
        async def send_message(self, *a):
            raise RuntimeError("telegram down")

    assert (await retention_job(Broken(), db, s)).files == 3


async def test_periodic_loop_survives_a_raising_job_and_runs_first_early():
    from pipeline.__main__ import _periodic

    stop = asyncio.Event()
    calls = []

    async def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) == 3:
            stop.set()

    # interval is huge: only first_delay lets the first call happen at all.
    await asyncio.wait_for(_periodic(0.01, job, stop, first_delay=0.0), 5)
    assert len(calls) == 3


async def test_s3_store_only_touches_the_item_prefix():
    from pipeline.retention import S3Store

    class Pager:
        def paginate(self, Bucket, Prefix):
            self.prefix = Prefix
            return [{"Contents": [{"Key": "items/5/a.png", "Size": 7}]}]

    class Client:
        def __init__(self):
            self.pager, self.deleted = Pager(), []

        def get_paginator(self, name):
            return self.pager

        def delete_objects(self, Bucket, Delete):
            self.deleted += [o["Key"] for o in Delete["Objects"]]

    c = Client()
    assert S3Store(c, "b").purge_item(5, apply=True) == (1, 7)
    assert c.pager.prefix == "items/5/" and c.deleted == ["items/5/a.png"]

    class Evil(Pager):
        def paginate(self, Bucket, Prefix):
            return [{"Contents": [{"Key": "items/50/a.png", "Size": 1}]}]

    c2 = Client()
    c2.pager = Evil()
    with pytest.raises(RuntimeError):
        S3Store(c2, "b").purge_item(5, apply=True)
    assert c2.deleted == []


async def test_s3_per_key_delete_errors_are_reported_not_swallowed(env):
    from pipeline.retention import S3Store

    class Client:
        def get_paginator(self, name):
            class P:
                def paginate(self, Bucket, Prefix):
                    return [{"Contents": [{"Key": "items/1/a.png", "Size": 3}]}]
            return P()

        def delete_objects(self, Bucket, Delete):
            return {"Errors": [{"Key": "items/1/a.png", "Code": "AccessDenied"}]}

    s, db, media = env
    item, *files = await add(db, media, 1, Status.REJECTED, ago(days=3))
    rep = await run_retention(db, s, apply=True, now=NOW, store=S3Store(Client(), "b"))
    assert len(rep.errors) == 1 and "AccessDenied" in rep.errors[0]
    assert all(not p.exists() for p in files)  # local work is still done and counted
    assert rep.files == 3 and rep.objects == 0


def test_retention_client_gets_explicit_timeouts(tmp_path):
    from pipeline.config import RETENTION_S3_CONNECT_TIMEOUT_S, RETENTION_S3_READ_TIMEOUT_S
    from pipeline.retention import build_store

    s = Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "t.db"),
                           "R2_BUCKET": "b", "R2_ACCESS_KEY": "k", "R2_SECRET_KEY": "s",
                           "S3_ENDPOINT": "http://127.0.0.1:9"})
    cfg = build_store(s).client.meta.config
    assert cfg.connect_timeout == RETENTION_S3_CONNECT_TIMEOUT_S
    assert cfg.read_timeout == RETENTION_S3_READ_TIMEOUT_S


async def test_symlink_inside_media_dir_is_not_followed_to_its_target(env):
    """A link that stays inside media_dir still must not delete what it points at."""
    s, db, media = env
    rid, raw, *_ = await add(db, media, 1, Status.REJECTED, ago(days=3))
    unrelated = media / "channel" / "unrelated.jpg"
    unrelated.write_bytes(b"keep")
    link = media / "channel" / "link.jpg"
    link.symlink_to(unrelated)
    await db.conn.execute("UPDATE items SET raw_media_paths=? WHERE id=?",
                          (json.dumps([str(link)]), rid))
    await db.conn.commit()
    rep = await run_retention(db, s, apply=True, now=NOW, store=FakeStore())
    assert unrelated.read_bytes() == b"keep"
    assert any("link.jpg" in r for r in rep.refused)


def test_dashboard_media_route_404s_for_deleted_slides_not_500(tmp_path):
    """Retention removes renders; the dashboard's <img> for them must 404 cleanly."""
    import importlib.util
    from pathlib import Path

    from aiohttp import web

    spec = importlib.util.spec_from_file_location(
        "dash_retention", Path(__file__).resolve().parents[1] / "scripts" / "dashboard.py")
    dash = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dash)

    class Req:
        app = {"settings": type("S", (), {"media_dir": tmp_path})()}
        match_info = {"id": "7", "name": "slide_01.png"}

    with pytest.raises(web.HTTPNotFound):
        asyncio.run(dash.media(Req()))
