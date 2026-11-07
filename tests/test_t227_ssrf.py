"""T227: the research path's fetches (pages, background images, avatars) are SSRF-guarded.

No network and no real DNS: httpx.MockTransport for the HTTP, a table for the
resolver, a recording backend for the pinned-connection tests.
"""

from __future__ import annotations

import gzip
import io
import logging
import random
import struct
import tracemalloc
import zlib

import httpcore
import httpx
import pytest
from PIL import Image

from pipeline import safe_download as sd
from pipeline.safe_download import Refusal
from pipeline.search import download_avatar, download_image, fetch_text

PUBLIC = "93.184.216.34"
PAGE_HTML = (
    "<html><body><article><p>The layout looks like this, and here is a longer sentence so the "
    "extractor treats this as a real article body worth keeping.</p>"
    "<pre><code>---\ntype: concept\n---</code></pre>"
    "<p>Each concept file carries YAML frontmatter followed by Markdown, which is what "
    "makes the format readable without any tooling.</p></article></body></html>"
)


@pytest.fixture
def dns(monkeypatch):
    table: dict[str, list[str]] = {}

    async def resolve(host, port):
        import ipaddress

        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        if host in table:
            return list(table[host])
        return [PUBLIC]

    monkeypatch.setattr(sd, "_resolve", resolve)
    return table


class Site:
    """A MockTransport-backed client that records every request that reaches 'the network'."""

    def __init__(self, handler):
        self.seen: list[httpx.Request] = []

        def wrapped(request):
            self.seen.append(request)
            return handler(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=True)

    @property
    def urls(self):
        return [str(r.url) for r in self.seen]


def chunked(blob: bytes, size: int = 65536):
    """MockTransport pre-reads bytes bodies (the stream is then consumed); an async
    generator stays a real stream, which is what the guarded fetch reads."""

    async def gen():
        for i in range(0, len(blob), size):
            yield blob[i:i + size]

    return gen()


def html_response(body=PAGE_HTML, **kw):
    headers = {"content-type": "text/html; charset=utf-8", **kw.pop("headers", {})}
    return httpx.Response(200, content=chunked(body if isinstance(body, bytes) else body.encode()), headers=headers, **kw)


def noise_png(w=1000, h=700, seed=1):
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), random.Random(seed).randbytes(w * h * 3)).save(buf, "PNG")
    return buf.getvalue()


def png_claiming(width, height, base=None):
    """A real PNG whose IHDR claims a size it does not have (decompression-bomb header)."""
    data = bytearray(base or noise_png())
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def image_response(blob, **kw):
    return httpx.Response(200, content=chunked(blob), headers={"content-type": "image/png", **kw.pop("headers", {})})


# ------------------------------------------------------------- fetch_text: unchanged path


async def test_public_page_text_is_exactly_what_trafilatura_extracts(dns):
    import trafilatura

    site = Site(lambda r: html_response())
    text = await fetch_text(site.client, "https://news.example/a")
    assert text == trafilatura.extract(PAGE_HTML, include_formatting=True)
    assert "type: concept" in text  # fenced code survives, as before


async def test_page_decoded_with_declared_charset(dns):
    body = "<html><body><article><p>" + "caf\xe9 au lait is a drink that people enjoy at breakfast time every day. " * 5 + "</p></article></body></html>"
    site = Site(lambda r: html_response(body.encode("latin-1"), headers={"content-type": "text/html; charset=ISO-8859-1"}))
    text = await fetch_text(site.client, "https://news.example/a")
    assert "caf\xe9" in text


async def test_single_gzip_layer_is_inflated_like_before(dns):
    site = Site(lambda r: html_response(gzip.compress(PAGE_HTML.encode()), headers={"content-encoding": "gzip"}))
    plain = Site(lambda r: html_response())
    assert await fetch_text(site.client, "https://news.example/a") == await fetch_text(plain.client, "https://news.example/a")


async def test_page_without_content_type_is_still_read(dns):
    site = Site(lambda r: httpx.Response(200, content=chunked(PAGE_HTML.encode())))
    assert "type: concept" in (await fetch_text(site.client, "https://news.example/a"))


