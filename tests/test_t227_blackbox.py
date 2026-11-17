"""T227 blackbox: the LIVE research path (fetch_text / download_image / download_avatar).

No test reads data/app.db. The client is built EXACTLY as ``__main__`` does
(``httpx.AsyncClient(follow_redirects=True, transport=RoutingTransport())``).
Attack servers are bound on 127.0.0.1 / ::1 by the test. "Public" hosts are fake
DNS names (``sd._resolve`` patched) whose vetted public IP is dialled to the local
server by a ``MapBackend`` that sits BEHIND the real ``VettingBackend``: the real
vetting, pinning and redirect code runs; only the final TCP dial is redirected.
MapBackend also maps 127.0.0.1:80 / ::1:80 to the local server, so a vetting
failure would show up as a recorded request (a zero count is then meaningful).
Real network only with ``T227_LIVE=1`` (AC6).
"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import logging
import os
import socket
import threading
import time
import tracemalloc
import zlib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from PIL import Image

from pipeline import safe_download as sd
from pipeline import search
from pipeline.models import Item, Status
from pipeline.safe_download import PinnedTransport, RoutingTransport
from pipeline.stages import enumerate_items as enum_stage
from pipeline.stages import research as research_stage
from pipeline.stages.extract import extract

PUBLIC = "93.184.216.34"
HOSTS = {
    "pub.test": [PUBLIC],
    "cdn.test": ["93.184.216.35"],
    "rebind.test": ["127.0.0.1"],
    "github.com": ["127.0.0.1"],
    "v6pub.test": ["2606:4700::1111"],
}
_REAL_GAI = socket.getaddrinfo
SECRET = "SECRETPATH7731"

PAGES = [
    "<html><head><title>A</title></head><body><article><h1>Alpha heading</h1>"
    + "".join(f"<p>Alpha paragraph number {i} talks about rockets, orbits and engines at length.</p>" for i in range(12))
    + "</article></body></html>",
    "<html><body><main><h2>Beta</h2><p>Beta page with code:</p><pre><code>def f(x):\n    return x * 2\n</code></pre>"
    + "".join(f"<p>Beta filler sentence {i} about gardens, soil, compost and seasonal planting.</p>" for i in range(10))
    + "</main></body></html>",
    "<html><body><div><h1>Gamma ünïcode</h1>"
    + "".join(f"<p>Gamma — “quoted” text {i} with naïve café résumé and 日本語 characters.</p>" for i in range(10))
    + "</div></body></html>",
]


# ------------------------------------------------------------------ server
class Server:
    """Threaded local HTTP server; routes are callables ``(handler) -> None``."""

    def __init__(self, family=socket.AF_INET, bind="127.0.0.1"):
        outer = self
        self.hits: list[tuple[str, str]] = []
        self.routes: dict[str, object] = {}
        self.lock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with outer.lock:
                    outer.hits.append((self.headers.get("Host", ""), self.path))
                route = outer.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class S(ThreadingHTTPServer):
            address_family = family
            daemon_threads = True

        self.httpd = S((bind, 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def private_hits(self):
        """Requests whose Host is not a fake-public name (i.e. a private hop)."""
        return [h for h in self.hits if not h[0].split(":")[0].endswith(".test")]

    def path_hits(self, prefix):
        return [h for h in self.hits if h[1].startswith(prefix)]


def reply(body: bytes, ctype="text/html; charset=utf-8", status=200, headers=None, length=True):
    def route(h):
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        if length:
            h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    return route


def redirect(to: str, status=302):
    def route(h):
        h.send_response(status)
        h.send_header("Location", to)
        h.send_header("Content-Length", "0")
        h.end_headers()

    return route


def endless(ctype, total, chunk=b"A" * 65536, headers=None):
    """Stream ``total`` bytes with NO content-length (connection close terminates)."""

    def route(h):
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        sent = 0
        while sent < total:
            h.wfile.write(chunk)
            sent += len(chunk)

    return route


def drip(ctype, seconds):
    def route(h):
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.end_headers()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            h.wfile.write(b"<p>x</p>")
            h.wfile.flush()
            time.sleep(0.2)

    return route


class MapBackend(httpcore.AsyncNetworkBackend):
    """Final dial only: vetted public IPs (and the :80 loopback literals) -> local server."""

    def __init__(self, port: int):
        self.port = port
        self.real = httpcore.AnyIOBackend()
        self.mapped = {PUBLIC, "93.184.216.35", "2606:4700::1111"}

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host in self.mapped or (host in ("127.0.0.1", "::1") and port == 80):
            return await self.real.connect_tcp("127.0.0.1", self.port, timeout=timeout)
        return await self.real.connect_tcp(host, port, timeout=timeout)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def prod_client() -> httpx.AsyncClient:
    """Verbatim production construction (src/pipeline/__main__.py:272)."""
    return httpx.AsyncClient(follow_redirects=True, transport=RoutingTransport())


def routed_client(port: int) -> httpx.AsyncClient:
    """Production construction, but the pinned pool's final dial is MapBackend."""
    return httpx.AsyncClient(
        follow_redirects=True, transport=RoutingTransport(pinned=PinnedTransport(MapBackend(port)))
    )


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
        infos = await asyncio.get_running_loop().run_in_executor(None, lambda: _REAL_GAI(host, port, type=socket.SOCK_STREAM))
        return [str(i[4][0]) for i in infos]

    monkeypatch.setattr(sd, "_resolve", resolve)


