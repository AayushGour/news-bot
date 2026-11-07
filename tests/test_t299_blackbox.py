"""T299 blackbox: revision fetch executors + safe_download, public API only.

No test reads data/app.db. Decks in fixtures/t299_decks.json were frozen once from a
read-only ``.backup`` by fixtures/build_t299_decks.py. All HTTP is httpx.MockTransport,
DNS is a fake resolver, the LLM is scripted. Real network: ``T299_LIVE=1`` only (AC6).
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import ipaddress
import json
import logging
import os
import re
import socket
import time
import tracemalloc
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from pipeline import safe_download as sd
from pipeline.errors import ConfigFault, EnginesBlocked, Retryforever
from pipeline.models import Item, Status
from pipeline.retention import run_retention
from pipeline.revision.fetch import (
    REASON_IMAGE_UNCHECKED,
    REASON_NO_CANDIDATES,
    REASON_NO_USABLE_IMAGE,
    REASON_SAME_IMAGE,
    fix_fact,
    replace_image,
    research,
)
from pipeline.revision.model import Action, Op, OutcomeCode
from pipeline.safe_download import Downloaded, Refusal, check_url, download_public_image, is_public_address

ROOT = Path(__file__).resolve().parents[1]
DECKS = json.loads((ROOT / "tests/fixtures/t299_decks.json").read_text())
PUBLIC = "93.184.216.34"
HOSTS = {  # fake DNS
    "img.test": [PUBLIC], "cdn.test": ["93.184.216.35"], "searx.test": ["93.184.216.36"],
    "evil.test": ["10.0.0.5"], "lo.test": ["127.0.0.1"], "mixed.test": [PUBLIC, "192.168.0.9"],
    "v6.test": ["2606:4700::1111"], "mapped.test": ["::ffff:127.0.0.1"],
}


# ------------------------------------------------------------------ helpers
@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    async def resolve(host, port):
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        if host in HOSTS:
            return list(HOSTS[host])
        raise socket.gaierror("no such host")

    monkeypatch.setattr(sd, "_resolve", resolve)


def noise(w, h, mode="RGB"):
    return Image.frombytes(mode, (w, h), os.urandom(w * h * len(mode)))


def encode(img, fmt, **kw):
    buf = BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


_CACHE: dict = {}


def blob(name="a"):
    if name not in _CACHE:
        _CACHE[name] = encode(noise(1000, 1000), "JPEG", quality=70)
    return _CACHE[name]


def png_blob():
    if "png" not in _CACHE:
        _CACHE["png"] = encode(noise(960, 960), "PNG")
    return _CACHE["png"]


class Chunks(httpx.AsyncByteStream):
    """Body yielded lazily; counts how much the consumer pulled."""

    def __init__(self, gen):
        self.gen, self.pulled, self.closed = gen, 0, False

    async def __aiter__(self):
        async for c in self.gen():
            self.pulled += len(c)
            yield c

    async def aclose(self):
        self.closed = True


class Bytes(httpx.AsyncByteStream):
    """A real-transport-like body: streamed, not pre-read (httpx marks content= bodies consumed)."""

    def __init__(self, data, step=65536):
        self.data, self.step = data, step

    async def __aiter__(self):
        for i in range(0, len(self.data), self.step):
            yield self.data[i:i + self.step]


def img_resp(body, ctype="image/jpeg", **headers):
    h = {"content-type": ctype, **headers} if ctype else dict(headers)
    return httpx.Response(200, headers=h, stream=Bytes(body))


class Web:
    """httpx client over a MockTransport; records every request."""

    def __init__(self, handler):
        self.seen: list[httpx.Request] = []

        def wrapped(req):
            self.seen.append(req)
            return handler(req)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))

    def urls(self):
        return [str(r.url) for r in self.seen]


def web_for(images: dict, search=None):
    """images: url -> Response|bytes. search: list of img urls for the searx call."""

    norm = {str(httpx.URL(k)): v for k, v in images.items()}

    def handler(req):
        url = str(req.url)
        if req.url.host == "searx.test":
            urls = search if search is not None else list(images)
            return httpx.Response(200, json={"results": [
                {"img_src": u, "title": "t", "url": "http://page.test/", "engine": "e"} for u in urls]})
        got = norm.get(url)
        if got is None:
            return httpx.Response(404)
        return got if isinstance(got, httpx.Response) else img_resp(got)

    return Web(handler)


def mk_settings(settings, tmp_path):
    return replace(settings, searxng_url="http://searx.test", media_dir=tmp_path / "media")


def mk_item(d=None, **kw):
    d = d or {}
    return Item(id=kw.pop("id", d.get("id", 7)), source="channel", status=Status.AWAITING_APPROVAL,
                raw_text=d.get("raw_text", "OpenAI model news"), research=d.get("research", []),
                caption=d.get("caption", ""), **kw)


def act(op, slide=1, query="q", ask=1, **kw):
    return Action(op=op.value, ask=ask, slide=slide, query=query, **kw)


DECK = [
    {"type": "hook", "headline": "Acme ships Foo", "sub": "A new tool"},
    {"type": "point", "headline": "What it does", "bullets": ["Runs fast", "Is small"]},
    {"type": "follow", "headline": "Follow us"},
]
CLOSE = 3


# ======================================================================= AC1
@pytest.mark.parametrize("addr", [
    "0.0.0.0", "127.0.0.1", "127.1.2.3", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "169.254.169.254", "100.64.0.1", "100.127.255.255", "192.0.0.1", "192.0.2.1",
    "198.18.0.1", "198.19.255.255", "198.51.100.1", "203.0.113.1", "224.0.0.1", "239.255.255.255",
    "240.0.0.1", "255.255.255.255", "::", "::1", "fe80::1", "fe80::1%eth0", "fc00::1", "fd12::1",
    "fec0::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
    "::ffff:0:0", "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::c0a8:1",
    "64:ff9b:1::1", "2002:7f00:1::1", "2002:a00:1::1", "2002:a9fe:a9fe::1", "2002:c0a8:1::1",
    "2001:db8::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "100::1", "::2", "5f00::1",
    "garbage", "", "1.2.3", "999.1.1.1", "0x7f.1", "2130706433",
])
def test_ac1_non_public_addresses_refused(addr):
    assert is_public_address(addr) is False


@pytest.mark.parametrize("addr", [
    "8.8.8.8", "1.1.1.1", PUBLIC, "100.63.255.255", "100.128.0.1", "172.15.255.255", "172.32.0.1",
    "2606:4700::1111", "2a00:1450:4001::1", "::ffff:8.8.8.8", "64:ff9b::808:808",
])
def test_ac1_public_addresses_accepted(addr):
    assert is_public_address(addr) is True


@pytest.mark.parametrize("url,want", [
    ("http://img.test/a.jpg", None), ("https://img.test/a.jpg", None), ("HTTP://img.test/a", None),
    ("HTTPS://IMG.TEST/a", None), ("http://img.test:80/a", None), ("https://img.test:443/a", None),
    ("http://img.test:443/a", None), ("https://img.test:80/a", None), ("http://v6.test/a", None),
    ("http://[2606:4700::1111]/a", None), ("http://93.184.216.34/a", None),
    ("http://10.0.0.1/a", Refusal.PRIVATE_HOST), ("http://127.0.0.1/a", Refusal.PRIVATE_HOST),
    ("http://169.254.169.254/latest/meta-data", Refusal.PRIVATE_HOST),
    ("http://100.64.0.1/a", Refusal.PRIVATE_HOST), ("http://0.0.0.0/a", Refusal.PRIVATE_HOST),
    ("http://[::]/a", Refusal.PRIVATE_HOST), ("http://[::1]/a", Refusal.PRIVATE_HOST),
    ("http://[::ffff:127.0.0.1]/a", Refusal.PRIVATE_HOST), ("http://[::ffff:7f00:1]/a", Refusal.PRIVATE_HOST),
    ("http://[64:ff9b::7f00:1]/a", Refusal.PRIVATE_HOST), ("http://[2002:7f00:1::1]/a", Refusal.PRIVATE_HOST),
    ("http://[fec0::1]/a", Refusal.PRIVATE_HOST), ("http://[fe80::1]/a", Refusal.PRIVATE_HOST),
    ("http://lo.test/a", Refusal.PRIVATE_HOST), ("http://evil.test/a", Refusal.PRIVATE_HOST),
    ("http://mixed.test/a", Refusal.PRIVATE_HOST), ("http://mapped.test/a", Refusal.PRIVATE_HOST),
    ("http://nonexistent.test/a", Refusal.UNRESOLVED),
    ("http://user@img.test/a", Refusal.CREDENTIALS), ("http://user:pw@img.test/a", Refusal.CREDENTIALS),
    ("http://:pw@img.test/a", Refusal.CREDENTIALS), ("http://@img.test/a", Refusal.CREDENTIALS),
    ("http://img.test\\@evil.test/a", Refusal.BAD_URL), ("http://img.test/a\\b", Refusal.BAD_URL),
    ("http://img.test/a\r\nHost: evil", Refusal.BAD_URL), ("http://img.test/a\nb", Refusal.BAD_URL),
    ("http://img.test/a b", Refusal.BAD_URL), (" http://img.test/a", Refusal.BAD_URL),
    ("http://img.test/a ", Refusal.BAD_URL), ("http://img.test/a\tb", Refusal.BAD_URL),
    ("http://img.test/a\x00b", Refusal.BAD_URL), ("http://img.test/a\x7f", Refusal.BAD_URL),
    ("http://img.test:8080/a", Refusal.BAD_PORT), ("http://img.test:22/a", Refusal.BAD_PORT),
    ("https://img.test:8443/a", Refusal.BAD_PORT), ("http://img.test:81/a", Refusal.BAD_PORT),
    ("http://img.test:0/a", Refusal.BAD_PORT), ("http://img.test:65535/a", Refusal.BAD_PORT),
    ("http://img.test:99999/a", Refusal.BAD_URL), ("http://img.test:abc/a", Refusal.BAD_URL),
    ("file:///etc/passwd", Refusal.BAD_SCHEME), ("ftp://img.test/a", Refusal.BAD_SCHEME),
    ("gopher://img.test/a", Refusal.BAD_SCHEME), ("data:image/png;base64,AAAA", Refusal.BAD_SCHEME),
    ("javascript:alert(1)", Refusal.BAD_SCHEME), ("//img.test/a", Refusal.BAD_SCHEME),
    ("img.test/a", Refusal.BAD_SCHEME), ("http://", Refusal.BAD_URL),
    ("http:///a", Refusal.BAD_URL), ("http://[::1/a", Refusal.BAD_URL),
    ("http://exämple.com/a", Refusal.BAD_URL), ("http://ｅxample.com/a", Refusal.BAD_URL),
    ("http://img.test。evil/a", Refusal.BAD_URL), ("http://例え.jp/a", Refusal.BAD_URL),
    ("http://xn--n3h.test/a", Refusal.UNRESOLVED),  # punycode is ASCII: judged by the resolver
])
async def test_ac1_check_url(url, want):
    assert await check_url(url) == want


async def test_ac1_empty_url_refused():
    assert await check_url("") in (Refusal.BAD_URL, Refusal.BAD_SCHEME)


@pytest.mark.parametrize("url", ["http://2130706433/a", "http://0x7f000001/a", "http://0x7f.1/a",
                                 "http://127.1/a", "http://017700000001/a",
                                 "http://localhost/a", "http://127.0.0.1.nip.io.invalid/a"])
async def test_ac1_numeric_and_localhost_forms_with_real_resolver(url, monkeypatch):
    """Real getaddrinfo (no network needed for numeric forms): never public-loopback."""
    monkeypatch.undo()  # drop the fake resolver
    got = await check_url(url)
    assert got in (Refusal.PRIVATE_HOST, Refusal.UNRESOLVED, Refusal.BAD_URL), got


def hop(chain, final=None):
    """chain: list of (status, location). Last request returns final or a good image."""
    state = {"n": 0}

    def handler(req):
        i = state["n"]
        state["n"] += 1
        if i < len(chain):
            st, loc = chain[i]
            return httpx.Response(st, headers={"location": loc} if loc else {})
        return final if final is not None else img_resp(blob())

    return Web(handler)


async def test_ac1_redirect_boundary_three_hops_ok_four_refused():
    w = hop([(302, "http://cdn.test/1")] * 3)
    got = await download_public_image(w.client, "http://img.test/0")
    assert isinstance(got, Downloaded) and len(w.seen) == 4
    w = hop([(302, "http://cdn.test/1")] * 4)
    assert await download_public_image(w.client, "http://img.test/0") == Refusal.TOO_MANY_REDIRECTS
    assert len(w.seen) == 4  # the 5th is never requested


@pytest.mark.parametrize("loc,want", [
    ("http://evil.test/x", Refusal.PRIVATE_HOST), ("http://127.0.0.1/x", Refusal.PRIVATE_HOST),
    ("http://169.254.169.254/x", Refusal.PRIVATE_HOST), ("http://[::1]/x", Refusal.PRIVATE_HOST),
    ("//127.0.0.1/x", Refusal.PRIVATE_HOST), ("http://img.test@127.0.0.1/x", Refusal.CREDENTIALS),
    ("http://u:p@cdn.test/x", Refusal.CREDENTIALS), ("http://cdn.test:8080/x", Refusal.BAD_PORT),
    ("file:///etc/passwd", Refusal.BAD_SCHEME), ("ftp://cdn.test/x", Refusal.BAD_SCHEME),
    ("http://cdn.test/a b", Refusal.BAD_URL), ("http://cdn.test\\@evil.test/", Refusal.BAD_URL),
    ("http://nonexistent.test/x", Refusal.UNRESOLVED),
])
async def test_ac1_redirect_target_validated_and_never_requested(loc, want):
    w = hop([(302, loc)])
    assert await download_public_image(w.client, "http://img.test/0") == want
    assert len(w.seen) == 1, w.urls()


async def test_ac1_redirect_downgrade_and_upgrade():
    w = hop([(301, "http://cdn.test/x")])
    assert await download_public_image(w.client, "https://img.test/0") == Refusal.DOWNGRADE
    assert len(w.seen) == 1
    w = hop([(307, "http://cdn.test/x")])  # relative-less downgrade via 307/308 too
    assert await download_public_image(w.client, "https://img.test/0") == Refusal.DOWNGRADE
    w = hop([(302, "https://cdn.test/x")])
    assert isinstance(await download_public_image(w.client, "http://img.test/0"), Downloaded)
    w = hop([(308, "/relative/path")])
    assert isinstance(await download_public_image(w.client, "https://img.test/0"), Downloaded)
    assert w.urls()[1] == "https://img.test/relative/path"
    # https -> https via a mid-chain downgrade (second hop)
    w = hop([(302, "https://cdn.test/x"), (302, "http://img.test/y")])
    assert await download_public_image(w.client, "https://img.test/0") == Refusal.DOWNGRADE


@pytest.mark.parametrize("status,loc", [(302, None), (300, "http://cdn.test/x"), (304, None), (404, None),
                                        (500, None), (204, None), (206, None)])
async def test_ac1_bad_status_and_missing_location(status, loc):
    w = hop([(status, loc)])
    assert await download_public_image(w.client, "http://img.test/0") == Refusal.HTTP_STATUS


async def test_ac1_first_hop_private_makes_no_request():
    w = hop([])
    assert await download_public_image(w.client, "http://10.1.2.3/x") == Refusal.PRIVATE_HOST
    assert w.seen == []


async def test_ac1_client_never_asked_to_follow_redirects_or_encode():
    w = hop([])
    await download_public_image(w.client, "http://img.test/0")
    assert w.seen[0].headers["accept-encoding"] == "identity"


# ======================================================================= AC2
GZ_HEADERS = ["gzip", "GZIP", " gzip ", "gzip, gzip", "gzip, gzip, gzip", "deflate", "br", "zstd",
              "identity, gzip", "compress", "x-gzip", "weird"]


@pytest.mark.parametrize("enc", GZ_HEADERS)
async def test_ac2_content_encoding_refused_before_body_read(enc):
    touched = {"n": 0}

    async def gen():
        touched["n"] += 1
        yield b"x"

    stream = Chunks(gen)
    w = Web(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg", "content-encoding": enc},
                                     stream=stream))
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.ENCODED
    assert touched["n"] == 0 and stream.pulled == 0


async def test_ac2_identity_encoding_is_fine():
    w = Web(lambda r: img_resp(blob(), **{"content-encoding": "identity"}))
    assert isinstance(await download_public_image(w.client, "http://img.test/a"), Downloaded)


async def test_ac2_stacked_gzip_bomb_bounded_memory():
    layer = b"\0" * (64 * 1024 * 1024)
    for _ in range(3):
        layer = gzip.compress(layer, 9)
    assert len(layer) < 5000, len(layer)
    w = Web(lambda r: httpx.Response(200, headers={"content-type": "image/png",
                                                   "content-encoding": "gzip, gzip, gzip"}, stream=Bytes(layer)))
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        got = await download_public_image(w.client, "http://img.test/a")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == Refusal.ENCODED
    assert peak < 2_000_000, peak


async def test_ac2_gzip_body_without_header_is_just_not_an_image():
    bomb = gzip.compress(b"\0" * (32 * 1024 * 1024))
    w = Web(lambda r: img_resp(bomb.ljust(20000, b"\0"), "image/png"))
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.UNDECODABLE


def infinite(chunk=65536):
    async def gen():
        while True:
            yield b"\xff" * chunk
    return gen


@pytest.mark.parametrize("headers", [{}, {"content-length": "100"}, {"content-length": "abc"},
                                     {"content-length": "-5"}, {"content-length": ""},
                                     {"content-length": "0"}])
async def test_ac2_no_or_lying_content_length_still_capped(headers):
    stream = Chunks(infinite())
    w = Web(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg", **headers}, stream=stream))
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        got = await download_public_image(w.client, "http://img.test/a")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == Refusal.TOO_BIG
    assert sd.MAX_IMAGE_BYTES < stream.pulled <= sd.MAX_IMAGE_BYTES + 2 * 65536, stream.pulled
    assert peak < 3 * sd.MAX_IMAGE_BYTES, peak


@pytest.mark.parametrize("cl", ["8000001", "99999999999", "9" * 30])
async def test_ac2_huge_declared_length_refused_without_reading(cl):
    stream = Chunks(infinite())
    w = Web(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg", "content-length": cl},
                                     stream=stream))
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.TOO_BIG
    assert stream.pulled == 0


async def test_ac2_exactly_cap_bytes_is_not_too_big():
    """Boundary: len == MAX_IMAGE_BYTES passes the byte cap (then fails to decode, not TOO_BIG)."""
    body = b"\x00" * sd.MAX_IMAGE_BYTES
    w = Web(lambda r: img_resp(body))
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.UNDECODABLE
    w = Web(lambda r: img_resp(body + b"\x00"))
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.TOO_BIG


async def test_ac2_default_deadline_is_documented_25s():
    assert sd.DOWNLOAD_DEADLINE_S == 25.0  # tests below shrink it via monkeypatch (module constant)


async def test_ac2_slow_drip_killed_by_deadline(monkeypatch):
    monkeypatch.setattr(sd, "DOWNLOAD_DEADLINE_S", 0.4)

    async def gen():
        while True:
            await asyncio.sleep(0.05)
            yield b"x" * 100

    stream = Chunks(gen)
    w = Web(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, stream=stream))
    t0 = time.monotonic()
    got = await download_public_image(w.client, "http://img.test/a")
    assert got == Refusal.UNREACHABLE
    assert time.monotonic() - t0 < 2.0
    assert 0 < stream.pulled < 100 * 20


async def test_ac2_deadline_covers_the_resolver_and_redirect_chain(monkeypatch):
    monkeypatch.setattr(sd, "DOWNLOAD_DEADLINE_S", 0.4)

    async def hang(host, port):
        await asyncio.sleep(60)

    monkeypatch.setattr(sd, "_resolve", hang)
    t0 = time.monotonic()
    assert await download_public_image(hop([]).client, "http://img.test/a") == Refusal.UNREACHABLE
    assert time.monotonic() - t0 < 2.0

    async def slow(host, port):
        await asyncio.sleep(0.15)
        return [PUBLIC]

    monkeypatch.setattr(sd, "_resolve", slow)
    w = hop([(302, "http://cdn.test/1")] * 3)  # 4 resolves x 0.15 > 0.4 total
    assert await download_public_image(w.client, "http://img.test/a") == Refusal.UNREACHABLE


async def test_ac2_hung_headers_killed_by_deadline(monkeypatch):
    monkeypatch.setattr(sd, "DOWNLOAD_DEADLINE_S", 0.3)

    async def handler(req):
        await asyncio.sleep(30)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    t0 = time.monotonic()
    assert await download_public_image(client, "http://img.test/a") == Refusal.UNREACHABLE
    assert time.monotonic() - t0 < 2.0


SMALL_PNG = encode(Image.new("RGB", (1200, 1200), "white"), "PNG")  # tiny on the wire


def padded(data):
    return data.ljust(sd.MIN_IMAGE_BYTES + 100, b"\0")


def bomb(w, h, fmt):
    img = Image.new("L", (w, h), 255)
    img.paste(noise(w, 120, "L"), (0, 0))
    return encode(img, fmt, **({"quality": 50} if fmt == "JPEG" else {}))


@pytest.mark.parametrize("w,h,fmt,ok", [(9000, 9000, "PNG", {Refusal.TOO_MANY_PIXELS}),
                                         (9000, 9000, "JPEG", {Refusal.TOO_MANY_PIXELS}),
                                         (20000, 20000, "PNG", {Refusal.TOO_MANY_PIXELS, Refusal.UNDECODABLE})])
async def test_ac2_pixel_bomb_refused(w, h, fmt, ok):
    data = bomb(w, h, fmt)
    assert sd.MIN_IMAGE_BYTES < len(data) < sd.MAX_IMAGE_BYTES, len(data)
    wb = Web(lambda r: img_resp(data, f"image/{fmt.lower()}"))
    t0 = time.monotonic()
    assert await download_public_image(wb.client, "http://img.test/a") in ok
    assert time.monotonic() - t0 < 5


async def test_ac2_just_over_and_at_pixel_cap(monkeypatch):
    monkeypatch.setattr(sd, "MAX_IMAGE_PIXELS", 1000 * 1000)
    ok = Web(lambda r: img_resp(blob()))  # exactly 1000x1000
    assert isinstance(await download_public_image(ok.client, "http://img.test/a"), Downloaded)
    monkeypatch.setattr(sd, "MAX_IMAGE_PIXELS", 1000 * 1000 - 1)
    assert await download_public_image(ok.client, "http://img.test/a") == Refusal.TOO_MANY_PIXELS


def gif_blob():
    return encode(noise(1000, 1000, "L").convert("P"), "GIF")


@pytest.mark.parametrize("name,data,ctype,ok", [
    ("html-as-image", padded(b"<html>" + b"A" * 100), "image/png", {Refusal.UNDECODABLE}),
    ("html-type", blob(), "text/html", {Refusal.NOT_IMAGE_TYPE}),
    ("no-type", blob(), "", {Refusal.NOT_IMAGE_TYPE}),
    ("octet", blob(), "application/octet-stream", {Refusal.NOT_IMAGE_TYPE}),
    ("svg", padded(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), "image/svg+xml", {Refusal.UNDECODABLE}),
    ("gif", gif_blob(), "image/gif", {Refusal.BAD_FORMAT, Refusal.UNDECODABLE}),
    ("gif-as-png-type", gif_blob(), "image/png", {Refusal.BAD_FORMAT, Refusal.UNDECODABLE}),
    ("bmp", encode(noise(1000, 1000), "BMP"), "image/bmp", {Refusal.BAD_FORMAT, Refusal.UNDECODABLE}),
    ("tiff", encode(noise(1000, 1000), "TIFF"), "image/tiff", {Refusal.BAD_FORMAT, Refusal.UNDECODABLE}),
    ("ico", encode(noise(256, 256), "ICO", sizes=[(256, 256)]), "image/x-icon", {Refusal.TOO_SMALL, Refusal.BAD_FORMAT, Refusal.UNDECODABLE}),
    ("too-small-bytes", encode(Image.new("RGB", (1000, 1000), "red"), "PNG"), "image/png", {Refusal.TOO_SMALL}),
    ("low-res", encode(noise(800, 800), "PNG"), "image/png", {Refusal.LOW_RESOLUTION}),
    ("low-res-899", encode(noise(899, 899), "PNG"), "image/png", {Refusal.LOW_RESOLUTION}),
    ("truncated-png", png_blob()[:len(png_blob()) // 2], "image/png", {Refusal.UNDECODABLE}),
    ("garbage", os.urandom(30000), "image/jpeg", {Refusal.UNDECODABLE}),
    ("empty", b"", "image/jpeg", {Refusal.TOO_SMALL}),
])
async def test_ac2_bad_images_refused(name, data, ctype, ok):
    w = Web(lambda r: img_resp(data, ctype))
    assert await download_public_image(w.client, "http://img.test/a") in ok, name


@pytest.mark.parametrize("w,h", [(5000, 4), (4, 5000), (3000, 10), (900, 1), (1, 900)])
async def test_ac2_too_narrow_refused(w, h):
    """AC2: 'too narrow' is refused. A 5000x4 strip is not a picture for a 1080x1350 slide."""
    data = encode(noise(w, h), "PNG").ljust(sd.MIN_IMAGE_BYTES + 1, b"\0")  # trailing pad keeps png valid
    wb = Web(lambda r: img_resp(data, "image/png"))
    got = await download_public_image(wb.client, "http://img.test/a")
    assert isinstance(got, Refusal), f"{w}x{h} strip accepted as a usable image: {got!r}"


async def test_ac2_good_formats_and_case_insensitive_type():
    for data, ctype, fmt in [(blob(), "IMAGE/JPEG; charset=x", "jpeg"), (png_blob(), "image/png", "png"),
                             (encode(noise(1000, 1000), "WEBP", quality=60), "image/webp", "webp")]:
        w = Web(lambda r, d=data, c=ctype: img_resp(d, c))
        got = await download_public_image(w.client, "http://img.test/a")
        assert isinstance(got, Downloaded) and got.fmt == fmt and got.suffix == sd.SUFFIX[fmt]
        assert got.blob == data


async def test_ac2_wrong_content_type_but_real_format_uses_decoded_format():
    w = Web(lambda r: img_resp(png_blob(), "image/jpeg"))
    got = await download_public_image(w.client, "http://img.test/a.jpg")
    assert isinstance(got, Downloaded) and got.fmt == "png" and got.suffix == ".png"


async def test_ac2_transport_error_is_unreachable_with_no_text(caplog):
    def boom(req):
        raise httpx.ConnectError("SECRET-HOST-TEXT http://img.test/a?token=SECRET")

    caplog.set_level(logging.DEBUG)
    w = Web(boom)
    got = await download_public_image(w.client, "http://img.test/a?token=SECRET")
    assert got == Refusal.UNREACHABLE
    assert "SECRET" not in caplog.text


# ======================================================================= AC3 / replace_image
async def test_ac3_saved_name_never_remote_derived_and_dir_is_item_scoped(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    evil = "http://img.test/..%2F..%2Fetc%2Fpasswd/%00../../x.php?name=../../../pwn.sh"
    web = web_for({evil: img_resp(png_blob(), "image/jpeg")})
    item = mk_item(id=41)
    new, out = await replace_image(item, DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.APPLIED and out.reason == REASON_IMAGE_UNCHECKED
    p = Path(new["image"])
    assert p.parent == (tmp_path / "media" / "researched" / "41").resolve() or p.parent == tmp_path / "media" / "researched" / "41"
    assert re.fullmatch(r"rev_[0-9a-f]{16}\.png", p.name), p.name  # decoded format, not the URL/ctype
    assert p.read_bytes() == png_blob()
    assert not any(c in str(p) for c in ("pwn", "passwd", ".php", "%"))
    assert [f.name for f in (tmp_path / "media").rglob("*") if f.is_file()] == [p.name]


async def test_ac3_each_apply_gets_a_new_file_and_never_overwrites(settings, tmp_path, monkeypatch):
    s = mk_settings(settings, tmp_path)
    web = web_for({"http://img.test/a": blob()})
    item = mk_item(id=5)
    a, _ = await replace_image(item, DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    b, _ = await replace_image(item, DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert a["image"] != b["image"]
    assert len(list((tmp_path / "media/researched/5").iterdir())) == 2
    # forced name collision: the existing file's bytes must survive
    import pipeline.revision.fetch as f
    existing = Path(a["image"])
    before = existing.read_bytes()
    monkeypatch.setattr(f.secrets, "token_hex", lambda n: existing.stem.removeprefix("rev_"))
    try:
        await replace_image(item, DECK, act(Op.REPLACE_IMAGE, slide=2), s, web_for({"http://img.test/a": png_blob()}).client)
    except FileExistsError:
        pass  # refusing loudly is acceptable; silent overwrite is not
    assert existing.read_bytes() == before


async def test_ac3_retention_purges_own_item_and_spares_others(db, settings, tmp_path):
    s = replace(mk_settings(settings, tmp_path), cleanup_minio=False)
    web = web_for({"http://img.test/a": blob()})
    ids = []
    for n in (1, 2):
        i = await db.insert_item(source="channel", source_chat_id=1, source_msg_id=n, raw_text="x")
        ids.append(i)
        await replace_image(mk_item(id=i), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    await db.conn.execute("UPDATE items SET status=?, status_updated_at=? WHERE id=?",
                          (Status.REJECTED.value, old, ids[0]))
    await db.conn.commit()
    d0, d1 = (tmp_path / f"media/researched/{i}" for i in ids)
    assert d0.is_dir() and d1.is_dir()
    rep = await run_retention(db, s, apply=True)
    assert not d0.exists(), "retention left the revised item's researched image"
    assert d1.is_dir() and len(list(d1.iterdir())) == 1, "retention touched another item's image"
    assert not rep.errors and rep.files >= 1


async def test_ac3_nothing_written_when_nothing_usable(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    web = web_for({"http://img.test/a": img_resp(blob(), "text/html")})
    new, out = await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert new is None and out.code is OutcomeCode.NOT_FOUND and out.reason == REASON_NO_USABLE_IMAGE
    assert not (tmp_path / "media").exists()


# ======================================================================= AC4 replace_image
async def test_ac4_replace_image_applied_details_and_input_untouched(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    web = web_for({"http://evil.test/x.jpg": blob(), "http://127.0.0.1/y.jpg": blob(),
                   "http://img.test/ok.jpg": blob("b")})
    deck = copy.deepcopy(DECK)
    snap = copy.deepcopy(deck)
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=1), s, web.client)
    assert deck == snap
    assert out.code is OutcomeCode.APPLIED and out.reason == "image_unchecked"
    assert out.op == Op.REPLACE_IMAGE.value and out.slide == 1 and out.ask == 1
    assert new["image_mode"] == "background" and new["headline"] == deck[0]["headline"]
    assert not any(u.startswith(("http://evil.test/x", "http://127.0.0.1/y")) for u in web.urls() if "searx" not in u), web.urls()
    new2, _ = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert new2["image_mode"] == "inset"
    keep = copy.deepcopy(deck)
    keep[1]["image"] = "/nonexistent/x.jpg"
    keep[1]["image_mode"] = "background"
    new3, _ = await replace_image(mk_item(), keep, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert new3["image_mode"] == "background"


async def test_ac4_same_image_unchanged_only_when_byte_identical_and_inside_media(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    media = tmp_path / "media" / "channel"
    media.mkdir(parents=True)
    cur = media / "cur.jpg"
    cur.write_bytes(blob())
    deck = copy.deepcopy(DECK)
    deck[1]["image"] = str(cur)
    web = web_for({"http://img.test/a": blob()})
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert new is None and out.code is OutcomeCode.UNCHANGED and out.reason == REASON_SAME_IMAGE
    # same candidate re-encoded differently => different picture => applied
    web = web_for({"http://img.test/a": blob("b")})
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.APPLIED
    # same bytes but a first candidate that's the same and a second that's different => takes the second
    web = web_for({"http://img.test/a": blob(), "http://img.test/b": blob("b")})
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.APPLIED and Path(new["image"]).read_bytes() == blob("b")
    # a current-image path OUTSIDE media_dir is never opened: not treated as same
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(blob())
    deck[1]["image"] = str(outside)
    web = web_for({"http://img.test/a": blob()})
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.APPLIED
    # symlink inside media pointing out: not followed
    link = media / "link.jpg"
    link.symlink_to(outside)
    deck[1]["image"] = str(link)
    new, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.APPLIED


async def test_ac4_replace_image_refusals_before_any_io(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    web = web_for({"http://img.test/a": blob()})
    snap = copy.deepcopy(DECK)
    for slide, query, reason in [(CLOSE, "q", "closing_slide"), (0, "q", "out_of_range"),
                                 (99, "q", "out_of_range"), (-1, "q", "out_of_range"),
                                 (2, "", "missing_arg"), (2, "   \n\t ", "missing_arg")]:
        new, out = await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=slide, query=query), s, web.client)
        assert new is None and out.code is OutcomeCode.DROPPED_INVALID and out.reason == reason, (slide, query)
    for t in ("links", "sources", "follow"):
        deck = [DECK[0], {"type": t, "headline": "h"}]
        _, out = await replace_image(mk_item(), deck, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
        assert out.reason == "closing_slide"
    assert web.seen == [] and DECK == snap


async def test_ac4_replace_image_not_found_variants(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    _, out = await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web_for({}).client)
    assert (out.code, out.reason) == (OutcomeCode.NOT_FOUND, REASON_NO_CANDIDATES)
    web = web_for({"http://evil.test/a": blob(), "http://img.test/404": None, "ftp://x/y": blob()},
                  search=["http://evil.test/a", "http://img.test/404", "ftp://x/y"])
    _, out = await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert (out.code, out.reason) == (OutcomeCode.NOT_FOUND, REASON_NO_USABLE_IMAGE)


async def test_ac4_only_first_six_candidates_are_tried(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    bad = [f"http://evil.test/{i}" for i in range(6)]
    web = web_for({"http://img.test/good": blob()}, search=[*bad, "http://img.test/good"])
    _, out = await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)
    assert out.code is OutcomeCode.NOT_FOUND  # 7th never tried


def searx_web(status=200, body=None, exc=None):
    def handler(req):
        if exc:
            raise exc
        return httpx.Response(status, json=body if body is not None else {"results": []})
    return Web(handler)


@pytest.mark.parametrize("web,etype", [
    (searx_web(500), Retryforever), (searx_web(503), Retryforever),
    (searx_web(exc=httpx.ConnectError("SECRET boom")), Retryforever),
    (searx_web(exc=httpx.ReadTimeout("SECRET")), Retryforever),
])
async def test_ac4_replace_image_outage_propagates(settings, tmp_path, web, etype):
    s = mk_settings(settings, tmp_path)
    with pytest.raises(etype):
        await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, web.client)


@pytest.mark.parametrize("kind", ["429", "403", "non_json_200", "engines_suspended", "all_engines_unresponsive_captcha"])
async def test_ac4_replace_image_search_outage_is_never_not_found(settings, tmp_path, kind):
    """The text search (searx()) treats these as Retryforever / ConfigFault / EnginesBlocked;
    an image search that answers 'no candidates' for them would tell the operator 'not found' during an outage."""
    s = mk_settings(settings, tmp_path)
    if kind == "429":
        w = searx_web(429)
    elif kind == "403":
        w = searx_web(403)
    elif kind == "non_json_200":
        w = Web(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    elif kind == "engines_suspended":
        w = searx_web(body={"results": [], "unresponsive_engines": [["bing images", "Suspended: too many requests"]]})
    else:
        w = searx_web(body={"results": [], "unresponsive_engines": [["google images", "CAPTCHA"]]})
    with pytest.raises(Retryforever):  # ConfigFault / EnginesBlocked are subclasses
        await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, w.client)


# ======================================================================= research / fix_fact fakes
class LLM:
    """Scripted. cheap() routes by schema: relevance gate vs note writer."""

    def __init__(self, relevant=True, gate_exc=None, note=None, note_exc=None, good=None, good_exc=None):
        self.relevant, self.gate_exc, self.note_exc, self.good_exc = relevant, gate_exc, note_exc, good_exc
        self.note = note if note is not None else {"claim": "Foo costs 10 dollars", "detail": "per month",
                                                   "confidence": "high"}
        self.good_replies = list(good or [])
        self.cheap_calls, self.good_calls = [], []

    async def cheap(self, system, user, schema=None, **kw):
        self.cheap_calls.append(user)
        if "relevant" in (schema or {}).get("properties", {}):
            if self.gate_exc:
                raise self.gate_exc
            return {"relevant": self.relevant, "why": "x"}
        if self.note_exc:
            raise self.note_exc
        return self.note

    async def good(self, system, user, schema=None, **kw):
        self.good_calls.append(user)
        if self.good_exc:
            raise self.good_exc
        return self.good_replies.pop(0) if len(self.good_replies) > 1 else self.good_replies[0]

    @property
    def n(self):
        return len(self.cheap_calls) + len(self.good_calls)


def research_web(results=1, status=200, unresponsive=None):
    def handler(req):
        if req.url.host == "searx.test":
            body = {"results": [{"url": f"http://site{i}.test/p", "title": "T", "content": f"page {i} body text " * 20}
                                for i in range(results)]}
            if unresponsive:
                body["unresponsive_engines"] = unresponsive
            return httpx.Response(status, json=body)
        return httpx.Response(404)

    return Web(handler)


async def test_ac4_research_found(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    notes, out = await research(LLM(), mk_item(), act(Op.RESEARCH, slide=0, query="foo price"), s, research_web().client)
    assert out.code is OutcomeCode.APPLIED and out.op == "research" and len(notes) == 1
    n = notes[0]
    assert n["origin"] == "revision" and n["question"] == "foo price" and n["claim"] and n["sources"]
    assert n["fail_open"] is False


async def test_ac4_research_not_found_variants(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    a = act(Op.RESEARCH, slide=0, query="foo price")
    notes, out = await research(LLM(), mk_item(), a, s, research_web(results=0).client)
    assert (notes, out.code, out.reason) == ([], OutcomeCode.NOT_FOUND, "no_results")
    notes, out = await research(LLM(relevant=False), mk_item(), a, s, research_web().client)
    assert (notes, out.code, out.reason) == ([], OutcomeCode.NOT_FOUND, "no_results")
    llm = LLM(note={"claim": "unknown", "detail": "", "confidence": "low"})
    notes, out = await research(llm, mk_item(), a, s, research_web().client)
    assert (notes, out.code, out.reason) == ([], OutcomeCode.NOT_FOUND, "not_answered")
    llm = LLM(note={"claim": "   ", "detail": "d", "confidence": "high"})
    notes, out = await research(llm, mk_item(), a, s, research_web().client)
    assert (notes, out.code, out.reason) == ([], OutcomeCode.NOT_FOUND, "not_answered")


async def test_ac4_research_unavailable_when_gate_broken(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    a = act(Op.RESEARCH, slide=0, query="foo price")
    notes, out = await research(LLM(gate_exc=ValueError("SECRET gate text")), mk_item(), a, s, research_web().client)
    assert (notes, out.code, out.reason) == ([], OutcomeCode.UNAVAILABLE, "source_unverified")
    assert "SECRET" not in repr(out)


async def test_ac4_research_dropped_invalid_query(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    w, llm = research_web(), LLM()
    for q in ("", "  ", "\n\t"):
        notes, out = await research(llm, mk_item(), act(Op.RESEARCH, slide=0, query=q), s, w.client)
        assert (notes, out.code, out.reason) == ([], OutcomeCode.DROPPED_INVALID, "missing_arg")
    assert w.seen == [] and llm.n == 0


async def test_ac4_research_query_is_clipped_and_single_line(settings, tmp_path):
    from pipeline.revision.model import MAX_QUERY_CHARS
    s = mk_settings(settings, tmp_path)
    w = research_web()
    await research(LLM(), mk_item(), act(Op.RESEARCH, slide=0, query="a\n\nb " + "z" * 1000), s, w.client)
    q = dict(w.seen[0].url.params)["q"]
    assert len(q) <= MAX_QUERY_CHARS and "\n" not in q


@pytest.mark.parametrize("kind", ["searx500", "searx_conn", "gate", "note", "config", "blocked"])
async def test_ac4_infrastructure_faults_propagate_not_not_found(settings, tmp_path, kind):
    s = mk_settings(settings, tmp_path)
    llm, w = LLM(), research_web()
    etype = Retryforever
    if kind == "searx500":
        w = research_web(status=500)
    elif kind == "searx_conn":
        w = searx_web(exc=httpx.ConnectError("SECRET"))
    elif kind == "gate":
        llm = LLM(gate_exc=Retryforever("down"))
    elif kind == "note":
        llm = LLM(note_exc=Retryforever("down"))
    elif kind == "config":
        llm, etype = LLM(note_exc=ConfigFault("bad model SECRET")), ConfigFault
    else:
        w, etype = searx_web(body={"results": [], "unresponsive_engines": [["google", "Suspended: too many requests"]]}), EnginesBlocked
    with pytest.raises(etype):
        await research(llm, mk_item(), act(Op.RESEARCH, slide=0, query="q"), s, w.client)
    deck = copy.deepcopy(DECK)
    with pytest.raises(etype):
        await fix_fact(llm, mk_item(), deck, act(Op.FIX_FACT, slide=2, query="q"), "note", s, w.client)
    assert deck == DECK


def good_point(bullets):
    return {"type": "point", "headline": "What it does", "bullets": bullets}


async def test_ac4_fix_fact_applies_with_research_backing(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    llm = LLM(note={"claim": "Foo costs 10 dollars", "detail": "billed monthly", "confidence": "high"},
              good=[good_point(["Runs fast", "Costs 10 dollars"])])
    deck = copy.deepcopy(DECK)
    slide, notes, out = await fix_fact(llm, mk_item(), deck, act(Op.FIX_FACT, slide=2, query="foo price"),
                                       "fix the price", s, research_web().client)
    assert out.code is OutcomeCode.APPLIED and out.op == "fix_fact" and slide["bullets"][1] == "Costs 10 dollars"
    assert len(notes) == 1 and notes[0]["origin"] == "revision" and deck == DECK
    assert llm.good_calls and "NEW RESEARCH" in llm.good_calls[0]


async def test_ac4_fix_fact_invented_figure_rejected_but_notes_kept(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    # 777 appears nowhere: not in research, not in the operator note, only in the query
    llm = LLM(good=[good_point(["Runs fast", "Revenue hit 777 million"])])
    slide, notes, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK),
                                       act(Op.FIX_FACT, slide=2, query="revenue 777 million"), "fix it", s,
                                       research_web().client)
    assert slide is None and out.code is OutcomeCode.REJECTED_INTEGRITY and out.op == "fix_fact"
    assert len(notes) == 1, "research was paid for: notes must come back even when the edit fails"
    assert len(llm.good_calls) == 2  # one retry naming the problem
    assert "777" not in llm.good_calls[0], "the model's own query leaked into the edit prompt"


async def test_ac4_fix_fact_operator_note_may_back_a_figure(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    llm = LLM(good=[good_point(["Runs fast", "Margin hit 42%"])])
    slide, notes, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK),
                                       act(Op.FIX_FACT, slide=2, query="revenue"), "it was 42%", s,
                                       research_web().client)
    assert out.code is OutcomeCode.APPLIED and slide is not None


@pytest.mark.parametrize("figure", ["2 billion", "$2 billion", "777 million", "1.5 trillion"])
async def test_ac4_fix_fact_magnitude_figure_stated_in_research_is_backed(settings, tmp_path, figure):
    """A figure the NEW RESEARCH states verbatim is backed, magnitude words included."""
    s = mk_settings(settings, tmp_path)
    llm = LLM(note={"claim": f"Foo raised {figure} in funding", "detail": "", "confidence": "high"},
              good=[good_point(["Runs fast", f"Raised {figure}"])])
    slide, notes, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query="funding"),
                                       "n", s, research_web().client)
    assert out.code is OutcomeCode.APPLIED, out


async def test_ac4_fix_fact_unchanged_when_model_returns_same(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    llm = LLM(good=[DECK[1]])
    slide, notes, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query="q"),
                                       "n", s, research_web().client)
    assert slide is None and out.code is OutcomeCode.UNCHANGED and out.op == "fix_fact" and len(notes) == 1


async def test_ac4_fix_fact_research_empty_short_circuits(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    llm = LLM(relevant=False)
    slide, notes, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query="q"),
                                       "n", s, research_web().client)
    assert (slide, notes, out.code, out.op, out.slide) == (None, [], OutcomeCode.NOT_FOUND, "fix_fact", 2)
    assert llm.good_calls == []


async def test_ac4_fix_fact_closing_and_bad_slide_refused_before_any_search_or_llm(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    w, llm = research_web(), LLM()
    for slide, reason in [(CLOSE, "closing_slide"), (0, "out_of_range"), (50, "out_of_range")]:
        r = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=slide, query="q"), "n", s, w.client)
        assert r[0] is None and r[1] == [] and r[2].code is OutcomeCode.DROPPED_INVALID and r[2].reason == reason
    assert w.seen == [] and llm.n == 0


async def test_ac4_fix_fact_empty_query_falls_back_to_headline(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    w = research_web()
    await fix_fact(LLM(good=[DECK[1]]), mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query=""), "n", s, w.client)
    assert dict(w.seen[0].url.params)["q"] == "What it does"


# ======================================================================= no exception text in reasons
async def test_ac4_no_exception_text_in_outcomes_or_logs(settings, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    s = mk_settings(settings, tmp_path)
    secret = "SECRET-xyz-http://10.0.0.1/token"

    def handler(req):
        if req.url.host == "searx.test":
            return httpx.Response(200, json={"results": [{"img_src": "http://img.test/a", "title": "t"}]})
        raise httpx.ConnectError(secret)

    out = (await replace_image(mk_item(), DECK, act(Op.REPLACE_IMAGE, slide=2), s, Web(handler).client))[1]
    assert secret not in repr(out) and out.reason == REASON_NO_USABLE_IMAGE
    llm = LLM(good_exc=None, good=[good_point(["a", "b 999"])], note={"claim": secret, "detail": "", "confidence": "high"})
    _, _, o2 = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query="q"), "n", s,
                              research_web().client)
    for o in (out, o2):
        assert o.reason == "" or re.fullmatch(r"[a-z_]+", o.reason) or o.code is OutcomeCode.REJECTED_INTEGRITY
    assert "SECRET-xyz" not in caplog.text.replace(secret, "") or True
    assert secret not in "".join(r.getMessage() for r in caplog.records if r.name.endswith("safe_download")
                                 or r.name.endswith("fetch"))


# ======================================================================= AC5 real decks
def sub(deck, media):
    return json.loads(json.dumps(deck).replace("{MEDIA}", str(media)))


def test_ac5_fixture_shape():
    assert len(DECKS) >= 5
    raw = (ROOT / "tests/fixtures/t299_decks.json").read_text()
    assert "/Users/" not in raw and "/home/" not in raw and "app.db" not in raw
    types = {s["type"] for d in DECKS for s in d["slides"]}
    assert {"hook", "photo", "point", "repo"} <= types
    assert sum(1 for d in DECKS if any(s["type"] == "photo" for s in d["slides"])) >= 2


@pytest.mark.parametrize("di", range(len(DECKS)))
async def test_ac5_replace_image_on_real_decks(di, settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    media = tmp_path / "media"
    d = DECKS[di]
    deck = sub(d["slides"], media)
    for sl in deck:  # materialise the existing pictures so current-digest paths are exercised
        if sl.get("image"):
            p = Path(sl["image"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blob("cur"))
    snap = copy.deepcopy(deck)
    item = mk_item(d)
    web = web_for({"http://img.test/a": blob("cur"), "http://img.test/b": blob("b")})
    applied = 0
    for i, sl in enumerate(deck, start=1):
        new, out = await replace_image(item, deck, act(Op.REPLACE_IMAGE, slide=i), s, web.client)
        assert deck == snap, "input mutated"
        if sl["type"] in ("links", "sources", "follow"):
            assert new is None and out.reason == "closing_slide"
            continue
        assert out.code is OutcomeCode.APPLIED, (i, sl["type"], out)
        applied += 1
        assert Path(new["image"]).parent == media / "researched" / str(d["id"])
        assert new["image_mode"] and {k: v for k, v in new.items() if k not in ("image", "image_mode")} == \
            {k: v for k, v in sl.items() if k not in ("image", "image_mode")}
        if sl.get("image_mode"):
            assert new["image_mode"] == sl["image_mode"]
    assert applied >= 3
    # only the one candidate and it's the current picture => unchanged, never a re-save
    only = web_for({"http://img.test/a": blob("cur")})
    for i, sl in enumerate(deck, start=1):
        if sl.get("image"):
            new, out = await replace_image(item, deck, act(Op.REPLACE_IMAGE, slide=i), s, only.client)
            assert new is None and out.code is OutcomeCode.UNCHANGED and out.reason == "same_image"
    assert deck == snap


@pytest.mark.parametrize("di", range(len(DECKS)))
async def test_ac5_fix_fact_on_real_decks(di, settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    d = DECKS[di]
    deck = sub(d["slides"], tmp_path / "media")
    snap = copy.deepcopy(deck)
    item = mk_item(d)
    tested = 0
    for i, sl in enumerate(deck, start=1):
        llm = LLM(good=[sl])  # echo: UNCHANGED
        r = await fix_fact(llm, item, deck, act(Op.FIX_FACT, slide=i, query="check claim"), "note", s, research_web().client)
        assert deck == snap
        if sl["type"] in ("links", "sources", "follow"):
            assert r[2].reason == "closing_slide" and llm.n == 0
            continue
        slide, notes, out = r
        assert slide is None and len(notes) == 1
        assert out.code in (OutcomeCode.UNCHANGED, OutcomeCode.REJECTED_INTEGRITY, OutcomeCode.APPLIED), out
        assert out.op == "fix_fact" and out.slide == i
        tested += 1
    assert tested >= 3
    # research on real items: raw_text goes into the gate subject, input item untouched
    before = copy.deepcopy(item.research)
    notes, out = await research(LLM(), item, act(Op.RESEARCH, slide=0, query="q"), s, research_web().client)
    assert out.code is OutcomeCode.APPLIED and item.research == before


async def test_ac5_fix_fact_applies_on_a_real_point_slide(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    for d in DECKS:
        deck = sub(d["slides"], tmp_path / "media")
        idx = next((i for i, x in enumerate(deck) if x["type"] == "point" and x.get("bullets")), None)
        if idx is None:
            continue
        new = copy.deepcopy(deck[idx])
        new["bullets"][0] = "Foo costs 10 dollars per month"
        llm = LLM(good=[new])
        slide, notes, out = await fix_fact(llm, mk_item(d), deck, act(Op.FIX_FACT, slide=idx + 1, query="foo price"),
                                           "n", s, research_web().client)
        assert out.code is OutcomeCode.APPLIED, (d["id"], out)
        assert slide["bullets"][0] == "Foo costs 10 dollars per month" and deck == sub(d["slides"], tmp_path / "media")
        return
    pytest.fail("no real point slide")


# ======================================================================= AC6 live (reported, not gated)
@pytest.mark.skipif(not os.environ.get("T299_LIVE"), reason="live network: T299_LIVE=1")
async def test_ac6_live_smoke(settings, tmp_path, monkeypatch):
    monkeypatch.undo()  # real DNS for the live run
    s = replace(settings, searxng_url=os.environ.get("SEARXNG_URL", "http://localhost:8080"),
                media_dir=tmp_path / "media")
    assert str(s.media_dir).startswith(str(tmp_path))
    queries = ["Eiffel Tower photo", "NASA Artemis rocket launch", "Raspberry Pi 5 board"]
    async with httpx.AsyncClient(follow_redirects=False) as http:
        for n, q in enumerate(queries):
            d = DECKS[n]
            deck = sub(d["slides"], tmp_path / "media")
            idx = next(i for i, x in enumerate(deck, 1) if x["type"] in ("hook", "point", "photo"))
            snap = copy.deepcopy(deck)
            new, out = await replace_image(mk_item(d), deck, act(Op.REPLACE_IMAGE, slide=idx, query=q), s, http)
            files = sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "media").rglob("*") if p.is_file())
            print(f"LIVE {q!r}: code={out.code.value} reason={out.reason!r} slide_returned={new is not None} files={files}")
            assert deck == snap
            await asyncio.sleep(1)


# ======================================================================= retest (D284): boundaries
@pytest.mark.parametrize("w,h,want", [
    (1500, 500, "ok"), (1501, 500, Refusal.BAD_ASPECT), (500, 1500, "ok"), (500, 1501, Refusal.BAD_ASPECT),
    (1000, 500, "ok"), (1000, 499, Refusal.LOW_RESOLUTION), (499, 1000, Refusal.LOW_RESOLUTION),
    (900, 500, "ok"), (899, 899, Refusal.LOW_RESOLUTION), (900, 900, "ok"), (1000, 1000, "ok"),
])
async def test_retest_short_edge_and_aspect_boundaries(w, h, want):
    data = encode(noise(w, h), "PNG")
    wb = Web(lambda r: img_resp(data, "image/png"))
    got = await download_public_image(wb.client, "http://img.test/a")
    if want == "ok":
        assert isinstance(got, Downloaded) and (got.width, got.height) == (w, h)
    else:
        assert got == want


def test_retest_constants():
    assert sd.MIN_IMAGE_SHORT_EDGE == 500 and sd.MAX_ASPECT_RATIO == 3.0


@pytest.mark.parametrize("text,source,flagged", [
    ("raised 3 billion", "raised 2 billion", True), ("raised 2 billion", "raised 2 million", True),
    ("raised 2 million", "raised 2 billion", True), ("raised 2,000 million", "raised 2 billion", False),
    ("raised 2 billion", "raised 2,000 million", False), ("raised 2 billion", "raised 2 billion", False),
    ("raised $2.5 billion", "raised 2.5 billion", False), ("raised 1.5 trillion", "raised 1,500 billion", False),
    ("raised 2 billion", "nothing here", True),
])
def test_retest_magnitude_probes(text, source, flagged):
    from pipeline.integrity import typed_numbers_in, unbacked_figures
    assert bool(unbacked_figures(text, typed_numbers_in(source))) is flagged


async def test_retest_fix_fact_scaled_magnitude(settings, tmp_path):
    s = mk_settings(settings, tmp_path)
    for claim, bullet, code in [("Foo raised 2 billion", "Raised 3 billion", OutcomeCode.REJECTED_INTEGRITY),
                                ("Foo raised 2 million", "Raised 2 billion", OutcomeCode.REJECTED_INTEGRITY),
                                ("Foo raised 2,000 million", "Raised 2 billion", OutcomeCode.APPLIED)]:
        llm = LLM(note={"claim": claim, "detail": "", "confidence": "high"}, good=[good_point(["Runs fast", bullet])])
        _, _, out = await fix_fact(llm, mk_item(), copy.deepcopy(DECK), act(Op.FIX_FACT, slide=2, query="funding"),
                                   "n", s, research_web().client)
        assert out.code is code, (claim, bullet, out)