async def test_non_200_and_unreachable_are_none(dns):
    assert await fetch_text(Site(lambda r: httpx.Response(404)).client, "https://news.example/a") is None

    def boom(request):
        raise httpx.ConnectError("down")

    assert await fetch_text(Site(boom).client, "https://news.example/a") is None


# ------------------------------------------------------------- fetch_text: refusals


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://localhost/admin",
    "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://0.0.0.0/",
    "http://2130706433/",  # decimal loopback
    "http://[::ffff:127.0.0.1]/",
    "file:///etc/passwd",
    "ftp://news.example/a",
    "http://user:pw@news.example/a",
    "http://news.example:8080/a",
    "http://news.example:22/a",
])
async def test_forbidden_targets_never_leave_the_process(dns, url):
    dns["localhost"] = ["127.0.0.1"]
    dns["2130706433"] = ["127.0.0.1"]  # what the real getaddrinfo says for the decimal form
    site = Site(lambda r: html_response())
    assert await fetch_text(site.client, url) is None
    assert site.seen == []


async def test_name_resolving_to_private_is_refused(dns):
    dns["internal.example"] = ["10.1.2.3"]
    dns["mixed.example"] = [PUBLIC, "169.254.169.254"]
    site = Site(lambda r: html_response())
    assert await fetch_text(site.client, "https://internal.example/") is None
    assert await fetch_text(site.client, "https://mixed.example/") is None
    assert site.seen == []


async def test_public_to_private_redirect_is_not_followed(dns):
    dns["internal.example"] = ["10.1.2.3"]

    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    site = Site(handler)
    assert await fetch_text(site.client, "http://news.example/a") is None
    assert site.urls == ["http://news.example/a"]  # the private hop was never requested


async def test_redirect_to_metadata_literal_is_not_followed(dns):
    site = Site(lambda r: httpx.Response(301, headers={"location": "http://169.254.169.254/latest/meta-data/"}))
    assert await fetch_text(site.client, "https://news.example/a") is None
    assert len(site.seen) == 1


async def test_https_to_http_downgrade_refused(dns):
    site = Site(lambda r: httpx.Response(302, headers={"location": "http://news.example/plain"}))
    assert await fetch_text(site.client, "https://news.example/a") is None
    assert len(site.seen) == 1


async def test_public_redirect_chain_is_followed_and_capped(dns):
    def handler(request):
        if request.url.path == "/b":
            return html_response()
        return httpx.Response(302, headers={"location": "/b"})

    assert "type: concept" in (await fetch_text(Site(handler).client, "https://news.example/a"))

    loop = Site(lambda r: httpx.Response(302, headers={"location": "/again"}))
    assert await fetch_text(loop.client, "https://news.example/a") is None
    assert len(loop.seen) == sd.MAX_REDIRECTS + 1


async def test_non_text_content_type_refused_before_the_body_is_read(dns):
    pulled = []

    async def stream():
        for _ in range(1000):
            pulled.append(1)
            yield b"x" * 4096

    for kind in ("application/pdf", "image/png", "application/octet-stream", "video/mp4"):
        pulled.clear()
        site = Site(lambda r, kind=kind: httpx.Response(200, content=stream(), headers={"content-type": kind}))
        assert await fetch_text(site.client, "https://news.example/a") is None, kind
        assert pulled == [], kind


async def test_oversize_html_is_cut_off_while_streaming(dns):
    pulled = []

    async def chunks():
        for _ in range(10_000):  # 40 MB offered
            pulled.append(1)
            yield b"<p>x</p>" * 512

    site = Site(lambda r: httpx.Response(200, content=chunks(), headers={"content-type": "text/html"}))
    assert await fetch_text(site.client, "https://news.example/a") is None
    assert len(pulled) * 4096 < sd.MAX_BODY_BYTES * 3  # stopped near the cap, never drained


async def test_declared_oversize_refused_without_reading(dns):
    pulled = []

    async def body():
        pulled.append(1)
        yield b"x"

    site = Site(lambda r: httpx.Response(200, content=body(), headers={"content-type": "text/html", "content-length": str(sd.MAX_BODY_BYTES + 1)}))
    assert await sd.fetch_public(site.client, "https://news.example/a", sd.PAGE_POLICY) is Refusal.TOO_BIG
    assert pulled == []


