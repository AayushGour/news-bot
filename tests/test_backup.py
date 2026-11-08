"""Hourly backups (T348). tmp dirs only - never data/app.db."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from pipeline import backup as B
from pipeline.config import Settings
from tests.conftest import TEST_ENV


def make_live(path, rows=5):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO items(body) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    return conn  # kept open so the WAL/-shm stay alive like the worker's


def count(path):
    c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return c.execute("SELECT count(*) FROM items").fetchone()[0]
    finally:
        c.close()


def test_backup_is_rollback_copy_with_no_sidecars(tmp_path):
    live, out = tmp_path / "live.db", tmp_path / "b" / "app-x.db"
    conn = make_live(live)
    assert B.write_backup(live, out)
    h = out.read_bytes()[:20]
    assert h[18] == 1 and h[19] == 1
    assert count(out) == 5
    assert [p.name for p in out.parent.iterdir()] == ["app-x.db"]
    conn.close()


def test_orphan_shm_is_unlinked_regression(tmp_path, monkeypatch):
    """The dst connection leaves <tmp>-shm behind; it must not survive or block."""
    live, out = tmp_path / "live.db", tmp_path / "b" / "app-x.db"
    make_live(live).close()
    seen = []
    real = B.os.replace

    def spy(src, dst):
        seen.append(sorted(p.name for p in dst.parent.iterdir()) if False else
                    sorted(p.name for p in src.parent.iterdir()))
        return real(src, dst)

    monkeypatch.setattr(B.os, "replace", spy)
    assert B.write_backup(live, out)
    assert seen == [["app-x.db.tmp"]]  # no -shm beside the tmp at rename time
    assert [p.name for p in out.parent.iterdir()] == ["app-x.db"]


def test_stale_tmp_leftovers_cleared(tmp_path):
    live, out = tmp_path / "live.db", tmp_path / "b" / "app-x.db"
    make_live(live).close()
    out.parent.mkdir()
    tmp = out.with_name("app-x.db.tmp")
    tmp.write_bytes(b"junk")
    tmp.with_name(tmp.name + "-wal").write_bytes(b"x")
    assert B.write_backup(live, out)
    assert count(out) == 5 and len(list(out.parent.iterdir())) == 1


def test_missing_live_not_created_and_logs_once(tmp_path, caplog):
    live, out = tmp_path / "none.db", tmp_path / "b" / "app-x.db"
    with caplog.at_level(logging.WARNING, logger="pipeline.backup"):
        assert B.write_backup(live, out) is False
    assert not live.exists() and not out.exists() and len(caplog.records) == 1


def test_refuses_to_overwrite_live(tmp_path):
    live = tmp_path / "live.db"
    make_live(live).close()
    before = live.read_bytes()
    assert B.write_backup(live, live) is False
    assert live.read_bytes() == before


def test_replace_failure_keeps_dir_clean(tmp_path, monkeypatch):
    live, out = tmp_path / "live.db", tmp_path / "b" / "app-x.db"
    make_live(live).close()

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(B.os, "replace", boom)
    assert B.write_backup(live, out) is False
    assert list(out.parent.iterdir()) == []


def test_prune_keeps_newest_n_and_other_files(tmp_path):
    for i in range(7):
        (tmp_path / f"app-2026010{i}T0000.db").write_text("x")
    (tmp_path / "pre-docker-1.db").write_text("x")
    (tmp_path / "notes.txt").write_text("x")
    doomed = B.prune_backups(tmp_path, keep=3)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["app-20260104T0000.db", "app-20260105T0000.db", "app-20260106T0000.db",
                    "notes.txt", "pre-docker-1.db"]
    assert len(doomed) == 4
    assert B.prune_backups(tmp_path, keep=3) == []


def test_run_backup_names_by_utc_and_rotates(tmp_path):
    live, d = tmp_path / "live.db", tmp_path / "b"
    make_live(live).close()
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    outs = [B.run_backup(live, d, now=t0 + timedelta(hours=i), keep=3) for i in range(5)]
    assert outs[0].name == "app-20260101T0000.db"
    assert sorted(p.name for p in d.iterdir()) == [
        "app-20260101T0200.db", "app-20260101T0300.db", "app-20260101T0400.db"]


def test_default_keep_is_48():
    assert B.BACKUP_KEEP == 48


async def test_backup_job_and_settings(tmp_path):
    s = Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "a.db"),
                           "BACKUP_DIR": str(tmp_path / "bk")})
    make_live(s.db_path).close()
    await B.backup_job(s)
    (f,) = list((tmp_path / "bk").iterdir())
    assert count(f) == 5
    d = Settings.load(env=TEST_ENV)
    assert d.backup_interval_s == 3600 and d.backup_dir.parts[-2:] == ("data", "backups")
    assert Settings.load(env={**TEST_ENV, "BACKUP_INTERVAL_S": "-5"}).backup_interval_s == 0


def test_settings_new_fields(tmp_path):
    d = Settings.load(env={**TEST_ENV, "LEASE_PATH": ""})
    assert d.tunnel_target == "http://localhost:9000"
    e = Settings.load(env={**TEST_ENV, "TUNNEL_TARGET": "http://host.docker.internal:9000",
                           "LEASE_PATH": str(tmp_path / "l")})
    assert e.tunnel_target == "http://host.docker.internal:9000"
    assert e.lease_path == tmp_path / "l"


def test_in_container(monkeypatch):
    from pipeline import config

    monkeypatch.setattr(config, "DOCKERENV_PATH", config.Path("/nonexistent-dockerenv"))
    monkeypatch.delenv(config.IN_CONTAINER_ENV, raising=False)
    assert config.in_container() is False
    monkeypatch.setenv(config.IN_CONTAINER_ENV, "1")
    assert config.in_container() is True
    monkeypatch.setenv(config.IN_CONTAINER_ENV, "0")
    assert config.in_container() is False
