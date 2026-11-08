"""TUNNEL_TARGET reaches cloudflared's spawn args (T348)."""

import asyncio

from pipeline.publish import tunnel


async def test_target_is_passed_to_cloudflared(monkeypatch):
    seen = {}

    async def fake_exec(*args, **kw):
        seen["args"] = args
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await tunnel.supervise(asyncio.Event(), "http://host.docker.internal:9000")
    assert seen["args"][:4] == ("cloudflared", "tunnel", "--url", "http://host.docker.internal:9000")


async def test_default_target_unchanged(monkeypatch):
    seen = {}

    async def fake_exec(*args, **kw):
        seen["args"] = args
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await tunnel.supervise(asyncio.Event())
    assert seen["args"][3] == "http://localhost:9000"