async def test_body_at_the_cap_passes_and_one_over_fails(dns):
    exactly = b"<p>" + b"a" * (sd.MAX_BODY_BYTES - 3)
    assert len(exactly) == sd.MAX_BODY_BYTES
    got = await sd.fetch_public(Site(lambda r: html_response(exactly)).client, "https://news.example/a", sd.PAGE_POLICY)
    assert isinstance(got, sd.Fetched)
    over = await sd.fetch_public(Site(lambda r: html_response(exactly + b"a")).client, "https://news.example/a", sd.PAGE_POLICY)
    assert over is Refusal.TOO_BIG


async def test_gzip_bomb_is_cut_off_at_the_cap_without_buffering_it(dns):
    bomb = gzip.compress(b"\0" * 400_000_000, compresslevel=9)  # ~400 KB on the wire, 400 MB inflated
    assert len(bomb) < sd.MAX_BODY_BYTES
    site = Site(lambda r: html_response(bomb, headers={"content-encoding": "gzip"}))
    tracemalloc.start()
    try:
        got = await sd.fetch_public(site.client, "https://news.example/a", sd.PAGE_POLICY)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got is Refusal.TOO_BIG
    assert peak < 6 * sd.MAX_BODY_BYTES, peak


async def test_endless_bytes_after_the_gzip_stream_ends_are_still_capped(dns):
    """zlib parks bytes after end-of-stream in ``unused_data`` without bound; the raw cap stops that."""
    pulled = []

    async def body():
        yield gzip.compress(PAGE_HTML.encode())
        for _ in range(10_000):
            pulled.append(1)
            yield b"\0" * 65536

    site = Site(lambda r: httpx.Response(200, content=body(), headers={"content-type": "text/html", "content-encoding": "gzip"}))
    assert await sd.fetch_public(site.client, "https://news.example/a", sd.PAGE_POLICY) is Refusal.TOO_BIG
    assert len(pulled) * 65536 < sd.MAX_BODY_BYTES * 3


def test_limits_keep_their_pre_guard_values():
    assert sd.MAX_BODY_BYTES == 2_000_000  # the page cap fetch_text always had
    assert sd.MAX_IMAGE_BYTES == 8_000_000
    assert sd.MAX_AVATAR_BYTES == 3_000_000


async def test_stacked_encodings_and_unknown_codecs_refused(dns):
    for enc in ("gzip, gzip", "br", "zstd", "gzip, br"):
        site = Site(lambda r, enc=enc: html_response(gzip.compress(PAGE_HTML.encode()), headers={"content-encoding": enc}))
        assert await sd.fetch_public(site.client, "https://news.example/a", sd.PAGE_POLICY) is Refusal.ENCODED, enc


async def test_truncated_gzip_is_not_returned(dns):
    cut = gzip.compress(PAGE_HTML.encode() * 50)[:-20]
    site = Site(lambda r: html_response(cut, headers={"content-encoding": "gzip"}))
    assert await sd.fetch_public(site.client, "https://news.example/a", sd.PAGE_POLICY) is Refusal.UNREACHABLE


async def test_request_asks_for_the_pinned_transport_and_limited_encoding(dns):
    site = Site(lambda r: html_response())
    await fetch_text(site.client, "https://news.example/a")
    (request,) = site.seen
    assert request.extensions[sd.PUBLIC_ONLY_EXTENSION] is True
    assert request.headers["accept-encoding"] == "gzip"
    site = Site(lambda r: image_response(noise_png()))
    await sd.download_public_image(site.client, "https://img.example/a.png")
    assert site.seen[0].headers["accept-encoding"] == "identity"
    assert site.seen[0].extensions[sd.PUBLIC_ONLY_EXTENSION] is True


