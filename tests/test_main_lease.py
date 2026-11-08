"""main() lease wiring (T348): heartbeat from acquire onward, fencing, tunnel target.

Everything external is faked (Telethon, aiogram polling, the worker loops); the
DB is a real sqlite file in tmp. Never the real session, secrets or data/.
"""

import asyncio
import json
import os
import signal

import aiogram
import pytest
import pytest_asyncio

from pipeline import __main__ as m
from pipeline import lease as L
from pipeline.config import Settings
from pipeline.lease import WorkerLease
from tests.conftest import TEST_ENV

PARENT = os.getppid()


class FakeTelethon:
    def __init__(self, *a, **k):
        FakeTelethon.instance = self
        self.started = asyncio.Event()
        self.disconnected = False

    async def start(self):
        await FakeTelethon.start_hook()
        self.started.set()

    async def disconnect(self):
        self.disconnected = True


async def _instant():
    return None


class FakeListener:
    def __init__(self, *a, **k):
        pass

    def register(self):
        pass


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    lease_path = tmp_path / "w.lease"
    s = Settings.load(env={
        **TEST_ENV, "TELEGRAM_BOT_TOKEN": "123:abc", "DB_PATH": str(tmp_path / "a.db"),
        "LEASE_PATH": str(lease_path), "TELEGRAM_SESSION_PATH": str(tmp_path / "s.session"),
        "BACKUP_INTERVAL_S": "0", "MANAGE_TUNNEL": "1",
        "TUNNEL_TARGET": "http://host.docker.internal:9000",
    })
    monkeypatch.setattr(m.Settings, "load", classmethod(lambda cls, env=None: s))
    monkeypatch.setattr(m.Settings, "validate_for_run", lambda self, env=None: None)
    monkeypatch.setattr(m.logging, "basicConfig", lambda **k: None)
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "log" / "p.log")
    monkeypatch.setattr(m, "LEASE_HEARTBEAT_S", 0.05)
    FakeTelethon.start_hook = staticmethod(_instant)
    FakeTelethon.instance = None
    import telethon
    monkeypatch.setattr(telethon, "TelegramClient", FakeTelethon)
    import pipeline.intake.channel as channel
    monkeypatch.setattr(channel, "ChannelListener", FakeListener)

    running = asyncio.Event()
    stopped_by = {}

    async def park(self, *a, stop=None, **k):
        stop = stop or a[-1]
        running.set()
        await stop.wait()

    async def park_listener(telethon_, listener, stop):
        await stop.wait()

    async def polling(self, *a, **k):
        await asyncio.Event().wait()

    async def fake_tunnel(stop, target=None):
        stopped_by["target"] = target
        await stop.wait()

    monkeypatch.setattr(m.Worker, "run_slow", lambda self, interval=5.0, stop=None: park(self, stop=stop))
    monkeypatch.setattr(m.Worker, "run_fast", lambda self, interval=2.0, stop=None: park(self, stop=stop))
    monkeypatch.setattr(m, "supervise_listener", park_listener)
    monkeypatch.setattr(aiogram.Dispatcher, "start_polling", polling)
    monkeypatch.setattr(m, "supervise_tunnel", fake_tunnel)
    # Fallback: if main() ever stops installing its SIGTERM handler, a test's
    # raise_signal must fail that test (it times out) rather than kill pytest.
    prev = signal.signal(signal.SIGTERM, lambda *_: None)
    yield s, lease_path, running, stopped_by
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.remove_signal_handler(sig)
    signal.signal(signal.SIGTERM, prev)


def _holder(path):
    return json.loads(path.read_text())["instance_id"]


async def test_fencing_stops_main_and_leaves_the_other_lease(rig):
    s, lp, running, _ = rig
    task = asyncio.create_task(m.main())
    await asyncio.wait_for(running.wait(), 20)
    other = WorkerLease(lp, pid=PARENT)
    other._write()  # a second worker took the lease
    assert await asyncio.wait_for(task, 5) == 0
    assert _holder(lp) == other.instance_id  # release() did not delete someone else's


async def test_heartbeat_keeps_lease_fresh_during_slow_telethon_start(rig, monkeypatch):
    s, lp, running, _ = rig
    monkeypatch.setattr(L, "LEASE_STALE_S", 0.3)

    async def slow():
        await asyncio.sleep(0.9)  # three times the stale window

    FakeTelethon.start_hook = staticmethod(slow)
    task = asyncio.create_task(m.main())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if FakeTelethon.instance is not None:
            break
    await asyncio.sleep(0.6)  # inside the slow start, well past one stale window
    assert not FakeTelethon.instance.started.is_set()
    with pytest.raises(L.LeaseHeld):
        WorkerLease(lp, pid=PARENT).acquire()  # still fresh -> second worker refused
    await asyncio.wait_for(running.wait(), 10)
    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, 5) == 0
    assert not lp.exists()  # released on clean shutdown


async def test_fenced_during_telethon_start_returns_1_and_disconnects(rig):
    s, lp, running, _ = rig
    other = WorkerLease(lp, pid=PARENT)

    async def slow():
        await asyncio.sleep(0.4)
        other._write()
        await asyncio.sleep(0.3)  # let a heartbeat notice

    FakeTelethon.start_hook = staticmethod(slow)
    assert await asyncio.wait_for(m.main(), 20) == 1
    assert FakeTelethon.instance.disconnected and not running.is_set()
    assert _holder(lp) == other.instance_id


async def test_second_worker_and_unsafe_session_exit_1_before_anything_starts(rig, monkeypatch):
    s, lp, running, _ = rig
    WorkerLease(lp, pid=PARENT).acquire()
    assert await asyncio.wait_for(m.main(), 10) == 1
    assert FakeTelethon.instance is None
    lp.unlink()
    monkeypatch.setattr(m, "require_local_fs",
                        lambda *a, **k: (_ for _ in ()).throw(m.UnsafeStorage("bad")))
    assert await asyncio.wait_for(m.main(), 10) == 1 and not lp.exists() and not s.db_path.exists()


async def test_tunnel_target_reaches_supervise_tunnel(rig):
    s, lp, running, seen = rig
    task = asyncio.create_task(m.main())
    await asyncio.wait_for(running.wait(), 20)
    await asyncio.sleep(0.05)
    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, 5) == 0
    assert seen["target"] == "http://host.docker.internal:9000"


async def test_fenced_before_telethon_start_never_calls_start(rig, monkeypatch):
    s, lp, running, _ = rig
    other = WorkerLease(lp, pid=PARENT)
    started = []

    async def start_hook():
        started.append(1)

    FakeTelethon.start_hook = staticmethod(start_hook)
    real_connect = m.Database.connect

    async def slow_connect(self):
        other._write()  # lease stolen while the DB opens...
        await asyncio.sleep(0.3)  # ...long enough for a heartbeat to notice
        return await real_connect(self)

    monkeypatch.setattr(m.Database, "connect", slow_connect)
    assert await asyncio.wait_for(m.main(), 10) == 1
    assert started == [] and not running.is_set()
    assert _holder(lp) == other.instance_id
