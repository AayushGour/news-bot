"""T359: host script refusal line, and clock-skew-safe lease liveness."""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline import lease as L
from pipeline.lease import LeaseHeld, host_fence, run_host_script

ROOT = Path(__file__).resolve().parents[1]


def put_lease(p, *, heartbeat_at, mtime=None, kind="container"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"owner_kind": kind, "instance_id": "x", "hostname": "c1",
                             "pid": 1, "heartbeat_at": heartbeat_at}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))


@pytest.fixture
def p(tmp_path):
    return tmp_path / "worker.lease"


# ---- run_host_script

def test_run_host_script_prints_one_line_and_returns_1(capsys):
    def boom():
        raise LeaseHeld("long internal text")

    assert run_host_script("requeue", boom) == 1
    out = capsys.readouterr()
    assert out.out.strip() == ("the pipeline runs in Docker — use: "
                               "docker compose exec app python scripts/requeue.py …")
    assert out.err == ""


def test_run_host_script_passes_results_and_other_errors_through():
    assert run_host_script("x", lambda: 7) == 7
    assert run_host_script("x", lambda: None) is None
    with pytest.raises(ValueError):
        run_host_script("x", lambda: (_ for _ in ()).throw(ValueError("real bug")))


def _script_env(lease, db):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM", "PIPELINE"))}
    env.update(LEASE_PATH=str(lease), DB_PATH=str(db))
    return env


@pytest.mark.parametrize("name,args", [
    ("requeue", ["1"]), ("queue_item", ["some text"]), ("cleanup", []),
    ("build_golden", ["--list"]),
])
def test_scripts_refuse_cleanly_while_container_holds_lease(tmp_path, name, args):
    lease, db = tmp_path / "w.lease", tmp_path / "sub" / "app.db"
    put_lease(lease, heartbeat_at=time.time())
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py"), *args],
                       capture_output=True, text=True, timeout=120, cwd=ROOT,
                       env=_script_env(lease, db))
    assert r.returncode == 1, r.stderr
    assert "Traceback" not in r.stderr and r.stderr.strip() == ""
    assert r.stdout.strip() == (f"the pipeline runs in Docker — use: "
                                f"docker compose exec app python scripts/{name}.py …")
    assert not db.parent.exists()  # refused before touching the filesystem


def test_every_script_that_opens_the_db_is_wrapped():
    """The scripts that connect a Database must route through run_host_script."""
    missing = []
    for f in sorted((ROOT / "scripts").glob("*.py")):
        src = f.read_text()
        if re.search(r"Database\(.*\)\.connect\(\)", src) and "run_host_script(" not in src:
            missing.append(f.name)
    assert missing == []


# ---- clock-skew-safe liveness

def test_lagging_container_clock_is_still_live_via_mtime(p):
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000, mtime=now)  # VM clock lagging, file fresh
    with pytest.raises(LeaseHeld):
        host_fence(p, clock=lambda: now)


def test_stale_heartbeat_and_stale_mtime_is_not_live(p):
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000, mtime=now - 1000)
    host_fence(p, clock=lambda: now)


def test_mtime_exactly_at_stale_boundary_is_not_live(p):
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000, mtime=now - L.LEASE_STALE_S)
    host_fence(p, clock=lambda: now)


def test_mtime_in_the_future_does_not_make_a_stale_lease_live(p):
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000, mtime=now + 50)
    host_fence(p, clock=lambda: now)


def test_far_future_heartbeat_is_live_and_logged(p, caplog):
    now = time.time()
    put_lease(p, heartbeat_at=now + L.LEASE_MAX_SKEW_S + 1, mtime=now - 1000)
    with caplog.at_level(logging.WARNING, logger="pipeline.lease"):
        with pytest.raises(LeaseHeld):
            host_fence(p, clock=lambda: now)
    assert "clock skew" in caplog.text


def test_small_future_heartbeat_is_live_without_warning(p, caplog):
    now = time.time()
    put_lease(p, heartbeat_at=now + 10, mtime=now - 1000)
    with caplog.at_level(logging.WARNING, logger="pipeline.lease"):
        with pytest.raises(LeaseHeld):
            host_fence(p, clock=lambda: now)
    assert "clock skew" not in caplog.text


def test_missing_or_unreadable_lease_not_live(p):
    assert L.lease_is_live(p, None, time.time()) is False
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000)
    rec = L.read_lease(p)
    p.unlink()
    assert L.lease_is_live(p, rec, now) is False  # stale and no mtime to consult


def test_host_worker_cannot_acquire_over_a_lagging_container_lease(p):
    now = time.time()
    put_lease(p, heartbeat_at=now - 1000, mtime=now)
    with pytest.raises(LeaseHeld):
        L.WorkerLease(p, pid=os.getpid() + 1, hostname="mac").acquire()