async def test_logs_carry_codes_not_urls_or_exception_text(dns, caplog):
    secret = "tok3n-s3cret"
    dns["internal.example"] = ["10.0.0.9"]

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    caplog.set_level(logging.DEBUG)
    await fetch_text(Site(boom).client, f"https://news.example/a?k={secret}")
    await fetch_text(Site(boom).client, f"https://internal.example/a?k={secret}")
    await download_image(Site(boom).client, f"https://news.example/i.png?k={secret}", __import__("pathlib").Path("/nonexistent"))
    await download_avatar(Site(boom).client, f"https://news.example/i.png?k={secret}", __import__("pathlib").Path("/nonexistent"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in text
    assert "news.example" not in text and "internal.example" not in text
    assert "unreachable" in text or "ConnectError" in text


# ------------------------------------------------------------- images


async def test_background_public_image_saved_as_before(dns, tmp_path):
    blob = noise_png(1000, 700)
    site = Site(lambda r: image_response(blob))
    path = await download_image(site.client, "https://img.example/a.png", tmp_path)
    assert path is not None and path.read_bytes() == blob and path.suffix == ".png"


async def test_background_octet_stream_still_accepted_because_it_is_decoded(dns, tmp_path):
    site = Site(lambda r: httpx.Response(200, content=chunked(noise_png()), headers={"content-type": "application/octet-stream"}))
    assert await download_image(site.client, "https://img.example/a", tmp_path) is not None


async def test_background_small_or_low_res_or_non_image_is_none(dns, tmp_path):
    small = noise_png(300, 300)  # long edge < 900
    assert await download_image(Site(lambda r: image_response(small)).client, "https://img.example/a", tmp_path) is None
    assert await download_image(Site(lambda r: image_response(b"x" * 100)).client, "https://img.example/a", tmp_path) is None
    junk = Site(lambda r: image_response(b"not an image" * 5000))
    assert await download_image(junk.client, "https://img.example/a", tmp_path) is None
    html = Site(lambda r: html_response())
    assert await download_image(html.client, "https://img.example/a", tmp_path) is None


async def test_background_private_targets_refused(dns, tmp_path):
    dns["internal.example"] = ["10.0.0.9"]
    site = Site(lambda r: image_response(noise_png()))
    for url in ("http://127.0.0.1/a.png", "http://169.254.169.254/a.png", "https://internal.example/a.png"):
        assert await download_image(site.client, url, tmp_path) is None
        assert await download_avatar(site.client, url, tmp_path) is None
    assert site.seen == []
    assert not list(tmp_path.iterdir())


async def test_background_redirect_to_private_not_followed(dns, tmp_path):
    site = Site(lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1:80/a.png"}))
    assert await download_image(site.client, "https://img.example/a.png", tmp_path) is None
    assert await download_avatar(site.client, "https://img.example/a.png", tmp_path) is None
    assert all("127.0.0.1" not in u for u in site.urls)


async def test_background_oversize_cut_off_while_streaming(dns, tmp_path):
    pulled = []

    async def chunks():
        for _ in range(5000):
            pulled.append(1)
            yield b"\0" * 65536

    site = Site(lambda r: httpx.Response(200, content=chunks(), headers={"content-type": "image/png"}))
    assert await download_image(site.client, "https://img.example/a.png", tmp_path) is None
    assert len(pulled) * 65536 < sd.MAX_IMAGE_BYTES * 3


async def test_encoded_image_refused_not_inflated(dns, tmp_path):
    bomb = gzip.compress(b"\0" * 50_000_000)
    site = Site(lambda r: image_response(bomb, headers={"content-encoding": "gzip"}))
    assert await download_image(site.client, "https://img.example/a.png", tmp_path) is None
    assert await download_avatar(site.client, "https://img.example/a.png", tmp_path) is None


async def test_pixel_bomb_header_refused(dns, tmp_path):
    bomb = png_claiming(9000, 9000)
    assert 9000 * 9000 > sd.MAX_IMAGE_PIXELS
    assert len(bomb) > sd.MIN_IMAGE_BYTES
    site = Site(lambda r: image_response(bomb))
    assert await download_image(site.client, "https://img.example/a.png", tmp_path) is None
    assert await download_avatar(site.client, "https://img.example/a.png", tmp_path) is None
    ok = png_claiming(5000, 5000)  # 25 MP: under the cap, over the edge floor
    assert await download_image(Site(lambda r: image_response(ok)).client, "https://img.example/a.png", tmp_path) is not None


async def test_avatar_and_background_have_their_own_byte_caps(dns, tmp_path):
    big = noise_png(1200, 900, seed=3)  # ~3.2 MB: a fine background, too big for an avatar
    assert sd.MAX_AVATAR_BYTES < len(big) < sd.MAX_IMAGE_BYTES
    assert await download_avatar(Site(lambda r: image_response(big)).client, "https://img.example/a.png", tmp_path) is None
    assert await download_image(Site(lambda r: image_response(big)).client, "https://img.example/a.png", tmp_path) is not None


async def test_background_under_the_byte_floor_is_none(dns, tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (1000, 1000), "blue").save(buf, "PNG")  # wide enough, but a few KB
    assert len(buf.getvalue()) < 15_000
    assert await download_image(Site(lambda r: image_response(buf.getvalue())).client, "https://img.example/a.png", tmp_path) is None


async def test_avatar_unchanged_path_and_floors(dns, tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), "red").save(buf, "PNG")
    path = await download_avatar(Site(lambda r: image_response(buf.getvalue())).client, "https://github.com/x.png?size=200", tmp_path)
    assert path is not None and path.name.startswith("logo_") and path.suffix == ".png"
    tiny = io.BytesIO()
    Image.new("RGB", (39, 200), "red").save(tiny, "PNG")
    assert await download_avatar(Site(lambda r: image_response(tiny.getvalue())).client, "https://github.com/x.png", tmp_path) is None
    big = Site(lambda r: image_response(b"\0" * (sd.MAX_AVATAR_BYTES + 1)))
    assert await download_avatar(big.client, "https://github.com/x.png", tmp_path) is None
    assert await download_avatar(Site(lambda r: httpx.Response(404)).client, "https://github.com/x.png", tmp_path) is None


# ------------------------------------------------------------- pinned connection


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend):
        self.backend = backend
        self.written = b""
        self.buffer = [
            b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\ncontent-length: 2\r\n\r\nhi"
        ]

    async def read(self, max_bytes, timeout=None):
        return self.buffer.pop(0) if self.buffer else b""

    async def write(self, buffer, timeout=None):
        self.written += buffer

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.backend.tls.append((server_hostname, ssl_context))
        return self

    def get_extra_info(self, info):
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, fail_on=()):
        self.dialed: list[tuple[str, int]] = []
        self.tls: list = []
        self.streams: list[RecordingStream] = []
        self.fail_on = set(fail_on)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialed.append((host, port))
        if host in self.fail_on:
            raise httpcore.ConnectError("refused")
        stream = RecordingStream(self)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("never")

    async def sleep(self, seconds):
        pass


