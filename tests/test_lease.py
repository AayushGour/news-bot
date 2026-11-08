"""Worker lease + host fence (T348). Temp dirs and a fake clock only."""

import json
import os

import pytest

from pipeline import lease as L
from pipeline.lease import LeaseHeld, WorkerLease, host_fence


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def p(tmp_path):
    return tmp_path / "data" / "worker.lease"


def test_acquire_writes_record_and_no_tmp_left(p):
    c = Clock()
    w = WorkerLease(p, clock=c)
    w.acquire()
    rec = json.loads(p.read_text())
    assert rec["instance_id"] == w.instance_id and rec["heartbeat_at"] == 1000.0
    assert rec["owner_kind"] == "host"
    assert [x.name for x in p.parent.iterdir()] == ["worker.lease"]


PARENT = os.getppid()  # a pid that is alive and is not ours


def test_second_worker_refused_while_fresh(p):
    c = Clock()
    WorkerLease(p, clock=c, pid=PARENT).acquire()
    c.t += L.LEASE_STALE_S - 1
    with pytest.raises(LeaseHeld):
        WorkerLease(p, clock=c).acquire()


def test_stale_lease_is_taken_over_and_old_owner_fenced(p):
    c = Clock()
    a, b = WorkerLease(p, clock=c, pid=PARENT), WorkerLease(p, clock=c)
    a.acquire()
    c.t += L.LEASE_STALE_S
    b.acquire()
    assert b.holds() and not a.holds()
    assert a.heartbeat() is False
    assert b.holds()  # fenced worker did not overwrite


def test_heartbeat_refreshes_and_recreates_missing(p):
    c = Clock()
    a = WorkerLease(p, clock=c)
    a.acquire()
    c.t += 30
    assert a.heartbeat()
    assert json.loads(p.read_text())["heartbeat_at"] == 1030.0
    p.unlink()
    assert a.heartbeat() and a.holds()


def test_release_only_removes_own(p):
    c = Clock()
    a, b = WorkerLease(p, clock=c, pid=PARENT), WorkerLease(p, clock=c)
    a.acquire()
    b.release()
    assert p.exists()
    a.release()
    assert not p.exists()


def test_malformed_lease_is_not_a_holder(p):
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    w = WorkerLease(p)
    w.acquire()
    assert w.holds()


def _container_lease(p, c, monkeypatch):
    monkeypatch.setattr(L, "in_container", lambda: True)
    w = WorkerLease(p, clock=c)
    w.acquire()
    monkeypatch.setattr(L, "in_container", lambda: False)
    return w


def test_host_fence_refuses_on_fresh_container_lease(p, monkeypatch):
    c = Clock()
    _container_lease(p, c, monkeypatch)
    with pytest.raises(LeaseHeld, match="docker compose exec"):
        host_fence(p, clock=c)
    c.t += L.LEASE_STALE_S
    host_fence(p, clock=c)  # stale: allowed


def test_host_fence_ignores_host_lease_missing_and_container(p, monkeypatch):
    c = Clock()
    host_fence(p, clock=c)
    WorkerLease(p, clock=c).acquire()  # host-owned
    host_fence(p, clock=c)
    p.unlink()
    _container_lease(p, c, monkeypatch)
    monkeypatch.setattr(L, "in_container", lambda: True)
    host_fence(p, clock=c)  # inside a container the fence is not the host's


async def test_database_connect_host_fence(p, monkeypatch, tmp_path):
    from pipeline.db import Database

    c = Clock()
    import time
    c.t = time.time()
    _container_lease(p, c, monkeypatch)
    with pytest.raises(LeaseHeld):
        await Database(tmp_path / "x" / "a.db", lease_path=p).connect()
    assert not (tmp_path / "x").exists()


def test_check_requires_own_fresh_container_lease(p, monkeypatch):
    c = Clock()
    assert not L.check(p, clock=c)
    _container_lease(p, c, monkeypatch)
    assert L.check(p, clock=c)
    c.t += L.LEASE_STALE_S
    assert not L.check(p, clock=c)
    c.t = 1000.0
    monkeypatch.setattr(L.socket, "gethostname", lambda: "other")
    assert not L.check(p, clock=c)


def test_cli_exit_codes(p, monkeypatch, capsys):
    monkeypatch.setenv("LEASE_PATH", str(p))
    assert L.main(["--check"]) == 1