@pytest.fixture
def srv():
    s = Server()
    yield s
    s.close()


def png(w=1000, h=1000):
    img = Image.frombytes("L", (w, h), os.urandom(w * h))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


IMG = png()
AVATAR = png(200, 200)


def serve_all(srv):
    srv.routes["/img"] = reply(IMG, "image/png")
    srv.routes["/avatar"] = reply(AVATAR, "image/png")
    srv.routes["/page"] = reply(PAGES[0].encode())
    srv.routes[f"/{SECRET}"] = reply(PAGES[0].encode())
    srv.routes[f"/{SECRET}img"] = reply(IMG, "image/png")


async def attempt_all(http, url, tmp_path, img_url=None):
    return (
        await search.fetch_text(http, url),
        await search.download_image(http, img_url or url, tmp_path / "bg"),
        await search.download_avatar(http, img_url or url, tmp_path / "logo"),
    )


# ================================================================== AC1
def v6_server():
    try:
        return Server(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("IPv6 loopback unavailable")


async def test_ac1_positive_control_harness_reaches_server(srv, tmp_path):
    """Without this, 'zero hits' could just mean the harness never connects."""
    serve_all(srv)
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/page")
        assert await search.download_image(http, "http://pub.test/img", tmp_path / "bg")
        assert await search.download_avatar(http, "http://pub.test/avatar", tmp_path / "lg")
    assert len(srv.hits) == 3


@pytest.mark.parametrize("kind", ["literal", "localhost", "v6", "rebind_always", "rebind_flip", "userinfo", "dec_ip"])
async def test_ac1_private_targets_refused_zero_requests(kind, srv, tmp_path, monkeypatch):
    serve_all(srv)
    s6 = None
    p = srv.port
    if kind == "literal":
        url, img = f"http://127.0.0.1:{p}/page", f"http://127.0.0.1:{p}/img"
    elif kind == "localhost":
        url, img = f"http://localhost:{p}/page", f"http://localhost:{p}/img"
    elif kind == "v6":
        s6 = v6_server()
        serve_all(s6)
        url, img = f"http://[::1]:{s6.port}/page", f"http://[::1]:{s6.port}/img"
    elif kind == "rebind_always":
        url, img = "http://rebind.test/page", "http://rebind.test/img"
    elif kind == "rebind_flip":
        calls: dict[str, int] = {}

        async def flip(host, port):
            calls[host] = calls.get(host, 0) + 1
            return [PUBLIC] if calls[host] == 1 else ["127.0.0.1"]

        # check_url sees a public answer; the connect-time lookup answers 127.0.0.1
        monkeypatch.setattr(sd, "_resolve", flip)
        url, img = "http://flip.test/page", "http://flip.test/img"
    elif kind == "userinfo":
        url = img = f"http://pub.test@127.0.0.1:{p}/page"
    else:  # decimal-integer IP for 127.0.0.1
        url = img = "http://2130706433/page"
    try:
        async with prod_client() as http:
            if kind in ("rebind_always", "rebind_flip"):
                # the production client has no MapBackend; swap only the final dial
                await http.aclose()
                http = routed_client(p)
            results = await attempt_all(http, url, tmp_path, img)
            await http.aclose()
        assert results == (None, None, None)
        assert srv.hits == []
        if s6:
            assert s6.hits == []
        assert not (tmp_path / "bg").exists() or not list((tmp_path / "bg").iterdir())
        assert not (tmp_path / "logo").exists() or not list((tmp_path / "logo").iterdir())
    finally:
        if s6:
            s6.close()


@pytest.mark.parametrize("kind", ["literal", "localhost", "v6"])
async def test_ac1_connect_pin_alone_holds(kind, srv, tmp_path, monkeypatch):
    """Defeat the early checks (port gate + check_url) so ONLY the connect-time pin stands."""
    serve_all(srv)
    s6 = v6_server() if kind == "v6" else None
    if s6:
        serve_all(s6)
    port = (s6 or srv).port
    host = {"literal": "127.0.0.1", "localhost": "localhost", "v6": "[::1]"}[kind]

    async def allow(url):
        return None

    monkeypatch.setattr(sd, "check_url", allow)
    monkeypatch.setattr(sd, "ALLOWED_PORTS", {None, 80, 443, port})
    try:
        async with prod_client() as http:
            r = await attempt_all(http, f"http://{host}:{port}/page", tmp_path, f"http://{host}:{port}/img")
        assert r == (None, None, None)
        assert srv.hits == [] and (not s6 or s6.hits == [])
    finally:
        if s6:
            s6.close()


@pytest.mark.parametrize("target", [
    "http://127.0.0.1/{p}", "http://localhost/{p}", "http://[::1]/{p}", "http://rebind.test/{p}",
    "http://127.0.0.1:{port}/{p}", "http://0.0.0.0/{p}", "http://[::ffff:127.0.0.1]/{p}",
    "http://169.254.169.254/{p}", "http://10.0.0.5/{p}",
])
async def test_ac1_redirect_from_public_to_private(target, srv, tmp_path):
    serve_all(srv)
    srv.routes["/r"] = redirect(target.format(p=SECRET, port=srv.port))
    srv.routes["/ri"] = redirect(target.format(p=SECRET + "img", port=srv.port))
    async with routed_client(srv.port) as http:
        r = await asyncio.gather(
            search.fetch_text(http, "http://pub.test/r"),
            search.download_image(http, "http://pub.test/ri", tmp_path / "bg"),
            search.download_avatar(http, "http://pub.test/ri", tmp_path / "lg"),
        )
    assert r == [None, None, None]
    assert srv.path_hits(f"/{SECRET}") == []  # the private hop was never requested
    assert any(h[1] == "/r" for h in srv.hits)  # but the public hop was (control)


async def test_ac1_redirect_chain_public_public_private(srv):
    serve_all(srv)
    srv.routes["/a"] = redirect("http://cdn.test/b")
    srv.routes["/b"] = redirect(f"http://127.0.0.1/{SECRET}")
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/a") is None
    assert srv.path_hits(f"/{SECRET}") == []
    # control: a public->public redirect still works
    srv.routes["/c"] = redirect("http://cdn.test/page")
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/c")


async def test_ac1_redirect_into_rebind_at_connect(srv, monkeypatch):
    """Redirect to a name that passes check_url but resolves private at connect."""
    serve_all(srv)
    srv.routes["/r"] = redirect(f"http://flip.test/{SECRET}")
    n = {"flip.test": 0}

    async def resolve(host, port):
        if host == "flip.test":
            n[host] += 1
            return [PUBLIC] if n[host] == 1 else ["127.0.0.1"]
        return [PUBLIC]

    monkeypatch.setattr(sd, "_resolve", resolve)
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/r") is None
    assert srv.path_hits(f"/{SECRET}") == []


async def test_ac1_non_http_schemes_and_redirect_to_file(srv):
    serve_all(srv)
    srv.routes["/f"] = redirect("file:///etc/passwd")
    srv.routes["/g"] = redirect("gopher://127.0.0.1/x")
    async with routed_client(srv.port) as http:
        for u in ("file:///etc/passwd", "ftp://pub.test/x", "http://pub.test/f", "http://pub.test/g"):
            assert await search.fetch_text(http, u) is None


# ================================================================== AC2
async def test_ac2_unmarked_requests_still_reach_loopback(srv):
    srv.routes["/search"] = reply(b'{"results": []}', "application/json")
    srv.routes["/redir"] = redirect(f"http://127.0.0.1:{srv.port}/search")
    async with prod_client() as http:
        r = await http.get(f"http://127.0.0.1:{srv.port}/search")
        assert r.status_code == 200 and r.json() == {"results": []}
        r = await http.get(f"http://localhost:{srv.port}/search")
        assert r.status_code == 200
        r = await http.post(f"http://127.0.0.1:{srv.port}/search")  # POST: server only does GET -> 501 but connected
        assert r.status_code in (200, 501)
        r = await http.get(f"http://127.0.0.1:{srv.port}/redir")  # follow_redirects=True default
        assert r.status_code == 200
    s6 = v6_server()
    s6.routes["/x"] = reply(b"ok", "text/plain")
    try:
        async with prod_client() as http:
            assert (await http.get(f"http://[::1]:{s6.port}/x")).status_code == 200
    finally:
        s6.close()


async def test_ac2_marked_request_does_not_leak_into_unmarked(srv):
    """Order independence: a refused guarded fetch must not poison later loopback calls."""
    serve_all(srv)
    async with prod_client() as http:
        assert await search.fetch_text(http, f"http://127.0.0.1:{srv.port}/page") is None
        assert (await http.get(f"http://127.0.0.1:{srv.port}/page")).status_code == 200


# ================================================================== AC3
def zero_gzip(n_mb: int) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    out = [c.compress(b"\0" * (1 << 20)) for _ in range(n_mb)]
    out.append(c.flush())
    return b"".join(out)


@pytest.fixture(scope="module")
def bomb():
    return zero_gzip(400)  # 400 MB inflated


async def _bounded(coro_fn, limit_s, peak_mb=24):
    tracemalloc.start()
    t0 = time.monotonic()
    try:
        res = await coro_fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    dt = time.monotonic() - t0
    assert dt < limit_s, f"took {dt:.1f}s"
    assert peak < peak_mb * (1 << 20), f"peak {peak / 1e6:.1f} MB"
    return res, dt, peak


async def test_ac3_gzip_bomb_page(srv, bomb):
    srv.routes["/b"] = reply(bomb, headers={"Content-Encoding": "gzip"})
    async with routed_client(srv.port) as http:
        res, dt, peak = await _bounded(lambda: search.fetch_text(http, "http://pub.test/b"), 10)
    assert res is None
    print(f"gzip bomb: {len(bomb)} wire bytes, {dt:.2f}s, peak {peak / 1e6:.1f} MB")


async def test_ac3_stacked_encodings(srv, bomb):
    twice = gzip.compress(bomb)
    srv.routes["/s1"] = reply(twice, headers={"Content-Encoding": "gzip, gzip"})
    srv.routes["/s2"] = reply(twice, headers={"Content-Encoding": "gzip"})  # one declared, two layers real
    srv.routes["/s3"] = reply(bomb, headers={"Content-Encoding": "deflate, gzip"})
    srv.routes["/s4"] = reply(b"x" * 100, headers={"Content-Encoding": "br"})
    srv.routes["/s5"] = reply(bomb, headers={"Content-Encoding": "GZIP , identity"})
    srv.routes["/s6"] = reply(bomb, ctype="image/png", headers={"Content-Encoding": "gzip"})
    async with routed_client(srv.port) as http:
        for p in ("s1", "s2", "s3", "s4", "s5"):
            res, _, _ = await _bounded(lambda p=p: search.fetch_text(http, f"http://pub.test/{p}"), 10)
            assert res is None, p
        # image policies never inflate: any Content-Encoding is refused
        r, _, _ = await _bounded(lambda: search.download_image(http, "http://pub.test/s6", Path("/nonexistent-t227")), 10)
        assert r is None
        r, _, _ = await _bounded(lambda: search.download_avatar(http, "http://pub.test/s6", Path("/nonexistent-t227")), 10)
        assert r is None


async def test_ac3_oversize_html_streaming_no_content_length(srv):
    srv.routes["/big"] = endless("text/html", 300 << 20, b"<p>lorem ipsum dolor</p>" * 3000)
    async with routed_client(srv.port) as http:
        res, dt, peak = await _bounded(lambda: search.fetch_text(http, "http://pub.test/big"), 10)
    assert res is None
    print(f"oversize html stream: {dt:.2f}s peak {peak / 1e6:.1f} MB")


async def test_ac3_declared_oversize_and_non_text(srv):
    srv.routes["/cl"] = reply(b"<p>x</p>" * 1_000_000)  # content-length 8MB > cap
    srv.routes["/pdf"] = reply(b"%PDF-1.4 " + b"x" * 5000, "application/pdf")
    srv.routes["/bin"] = reply(b"\0" * 5000, "application/octet-stream")
    srv.routes["/json"] = reply(b"{}", "application/json")
    async with routed_client(srv.port) as http:
        for p in ("cl", "pdf", "bin", "json"):
            assert await search.fetch_text(http, f"http://pub.test/{p}") is None, p


async def test_ac3_oversize_image(srv, tmp_path):
    srv.routes["/bigimg"] = endless("image/png", 200 << 20)
    srv.routes["/bigimg2"] = reply(b"\x89PNG" + b"\0" * (9 << 20), "image/png")  # declared > 8MB
    srv.routes["/bigav"] = reply(b"\x89PNG" + b"\0" * (4 << 20), "image/png")  # > 3MB avatar cap
    async with routed_client(srv.port) as http:
        for p in ("bigimg", "bigimg2"):
            res, dt, peak = await _bounded(lambda p=p: search.download_image(http, f"http://pub.test/{p}", tmp_path), 10)
            assert res is None
        res, _, _ = await _bounded(lambda: search.download_avatar(http, "http://pub.test/bigav", tmp_path), 10)
        assert res is None
        res, _, _ = await _bounded(lambda: search.download_avatar(http, "http://pub.test/bigimg", tmp_path), 10)
        assert res is None
    assert not list(tmp_path.iterdir())


async def test_ac3_non_image_and_decompression_bomb_image(srv, tmp_path):
    srv.routes["/html"] = reply(PAGES[0].encode())
    big_px = Image.new("L", (7000, 7000))  # 49 MP > MAX_IMAGE_PIXELS, compresses tiny
    buf = BytesIO()
    big_px.save(buf, "PNG")
    srv.routes["/px"] = reply(buf.getvalue() + b"\0" * 20000, "image/png")
    srv.routes["/junk"] = reply(os.urandom(50000), "image/png")
    async with routed_client(srv.port) as http:
        for p in ("html", "px", "junk"):
            assert await search.download_image(http, f"http://pub.test/{p}", tmp_path) is None, p
    assert not list(tmp_path.iterdir())


async def test_ac3_slow_drip_respects_deadline(srv, monkeypatch):
    srv.routes["/drip"] = drip("text/html", 30)
    monkeypatch.setattr(search, "PAGE_POLICY", replace(sd.PAGE_POLICY, deadline_s=1.5, timeout_s=5.0))
    async with routed_client(srv.port) as http:
        t0 = time.monotonic()
        assert await search.fetch_text(http, "http://pub.test/drip") is None
        dt = time.monotonic() - t0
    assert dt < 3.0, dt
    print(f"slow drip: gave up after {dt:.2f}s (deadline 1.5s)")


async def test_ac3_redirect_loop_bounded(srv):
    srv.routes["/loop"] = redirect("http://pub.test/loop")
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/loop") is None
    assert len(srv.hits) <= sd.MAX_REDIRECTS + 1


async def test_ac3_truncated_gzip_and_cap_boundary(srv):
    good = gzip.compress(PAGES[0].encode())
    srv.routes["/trunc"] = reply(good[: len(good) // 2], headers={"Content-Encoding": "gzip"})
    # exactly-at-cap identity page passes the size gate; one byte over does not
    pad = lambda n: (PAGES[0] + " " * n).encode()[: sd.MAX_BODY_BYTES]  # noqa: E731
    srv.routes["/atcap"] = reply(pad(sd.MAX_BODY_BYTES))
    srv.routes["/overcap"] = reply(b"<p>" + b"a" * sd.MAX_BODY_BYTES)
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/trunc") is None
        assert await search.fetch_text(http, "http://pub.test/overcap") is None
        out = await sd.fetch_public(http, "http://pub.test/atcap", sd.PAGE_POLICY)
        assert not isinstance(out, sd.Refusal) and len(out.body) == sd.MAX_BODY_BYTES


# ================================================================== AC4
@pytest.mark.parametrize("i", range(len(PAGES)))
async def test_ac4_pages_match_trafilatura_and_gzip_equals_plain(i, srv):
    import trafilatura

    html = PAGES[i]
    expected = trafilatura.extract(html, include_formatting=True)
    assert expected, "fixture must be extractable"
    srv.routes["/plain"] = reply(html.encode())
    srv.routes["/gz"] = reply(gzip.compress(html.encode()), headers={"Content-Encoding": "gzip"})
    srv.routes["/z"] = reply(zlib.compress(html.encode()), headers={"Content-Encoding": "deflate"})
    srv.routes["/nocharset"] = reply(html.encode(), ctype="text/html")
    srv.routes["/latin"] = reply(html.encode("latin-1", "replace"), ctype="text/html; charset=iso-8859-1")
    async with routed_client(srv.port) as http:
        plain = await search.fetch_text(http, "http://pub.test/plain")
        gz = await search.fetch_text(http, "http://pub.test/gz")
        z = await search.fetch_text(http, "http://pub.test/z")
        nocs = await search.fetch_text(http, "http://pub.test/nocharset")
        latin = await search.fetch_text(http, "http://pub.test/latin")
    assert plain == expected[: search.MAX_TEXT_CHARS or None]
    assert gz == plain and z == plain and nocs == plain
    assert latin  # declared-charset page decodes (lossy chars replaced) rather than failing


async def test_ac4_missing_content_type_and_xhtml_accepted(srv):
    srv.routes["/nt"] = reply(PAGES[0].encode(), ctype="", headers={})
    srv.routes["/xh"] = reply(PAGES[0].encode(), ctype="application/xhtml+xml")
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/xh")
        # empty Content-Type header value => treated as missing type, allowed for pages
        assert await search.fetch_text(http, "http://pub.test/nt")


async def test_ac4_images_saved_with_same_naming(srv, tmp_path):
    srv.routes["/i.png"] = reply(IMG, "image/png")
    srv.routes["/o.bin"] = reply(IMG, "application/octet-stream")
    srv.routes["/a.png"] = reply(AVATAR, "image/png")
    srv.routes["/small.png"] = reply(png(300, 300), "image/png")
    srv.routes["/jpg"] = reply(_jpeg(), "image/jpeg")
    async with routed_client(srv.port) as http:
        for path in ("i.png", "o.bin"):
            url = f"http://pub.test/{path}"
            p = await search.download_image(http, url, tmp_path / "bg")
            assert p and p.name == f"bg_{abs(hash(url)) % 10**10}.png"
            assert p.read_bytes() == IMG
            with Image.open(p) as im:
                im.verify()
        url = "http://pub.test/jpg"
        p = await search.download_image(http, url, tmp_path / "bg")
        assert p and p.suffix == ".jpg" and p.name == f"bg_{abs(hash(url)) % 10**10}.jpg"
        url = "http://pub.test/a.png"
        p = await search.download_avatar(http, url, tmp_path / "logo")
        assert p and p.name == f"logo_{abs(hash(url)) % 10**10}.png" and p.read_bytes() == AVATAR
        # unchanged floors: 300px is not a background (900 floor) but is a valid avatar
        assert await search.download_image(http, "http://pub.test/small.png", tmp_path / "bg") is None
        p = await search.download_avatar(http, "http://pub.test/small.png", tmp_path / "logo")
        assert p and p.exists()


def _jpeg():
    buf = BytesIO()
    Image.frombytes("RGB", (1000, 1000), os.urandom(3_000_000)).save(buf, "JPEG", quality=30)
    return buf.getvalue()


async def test_ac4_http_error_statuses_are_none(srv):
    srv.routes["/500"] = reply(b"x", status=500)
    srv.routes["/404"] = reply(b"x", status=404)
    async with routed_client(srv.port) as http:
        assert await search.fetch_text(http, "http://pub.test/500") is None
        assert await search.fetch_text(http, "http://pub.test/404") is None
        assert await search.fetch_text(http, "http://pub.test/nonexistent-route") is None
        assert await search.fetch_text(http, "http://unresolvable.invalid/") is None


# ================================================================== AC5
def item(text=""):
    return Item(id=7, source="dm", status=Status.TRIAGED, raw_text=text)


async def test_ac5_extract_user_links_refused(srv):
    serve_all(srv)
    async with prod_client() as http:
        out = await extract(
            item(f"look http://127.0.0.1:{srv.port}/page and http://localhost:{srv.port}/page and http://rebind.test/page"),
            llm=None, http=http,
        )
    texts = out["extracted"]["url_texts"]
    assert len(texts) == 3
    assert all(t.get("error") == "unreachable or no extractable text" and "text" not in t for t in texts)
    assert out["extracted"]["image_descriptions"] == []
    assert srv.hits == []


async def test_ac5_extract_mixed_public_and_private(srv):
    serve_all(srv)
    async with routed_client(srv.port) as http:
        out = await extract(item(f"http://pub.test/page http://127.0.0.1/{SECRET}"), llm=None, http=http)
    a, b = out["extracted"]["url_texts"]
    assert "text" in a and "error" in b
    assert srv.path_hits(f"/{SECRET}") == []


async def test_ac5_research_background_skips_refused_candidates(srv, tmp_path, monkeypatch):
    serve_all(srv)
    cands = [
        {"url": f"http://127.0.0.1:{srv.port}/img", "title": "subject a"},
        {"url": "http://localhost/img", "title": "subject b"},
        {"url": "http://rebind.test/img", "title": "subject c"},
    ]

    async def fake_image_search(http, base, query):
        return cands

    monkeypatch.setattr(research_stage, "image_search", fake_image_search)
    settings = SimpleNamespace(searxng_url="http://x", media_dir=str(tmp_path))
    async with routed_client(srv.port) as http:
        got = await research_stage._find_background("q", "subject", item(), http, settings)
        assert got is None  # no background: the stage shrugs, no exception
        cands.append({"url": "http://pub.test/img", "title": "subject ok"})  # control: public one is used
        got = await research_stage._find_background("q", "subject", item(), http, settings)
    assert got and got["host"] == "pub.test"
    assert srv.private_hits() == []


async def test_ac5_research_one_falls_back_to_snippet(srv, monkeypatch):
    serve_all(srv)
    results = [
        {"url": f"http://127.0.0.1:{srv.port}/page", "title": "t1", "content": "SNIPPET-ONE about the subject"},
        {"url": "http://rebind.test/page", "title": "t2", "content": ""},  # no snippet -> skipped
    ]

    async def fake_searx(*a, **k):
        return results

    seen: list[tuple[str, str]] = []

    async def gate(llm, subject, url, body, settings, request=""):
        seen.append((url, body))
        return research_stage._GateVerdict.REFUSED

    monkeypatch.setattr(research_stage, "searx", fake_searx)
    monkeypatch.setattr(research_stage, "dedupe_by_domain", lambda r, n: r)
    monkeypatch.setattr(research_stage, "_is_relevant", gate)
    settings = SimpleNamespace(searxng_url="http://x", results_per_query=5, docs_per_query=5)
    async with prod_client() as http:
        out = await research_stage._research_one(0, "q", "subj", item(), llm=None, http=http, settings=settings)
    assert out is None  # gate refused the only (snippet) source -> same as an unreachable page
    assert seen == [(results[0]["url"], "SNIPPET-ONE about the subject")]  # snippet, not page; second skipped
    assert srv.hits == []


async def test_ac5_enumerate_things_and_logos(srv, tmp_path):
    serve_all(srv)
    prompts: list[str] = []

    class LLM:
        async def good(self, system, user, schema=None):
            prompts.append(user)
            return {"things": []}

    cands = [
        {"url": f"http://127.0.0.1:{srv.port}/page", "title": "x", "content": "ENUM-SNIPPET"},
        {"url": "http://localhost/page", "title": "y", "content": ""},
    ]
    plan = {"subject": "subj", "count": 3}
    async with prod_client() as http:
        notes = await enum_stage._extract_things(cands, plan, item(), LLM(), http)
        assert notes == [] and len(prompts) == 1 and "ENUM-SNIPPET" in prompts[0]
        # no snippets at all => nothing to cite => [] without calling the model
        prompts.clear()
        assert await enum_stage._extract_things([cands[1]], plan, item(), LLM(), http) == []
        assert prompts == []
        # logos: github.com is made to resolve privately; no logo, no exception
        notes = [{"owner": "someone"}, {"owner": ""}]
        settings = SimpleNamespace(media_dir=str(tmp_path))
        await enum_stage._attach_logos(notes, item(), http, settings)
    assert all("logo" not in n for n in notes)
    assert not (tmp_path / "logos").exists() or not list((tmp_path / "logos").rglob("*.*"))
    assert srv.hits == []


# ================================================================== AC7
async def test_ac7_refusals_log_no_url_or_exception_text(srv, tmp_path, caplog):
    serve_all(srv)
    srv.routes["/r"] = redirect(f"http://127.0.0.1/{SECRET}")
    secret_urls = [
        f"http://127.0.0.1:{srv.port}/{SECRET}",
        f"http://localhost:{srv.port}/{SECRET}",
        f"http://rebind.test/{SECRET}",
        "http://pub.test/r",
        f"http://[::1]:{srv.port}/{SECRET}",
        "http://user:pw@pub.test/" + SECRET,
        f"http://pub.test/{SECRET}-missing",
    ]
    caplog.set_level(logging.DEBUG)
    async with routed_client(srv.port) as http:
        for u in secret_urls:
            await attempt_all(http, u, tmp_path)
    ours = [r for r in caplog.records if r.name.startswith("pipeline")]
    assert ours, "expected some pipeline log records (refusal reasons)"
    needles = [SECRET, "127.0.0.1", "localhost", "::1", "rebind.test", "pw", "user:", str(srv.port)]
    for r in ours:
        text = r.getMessage() + (r.exc_text or "") + (str(r.exc_info[1]) if r.exc_info else "")
        for n in needles:
            assert n not in text, f"{n!r} leaked in {r.name}: {text}"
        assert r.exc_info is None
    print("pipeline records:", sorted({r.getMessage() for r in ours}))
    # third-party DEBUG chatter (httpcore) is reported, not gated: production sets WARNING+ for it
    third = [r for r in caplog.records if not r.name.startswith("pipeline") and any(n in r.getMessage() for n in needles)]
    print("third-party debug records mentioning hosts (informational):", len(third), {r.name for r in third})


async def test_ac7_info_level_all_loggers_clean(srv, tmp_path, caplog):
    serve_all(srv)
    caplog.set_level(logging.INFO)
    async with routed_client(srv.port) as http:
        await attempt_all(http, f"http://127.0.0.1:{srv.port}/{SECRET}", tmp_path)
        await attempt_all(http, f"http://rebind.test/{SECRET}", tmp_path)
    for r in caplog.records:
        assert SECRET not in r.getMessage() and "127.0.0.1" not in r.getMessage(), r.getMessage()


# ================================================================== AC6 (reported, not gated)
@pytest.mark.skipif(not os.environ.get("T227_LIVE"), reason="live network: set T227_LIVE=1")
async def test_ac6_live_smoke(tmp_path):
    pages = [
        "https://example.com/",
        "https://www.python.org/about/",
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
    ]
    images = [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ec/Mona_Lisa%2C_by_Leonardo_da_Vinci%2C_from_C2RMF_retouched.jpg/960px-Mona_Lisa%2C_by_Leonardo_da_Vinci%2C_from_C2RMF_retouched.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/Fronalpstock_big.jpg/1280px-Fronalpstock_big.jpg",
    ]
    report = []
    async with prod_client() as http:
        for u in pages:
            t = await search.fetch_text(http, u)
            report.append((u, None if t is None else (len(t), t[:80].replace("\n", " "))))
            await asyncio.sleep(1)
        for u in images:
            p = await search.download_image(http, u, tmp_path)
            info = None
            if p:
                with Image.open(p) as im:
                    info = (p.name, im.size, p.stat().st_size)
            report.append((u[-40:], info))
            await asyncio.sleep(1)
    for line in report:
        print("LIVE", line)
    assert all(r[1] is not None for r in report), report