def pinned_client(backend):
    return httpx.AsyncClient(transport=sd.PinnedTransport(backend))


async def test_pinned_transport_dials_the_vetted_ip_keeping_host_and_sni(dns):
    dns["news.example"] = [PUBLIC]
    backend = RecordingBackend()
    async with pinned_client(backend) as client:
        response = await client.get("https://news.example/a", extensions={sd.PUBLIC_ONLY_EXTENSION: True})
    assert response.text == "hi"
    assert backend.dialed == [(PUBLIC, 443)]  # the literal IP, never the name
    assert backend.tls[0][0] == "news.example"  # SNI + certificate name stay the hostname
    assert b"host: news.example" in backend.streams[0].written.lower()
    ctx = backend.tls[0][1]
    import ssl

    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


async def test_pinned_transport_plain_http_dials_ip_with_host_header(dns):
    backend = RecordingBackend()
    async with pinned_client(backend) as client:
        await client.get("http://news.example/a")
    assert backend.dialed == [(PUBLIC, 80)]
    assert b"host: news.example" in backend.streams[0].written.lower()
    assert backend.tls == []


async def test_dns_rebinding_between_check_and_connect_is_closed(monkeypatch):
    """check_url sees a public answer; the connect-time lookup answers private. The
    connection must never be dialed -- and the Refusal says why."""
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])

    async def resolve(host, port):
        return next(answers)

    monkeypatch.setattr(sd, "_resolve", resolve)
    backend = RecordingBackend()
    async with pinned_client(backend) as client:
        got = await sd.fetch_public(client, "https://rebind.example/a", sd.PAGE_POLICY)
    assert got is Refusal.PRIVATE_HOST
    assert backend.dialed == []