def test_tmp_name_is_per_instance_and_cleaned(p, monkeypatch):
    sources = []
    real = L.Path.replace

    def spy(self, target):
        sources.append(self.name)
        return real(self, target)

    monkeypatch.setattr(L.Path, "replace", spy)
    a = WorkerLease(p)
    a._write()
    assert sources == [f"worker.lease.{a.instance_id}.tmp"]
    assert [x.name for x in p.parent.iterdir()] == ["worker.lease"]


def test_absent_lease_claimed_exclusively(p, monkeypatch):
    """Two workers see an empty slot: O_EXCL lets exactly one in."""
    a, b = WorkerLease(p, pid=PARENT), WorkerLease(p)
    # b saw an empty slot, but a creates the file before b's create_exclusive runs
    orig = b._create_exclusive

    def late():
        a.acquire()
        orig()

    monkeypatch.setattr(b, "_create_exclusive", late)
    with pytest.raises(LeaseHeld):
        b.acquire()
    assert a.holds() and not b.holds()


def test_race_loss_after_write_is_detected(p):
    class Racy(WorkerLease):
        def _write(self):
            super()._write()
            other = WorkerLease(self.path, pid=PARENT)
            other._write()  # a competitor renames over ours

    p.parent.mkdir(parents=True)
    p.write_text("{bad")  # exists -> rename path, not O_EXCL
    w = Racy(p)
    with pytest.raises(LeaseHeld, match="lost the race"):
        w.acquire()


def test_dead_pid_same_host_is_taken_over_immediately(p, monkeypatch):
    c = Clock()
    WorkerLease(p, clock=c, pid=4242, hostname="mac").acquire()
    monkeypatch.setattr(L, "pid_alive", lambda pid: False)
    b = WorkerLease(p, clock=c, pid=99, hostname="mac")
    b.acquire()  # fresh lease, but its owner was SIGKILLed
    assert b.holds()


def test_live_pid_same_host_is_still_refused(p, monkeypatch):
    c = Clock()
    WorkerLease(p, clock=c, pid=4242, hostname="mac").acquire()
    monkeypatch.setattr(L, "pid_alive", lambda pid: True)
    with pytest.raises(LeaseHeld):
        WorkerLease(p, clock=c, pid=99, hostname="mac").acquire()


def test_dead_pid_on_another_host_is_not_trusted(p, monkeypatch):
    c = Clock()
    WorkerLease(p, clock=c, pid=4242, hostname="other").acquire()
    monkeypatch.setattr(L, "pid_alive", lambda pid: False)
    with pytest.raises(LeaseHeld):
        WorkerLease(p, clock=c, pid=99, hostname="mac").acquire()


def test_dead_pid_of_other_kind_is_not_taken(p, monkeypatch):
    c = Clock()
    monkeypatch.setattr(L, "in_container", lambda: True)
    WorkerLease(p, clock=c, pid=4242, hostname="mac").acquire()
    monkeypatch.setattr(L, "in_container", lambda: False)
    monkeypatch.setattr(L, "pid_alive", lambda pid: False)
    with pytest.raises(LeaseHeld):
        WorkerLease(p, clock=c, pid=99, hostname="mac").acquire()


def test_restarted_container_same_host_and_pid_takes_over(p, monkeypatch):
    c = Clock()
    monkeypatch.setattr(L, "in_container", lambda: True)
    monkeypatch.setattr(L, "pid_alive", lambda pid: True)  # pid 1 "alive": it is us
    WorkerLease(p, clock=c, pid=1, hostname="abc123").acquire()
    new = WorkerLease(p, clock=c, pid=1, hostname="abc123")
    new.acquire()
    assert new.holds()
    other_pid = WorkerLease(p, clock=c, pid=7, hostname="abc123")
    with pytest.raises(LeaseHeld):  # live different pid, same host: a real second worker
        other_pid.acquire()


def test_pid_alive_real():
    assert L.pid_alive(os.getpid())
    assert not L.pid_alive(2**22 + 12345)


def test_pid_alive_error_mapping(monkeypatch):
    def raiser(exc):
        def kill(pid, sig):
            raise exc
        return kill

    monkeypatch.setattr(L.os, "kill", raiser(PermissionError()))
    assert L.pid_alive(5) is True  # exists, owned by someone else
    monkeypatch.setattr(L.os, "kill", raiser(ProcessLookupError()))
    assert L.pid_alive(5) is False
    monkeypatch.setattr(L.os, "kill", raiser(OSError()))
    assert L.pid_alive(5) is False