async def test_rebinding_answer_cannot_redirect_the_dial_after_vetting(monkeypatch):
    """The resolver is consulted once per connect; the dial target is a vetted answer,
    never a second lookup (a flipping resolver would otherwise win)."""
    calls = []

    async def resolve(host, port):
        calls.append(host)
        return [PUBLIC] if len(calls) % 2 else ["10.0.0.1"]

    monkeypatch.setattr(sd, "_resolve", resolve)
    backend = RecordingBackend()
    stream = await sd.VettingBackend(backend).connect_tcp("flip.example", 443)
    assert stream is not None
    assert backend.dialed == [(PUBLIC, 443)]
    assert calls == ["flip.example"]


async def test_vetting_backend_refuses_any_private_address_in_the_answer(monkeypatch):
    for answer in (["10.0.0.1"], [PUBLIC, "127.0.0.1"], ["169.254.169.254"], ["::1"], ["::ffff:10.0.0.1"], []):
        async def resolve(host, port, answer=answer):
            return answer

        monkeypatch.setattr(sd, "_resolve", resolve)
        backend = RecordingBackend()
        with pytest.raises(httpcore.ConnectError):
            await sd.VettingBackend(backend).connect_tcp("x.example", 80)
        assert backend.dialed == []


async def test_vetting_backend_falls_through_to_the_next_vetted_address(monkeypatch):
    async def resolve(host, port):
        return ["2606:4700::1111", PUBLIC]

    monkeypatch.setattr(sd, "_resolve", resolve)
    backend = RecordingBackend(fail_on={"2606:4700::1111"})
    await sd.VettingBackend(backend).connect_tcp("x.example", 80)
    assert backend.dialed == [("2606:4700::1111", 80), (PUBLIC, 80)]


async def test_vetting_backend_unresolved_and_unix_socket_refused(monkeypatch):
    async def resolve(host, port):
        raise OSError("nx")

    monkeypatch.setattr(sd, "_resolve", resolve)
    with pytest.raises(httpcore.ConnectError):
        await sd.VettingBackend(RecordingBackend()).connect_tcp("x.example", 80)
    with pytest.raises(sd.BlockedAddress):
        await sd.VettingBackend(RecordingBackend()).connect_unix_socket("/var/run/docker.sock")


async def test_pinned_transport_uses_the_vetting_backend():
    """Pins the httpx private we swap (``_pool``): an upgrade that renames it fails here."""
    transport = sd.PinnedTransport()
    assert isinstance(transport._pool, httpcore.AsyncConnectionPool)
    assert isinstance(transport._pool._network_backend, sd.VettingBackend)
    await transport.aclose()


async def test_routing_transport_pins_only_what_asks_for_it():
    hits = []

    class Tag(httpx.AsyncBaseTransport):
        def __init__(self, name):
            self.name = name

        async def handle_async_request(self, request):
            hits.append(self.name)
            return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=sd.RoutingTransport(Tag("default"), Tag("pinned"))) as client:
        await client.get("http://localhost:8080/search")
        await client.get("http://news.example/a", extensions={sd.PUBLIC_ONLY_EXTENSION: True})
        await client.get("http://news.example/a", extensions={sd.PUBLIC_ONLY_EXTENSION: False})
    assert hits == ["default", "pinned", "default"]


async def test_end_to_end_client_with_routing_transport_hits_the_pin(dns):
    """The guarded fetch through a real RoutingTransport reaches the vetting backend."""
    backend = RecordingBackend()
    client = httpx.AsyncClient(transport=sd.RoutingTransport(pinned=sd.PinnedTransport(backend)))
    got = await sd.fetch_public(client, "http://news.example/a", sd.PAGE_POLICY)
    assert isinstance(got, sd.Fetched) and got.body == b"hi"
    assert backend.dialed == [(PUBLIC, 80)]
    await client.aclose()
