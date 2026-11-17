"""T299: revision/fetch.py (research, fix_fact, replace_image) and the SSRF-safe
downloader under it (safe_download.py). Fake HTTP, fake LLM, fake resolver: no
network, no DNS."""

from __future__ import annotations

import copy
import io
import os
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from PIL import Image

from pipeline import safe_download as sd
from pipeline.config import Settings
from pipeline.errors import ConfigFault, EnginesBlocked, Retryforever
from pipeline.models import Item, Status
from pipeline.retention import run_retention
from pipeline.revision import fetch
from pipeline.revision.model import Action, Op, OutcomeCode
from pipeline.stages.compose import IMAGE_MODES
from tests.conftest import TEST_ENV, FakeHTTP

PUBLIC_IP = "93.184.216.34"
RAW = "Fusion lab reports tungsten tiles survived plasma."
MATERIAL_NOTE = {"claim": "Tiles survived 74 shots", "sources": ["https://a.example/1"]}


# ---------------------------------------------------------------- image blobs


def png(width=900, height=500, seed=0) -> bytes:
    """Noise PNG: big enough (>15 kB) and wide enough (>=900) to pass every size gate."""
    rnd = random.Random(seed).randbytes(width * height * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), rnd).save(buf, "PNG")
    return buf.getvalue()


GOOD = png(seed=7)
OTHER = png(seed=11)


# ----------------------------------------------------------------- fake HTTP


@dataclass
class Stream:
    status: int = 200
    headers: dict = field(default_factory=lambda: {"content-type": "image/png"})
    chunks: list[bytes] = field(default_factory=list)
    consumed: int = 0


class StreamResponse:
    def __init__(self, spec: Stream) -> None:
        self.spec = spec
        self.status_code = spec.status
        self.headers = spec.headers

    async def aiter_raw(self):
        for chunk in self.spec.chunks:
            self.spec.consumed += 1
            yield chunk


class StreamCtx:
    def __init__(self, spec: Stream) -> None:
        self.spec = spec

    async def __aenter__(self):
        return StreamResponse(self.spec)

    async def __aexit__(self, *exc):
        return False


class Web(FakeHTTP):
    """FakeHTTP plus ``stream`` (what the downloader uses), keyed by exact url."""

    def __init__(self) -> None:
        super().__init__()
        self.streams: dict[str, Stream] = {}
        self.stream_calls: list[dict] = []

    def serve(self, url: str, blob: bytes, **kw) -> Stream:
        spec = Stream(chunks=[blob[i:i + 4096] for i in range(0, len(blob), 4096)], **kw)
        self.streams[url] = spec
        return spec

    def redirect(self, url: str, to: str, status: int = 302) -> None:
        self.streams[url] = Stream(status=status, headers={"location": to})

    def stream(self, method, url, **kw):
        self.stream_calls.append({"method": method, "url": url, **kw})
        if self.raise_on_request is not None:
            raise self.raise_on_request
        return StreamCtx(self.streams[url])

    def image_results(self, *urls: str) -> None:
        self.respond_for("/search", {"results": [{"img_src": u, "title": "t", "url": "https://p.example/"} for u in urls]})


@pytest.fixture
def web() -> Web:
    return Web()


@pytest.fixture(autouse=True)
def resolver(monkeypatch):
    """host -> addresses. ``*.example`` is public unless mapped; unknown others fail to resolve."""
    table: dict[str, list[str]] = {}

    async def fake_resolve(host: str, port: int) -> list[str]:
        if host in table:
            return table[host]
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]  # a literal resolves to itself
        except ValueError:
            pass
        if host.endswith(".example"):
            return [PUBLIC_IP]
        raise OSError("no such host")

    monkeypatch.setattr(sd, "_resolve", fake_resolve)
    return table


# ------------------------------------------------------------ item / settings


@pytest.fixture
def cfg(tmp_path) -> Settings:
    s = Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "t.db")})
    return replace(s, media_dir=tmp_path / "media", research_concurrency=1, docs_per_query=2)


DECK = [
    {"type": "hook", "headline": "Tungsten tiles", "sub": "They survived plasma.", "image": "/elsewhere/h.jpg",
     "image_mode": "background"},
    {"type": "point", "headline": "The test", "bullets": ["74 plasma shots", "3 days of runs"]},
    {"type": "takeaway", "headline": "So what", "sub": "It holds up."},
    {"type": "follow", "headline": "Follow", "sub": "Daily tech, explained."},
]


def item(**kw) -> Item:
    base = dict(id=7, source="channel", status=Status.AWAITING_APPROVAL, raw_text=RAW,
                caption="Cap.\n\n#fusion", research=[copy.deepcopy(MATERIAL_NOTE)], slides=copy.deepcopy(DECK))
    base.update(kw)
    return Item(**base)


def act(op=Op.REPLACE_IMAGE, slide=2, query="tungsten tile close-up photo"):
    return Action(op=op.value, ask=1, slide=slide, query=query)


# =========================================================== is_public_address


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
    "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
    "::ffff:10.0.0.1", "fec0::1", "64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::", "ff02::1", "not-an-ip", "",
])
def test_non_public_addresses_refused(addr):
    assert sd.is_public_address(addr) is False


@pytest.mark.parametrize("addr", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_accepted(addr):
    assert sd.is_public_address(addr) is True


# ====================================================================== SSRF


@pytest.mark.parametrize("url, why", [
    ("file:///etc/passwd", sd.Refusal.BAD_SCHEME),
    ("ftp://img.example/a.png", sd.Refusal.BAD_SCHEME),
    ("gopher://img.example/a", sd.Refusal.BAD_SCHEME),
    ("data:image/png;base64,AAAA", sd.Refusal.BAD_SCHEME),
    ("http://127.0.0.1/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://127.1/a.png", sd.Refusal.UNRESOLVED),
    ("http://10.0.0.5/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://169.254.169.254/latest/meta-data", sd.Refusal.PRIVATE_HOST),
    ("http://[::1]/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://[::ffff:127.0.0.1]/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://0.0.0.0/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://localhost/a.png", sd.Refusal.UNRESOLVED),
    ("http://user:pw@img.example/a.png", sd.Refusal.CREDENTIALS),
    ("http://img.example:8080/a.png", sd.Refusal.BAD_PORT),
    ("http://img.example:99999/a.png", sd.Refusal.BAD_URL),
    ("http:///a.png", sd.Refusal.BAD_URL),
    ("", sd.Refusal.BAD_SCHEME),
    ("http://evil\u00df.com/a.png", sd.Refusal.BAD_URL),
    ("http://fa\u03c2.com/a.png", sd.Refusal.BAD_URL),
    ("http://[fec0::1]/a.png", sd.Refusal.PRIVATE_HOST),
    ("http://img.example\\@127.0.0.1/a.png", sd.Refusal.BAD_URL),
    ("http://img.example/a b.png", sd.Refusal.BAD_URL),
    (" http://127.0.0.1/a.png", sd.Refusal.BAD_URL),
    ("http://img.example/a.png\r\nHost: x", sd.Refusal.BAD_URL),
    ("http://nonexistent.invalid/a.png", sd.Refusal.UNRESOLVED),
])
async def test_hostile_urls_refused_without_a_request(web, url, why):
    got = await sd.download_public_image(web, url)
    assert got is why
    assert web.stream_calls == [] and web.calls == []


async def test_name_resolving_to_a_private_address_refused(web, resolver):
    resolver["rebind.example"] = [PUBLIC_IP, "127.0.0.1"]  # one private answer is enough
    resolver["internal.example"] = ["10.0.0.9"]
    assert await sd.download_public_image(web, "https://rebind.example/a.png") is sd.Refusal.PRIVATE_HOST
    assert await sd.download_public_image(web, "https://internal.example/a.png") is sd.Refusal.PRIVATE_HOST
    assert web.stream_calls == []


async def test_redirect_to_private_address_refused_after_first_hop(web):
    web.redirect("http://img.example/a.png", "http://127.0.0.1/secret.png")
    assert await sd.download_public_image(web, "http://img.example/a.png") is sd.Refusal.PRIVATE_HOST
    assert [c["url"] for c in web.stream_calls] == ["http://img.example/a.png"]


async def test_redirect_to_file_scheme_and_relative_redirect(web):
    web.redirect("http://img.example/a.png", "file:///etc/passwd")
    assert await sd.download_public_image(web, "http://img.example/a.png") is sd.Refusal.BAD_SCHEME
    web.redirect("http://img.example/b.png", "/real.png")  # relative -> same public host
    web.serve("http://img.example/real.png", GOOD)
    got = await sd.download_public_image(web, "http://img.example/b.png")
    assert isinstance(got, sd.Downloaded) and got.blob == GOOD


async def test_redirect_hops_are_capped(web):
    for i in range(sd.MAX_REDIRECTS + 2):
        web.redirect(f"https://img.example/{i}", f"https://img.example/{i + 1}")
    assert await sd.download_public_image(web, "https://img.example/0") is sd.Refusal.TOO_MANY_REDIRECTS
    assert len(web.stream_calls) == sd.MAX_REDIRECTS + 1
    # exactly MAX_REDIRECTS hops still succeeds
    for i in range(sd.MAX_REDIRECTS):
        web.redirect(f"https://img.example/ok{i}", f"https://img.example/ok{i + 1}")
    web.serve(f"https://img.example/ok{sd.MAX_REDIRECTS}", GOOD)
    assert isinstance(await sd.download_public_image(web, "https://img.example/ok0"), sd.Downloaded)


async def test_redirects_are_never_followed_by_the_client(web):
    web.serve("https://img.example/a.png", GOOD)
    await sd.download_public_image(web, "https://img.example/a.png")
    assert web.stream_calls[0]["follow_redirects"] is False
    assert web.stream_calls[0]["timeout"] == sd.REQUEST_TIMEOUT_S


# ===================================================================== bodies


async def test_declared_oversize_refused_before_reading(web):
    spec = web.serve("https://img.example/a.png", GOOD,
                     headers={"content-type": "image/png", "content-length": str(sd.MAX_IMAGE_BYTES + 1)})
    assert await sd.download_public_image(web, "https://img.example/a.png") is sd.Refusal.TOO_BIG
    assert spec.consumed == 0


async def test_streamed_oversize_stops_reading_at_the_cap(web):
    chunk = b"x" * 1_000_000
    spec = Stream(chunks=[chunk] * 100)  # no content-length: a liar / chunked
    web.streams["https://img.example/big.png"] = spec
    assert await sd.download_public_image(web, "https://img.example/big.png") is sd.Refusal.TOO_BIG
    assert spec.consumed == sd.MAX_IMAGE_BYTES // 1_000_000 + 1  # not 100


@pytest.mark.parametrize("headers", [
    {"content-type": "text/html"}, {"content-type": "application/octet-stream"}, {},
    {"content-type": "image/svg+xml"},  # declared image, but SVG fails the decode below
])
async def test_non_image_responses_refused(web, headers):
    web.serve("https://img.example/a", b"<html>" + b"x" * 20_000, headers=headers)
    got = await sd.download_public_image(web, "https://img.example/a")
    assert got in (sd.Refusal.NOT_IMAGE_TYPE, sd.Refusal.UNDECODABLE)


async def test_punycode_host_is_fine(web):
    web.serve("https://xn--evil-yna.example/a.png", GOOD)
    assert isinstance(await sd.download_public_image(web, "https://xn--evil-yna.example/a.png"), sd.Downloaded)


async def test_https_to_http_downgrade_refused_but_http_to_https_ok(web):
    web.redirect("https://img.example/a.png", "http://img.example/a.png")
    assert await sd.download_public_image(web, "https://img.example/a.png") is sd.Refusal.DOWNGRADE
    web.redirect("http://img.example/b.png", "https://img.example/real.png")
    web.serve("https://img.example/real.png", GOOD)
    assert isinstance(await sd.download_public_image(web, "http://img.example/b.png"), sd.Downloaded)


async def test_identity_requested_and_encoded_responses_refused(web):
    spec = web.serve("https://img.example/a.png", GOOD, headers={"content-type": "image/png", "content-encoding": "gzip"})
    assert await sd.download_public_image(web, "https://img.example/a.png") is sd.Refusal.ENCODED
    assert spec.consumed == 0
    assert web.stream_calls[0]["headers"]["Accept-Encoding"] == "identity"
    web.serve("https://img.example/b.png", GOOD, headers={"content-type": "image/png", "content-encoding": "identity"})
    assert isinstance(await sd.download_public_image(web, "https://img.example/b.png"), sd.Downloaded)


async def test_stacked_gzip_bomb_through_real_httpx_stays_bounded(monkeypatch):
    import gzip

    import httpx

    layer = b"\0" * 20_000_000
    for _ in range(2):
        layer = gzip.compress(layer)
    wire = len(layer)
    sent = {"n": 0}

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, wire, 64):
                sent["n"] += 64
                yield layer[i:i + 64]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-type": "image/png", "content-encoding": "gzip, gzip"},
                              stream=Body())

    import tracemalloc
    tracemalloc.start()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await sd.download_public_image(client, "https://img.example/bomb.png")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert got is sd.Refusal.ENCODED
    assert sent["n"] == 0 and peak < 5_000_000  # refused on headers; nothing inflated


async def test_declared_type_is_enforced_even_for_a_valid_image(web):
    web.serve("https://img.example/a", GOOD, headers={"content-type": "text/html"})
    assert await sd.download_public_image(web, "https://img.example/a") is sd.Refusal.NOT_IMAGE_TYPE


async def test_slow_drip_host_hits_the_deadline(web, monkeypatch):
    import asyncio

    class Stall(Stream):
        pass

    spec = Stall()

    async def forever(self):
        await asyncio.sleep(2)
        yield b""

    monkeypatch.setattr(StreamResponse, "aiter_raw", forever)
    monkeypatch.setattr(sd, "DOWNLOAD_DEADLINE_S", 0.05)
    web.streams["https://img.example/slow.png"] = spec
    assert await sd.download_public_image(web, "https://img.example/slow.png") is sd.Refusal.UNREACHABLE


async def test_http_errors_and_dead_hosts_refused(web):
    web.streams["https://img.example/404"] = Stream(status=404)
    web.streams["https://img.example/noloc"] = Stream(status=302, headers={})
    assert await sd.download_public_image(web, "https://img.example/404") is sd.Refusal.HTTP_STATUS
    assert await sd.download_public_image(web, "https://img.example/noloc") is sd.Refusal.HTTP_STATUS
    web.raise_on_request = RuntimeError("boom with https://secret.example/token in its text")
    assert await sd.download_public_image(web, "https://img.example/x") is sd.Refusal.UNREACHABLE


async def test_content_type_image_but_bytes_are_not(web):
    web.serve("https://img.example/fake.png", b"MZ" + os.urandom(30_000))
    assert await sd.download_public_image(web, "https://img.example/fake.png") is sd.Refusal.UNDECODABLE


async def test_truncated_image_refused_as_undecodable(web):
    web.serve("https://img.example/cut.png", GOOD[: len(GOOD) // 2])
    assert await sd.download_public_image(web, "https://img.example/cut.png") is sd.Refusal.UNDECODABLE


async def test_size_and_dimension_floors_and_formats(web):
    tiny = io.BytesIO()
    Image.new("RGB", (10, 10)).save(tiny, "PNG")
    web.serve("https://img.example/tiny.png", tiny.getvalue())
    assert await sd.download_public_image(web, "https://img.example/tiny.png") is sd.Refusal.TOO_SMALL
    web.serve("https://img.example/small.png", png(width=200, height=200))
    assert await sd.download_public_image(web, "https://img.example/small.png") is sd.Refusal.LOW_RESOLUTION
    gif = io.BytesIO()
    Image.frombytes("RGB", (900, 500), os.urandom(900 * 500 * 3)).convert("P").save(gif, "GIF")
    web.serve("https://img.example/a.gif", gif.getvalue() + b"\0" * 20_000)
    assert await sd.download_public_image(web, "https://img.example/a.gif") is sd.Refusal.UNDECODABLE  # PIL is told the allowed formats


async def test_pixel_bomb_refused_before_decode(web, monkeypatch):
    monkeypatch.setattr(sd, "MAX_IMAGE_PIXELS", 900 * 500 - 1)
    web.serve("https://img.example/a.png", GOOD)
    assert await sd.download_public_image(web, "https://img.example/a.png") is sd.Refusal.TOO_MANY_PIXELS


async def test_good_image_returned_with_suffix_from_decoded_format(web):
    jpg = io.BytesIO()
    Image.frombytes("RGB", (900, 500), os.urandom(900 * 500 * 3)).save(jpg, "JPEG", quality=100)
    web.serve("https://img.example/photo.php?x=1", jpg.getvalue(), headers={"content-type": "image/jpeg; charset=x"})
    got = await sd.download_public_image(web, "https://img.example/photo.php?x=1")
    assert isinstance(got, sd.Downloaded) and got.fmt == "jpeg" and got.suffix == ".jpg" and (got.width, got.height) == (900, 500)


# ================================================================== research


def search_hits(web: Web, *urls: str) -> None:
    web.respond_for("/search", {"results": [{"url": u, "title": "t", "content": "some content"} for u in urls]})
    web.respond(200, "<html><body><article>" + ("Body text about tiles. " * 60) + "</article></body></html>")


GATE_OK = {"relevant": True, "why": "on topic"}
NOTE = {"claim": "The tiles survived 120 shots", "detail": "Report: 120 shots, 5 days.", "confidence": "high"}


async def run_research(llm, web, cfg, it=None, action=None):
    return await fetch.research(llm, it or item(), action or act(Op.RESEARCH, slide=0, query="tungsten tile shots"), cfg, web)


async def test_research_found_note_is_stamped_revision(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, NOTE])
    it = item()
    before = copy.deepcopy(it.research)
    notes, out = await run_research(fake_llm, web, cfg, it)
    assert out.code is OutcomeCode.APPLIED and out.op == "research"
    assert len(notes) == 1 and notes[0]["origin"] == "revision" == fetch.ORIGIN_REVISION
    assert notes[0]["claim"] == NOTE["claim"] and notes[0]["sources"] == ["https://a.example/1"]
    assert it.research == before  # the item is not mutated
    gate = fake_llm.calls[0].user
    assert "tungsten tile shots" in gate and "Fusion lab" in gate  # query + story context reach the gate


async def test_research_empty_search_is_not_found(fake_llm, web, cfg):
    web.respond_for("/search", {"results": []})
    notes, out = await run_research(fake_llm, web, cfg)
    assert notes == [] and out.code is OutcomeCode.NOT_FOUND and out.reason == fetch.REASON_NO_RESULTS
    assert fake_llm.calls == []


async def test_research_every_page_irrelevant_is_not_found(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1", "https://b.example/1")
    fake_llm.queue_each([{"relevant": False, "why": "mouse cursors"}] * 2)
    notes, out = await run_research(fake_llm, web, cfg)
    assert notes == [] and out.code is OutcomeCode.NOT_FOUND
    assert len(fake_llm.calls) == 2  # no note-writing call on refused pages


async def test_research_writer_says_pages_do_not_answer_is_not_found(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, {**NOTE, "confidence": "low"}])
    notes, out = await run_research(fake_llm, web, cfg)
    assert notes == [] and out.code is OutcomeCode.NOT_FOUND and out.reason == fetch.REASON_NOT_ANSWERED


async def test_research_blank_claim_is_not_found(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, {**NOTE, "claim": "  "}])
    notes, out = await run_research(fake_llm, web, cfg)
    assert notes == [] and out.code is OutcomeCode.NOT_FOUND


async def test_research_broken_gate_fail_open_note_is_refused(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([RuntimeError("gate down"), NOTE])  # gate ERRORED -> _research_one fails open
    notes, out = await run_research(fake_llm, web, cfg)
    assert notes == [] and out.code is OutcomeCode.UNAVAILABLE and out.reason == fetch.REASON_SOURCE_UNVERIFIED


async def test_research_blank_query_never_searches(fake_llm, web, cfg):
    notes, out = await run_research(fake_llm, web, cfg, action=act(Op.RESEARCH, slide=0, query="   "))
    assert notes == [] and out.code is OutcomeCode.DROPPED_INVALID and web.calls == []


async def test_research_transport_error_becomes_retryforever(fake_llm, web, cfg):
    web.raise_on_request = RuntimeError("down")  # searx wraps a transport error
    with pytest.raises(Retryforever):
        await run_research(fake_llm, web, cfg)


@pytest.mark.parametrize("fault", [Retryforever("x"), ConfigFault("x"), EnginesBlocked("x")])
async def test_research_infra_fault_propagates_with_its_exact_type(fake_llm, web, cfg, monkeypatch, fault):
    async def boom(*a, **k):
        raise fault

    monkeypatch.setattr(fetch, "_research_one", boom)
    with pytest.raises(type(fault)):
        await run_research(fake_llm, web, cfg)


async def test_research_search_5xx_is_an_outage(fake_llm, web, cfg):
    web.respond(503, {})
    with pytest.raises(Retryforever):
        await run_research(fake_llm, web, cfg)


async def test_research_llm_outage_in_gate_or_writer_propagates(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue(Retryforever("ollama down"))
    with pytest.raises(Retryforever):
        await run_research(fake_llm, web, cfg)
    fake_llm.queue_each([GATE_OK, Retryforever("down")])
    with pytest.raises(Retryforever):
        await run_research(fake_llm, web, cfg)


# =================================================================== fix_fact


def fix_action(slide=2, query="how many plasma shots"):
    return Action(op=Op.FIX_FACT.value, ask=2, slide=slide, query=query)


def point(**kw):
    s = {"type": "point", "headline": "The test", "bullets": ["74 plasma shots", "3 days of runs"]}
    s.update(kw)
    return s


async def run_fix(llm, web, cfg, it=None, action=None, note="check the number on slide 2"):
    it = it or item()
    return await fetch.fix_fact(llm, it, it.slides, action or fix_action(), note, cfg, web)


async def test_fix_fact_researches_then_edits_with_new_notes_as_priority(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, NOTE, point(bullets=["120 plasma shots", "5 days of runs"])])
    slide, notes, out = await run_fix(fake_llm, web, cfg)
    assert out.code is OutcomeCode.APPLIED and out.op == "fix_fact" and out.slide == 2 and out.ask == 2
    assert slide["bullets"] == ["120 plasma shots", "5 days of runs"]
    assert [n["origin"] for n in notes] == ["revision"]
    edit_prompt = fake_llm.calls[2].user
    assert "120 shots, 5 days" in edit_prompt and fetch.FIX_FACT_INSTRUCTION in edit_prompt
    assert edit_prompt.index(fetch.NEW_RESEARCH_HEADING) < edit_prompt.index(fetch.EXISTING_MATERIAL_HEADING)
    assert "[DATA-" in edit_prompt  # web-derived text is a delimited data block (D260)


async def test_fix_fact_invented_figure_still_rejected_but_notes_kept(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    bad = point(bullets=["999 plasma shots", "5 days of runs"])  # 999 is in no source
    fake_llm.queue_each([GATE_OK, NOTE, bad, bad])
    it = item()
    slide, notes, out = await run_fix(fake_llm, web, cfg, it)
    assert slide is None and out.code is OutcomeCode.REJECTED_INTEGRITY and out.op == "fix_fact"
    assert "999" in out.reason and len(notes) == 1 and it.slides == DECK


async def test_fix_fact_operator_note_may_back_a_figure_but_planner_query_may_not(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    # 2026 appears only in the operator's raw note
    fake_llm.queue_each([GATE_OK, NOTE, point(headline="The 2026 test")])
    slide, _, out = await run_fix(fake_llm, web, cfg, note="make it the 2026 test")
    assert out.code is OutcomeCode.APPLIED and slide["headline"] == "The 2026 test"
    # ... while the model-written query is never backing: 1999 only in the query
    search_hits(web, "https://a.example/1")
    bad = point(headline="The 1999 test")
    fake_llm.queue_each([GATE_OK, NOTE, bad, bad])
    slide, _, out = await run_fix(fake_llm, web, cfg, action=fix_action(query="1999 shots"))
    assert slide is None and out.code is OutcomeCode.REJECTED_INTEGRITY


async def test_fix_fact_nothing_found_makes_no_edit_call(fake_llm, web, cfg):
    web.respond_for("/search", {"results": []})
    slide, notes, out = await run_fix(fake_llm, web, cfg)
    assert slide is None and notes == []
    assert out.code is OutcomeCode.NOT_FOUND and out.op == "fix_fact" and out.slide == 2
    assert fake_llm.calls == []


async def test_fix_fact_slide_already_right_is_unchanged_with_notes(fake_llm, web, cfg):
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, NOTE, point()])  # model returns the slide as it was
    slide, notes, out = await run_fix(fake_llm, web, cfg)
    assert slide is None and out.code is OutcomeCode.UNCHANGED and out.op == "fix_fact" and len(notes) == 1


async def test_fix_fact_bad_slide_and_blank_query_fallback(fake_llm, web, cfg):
    slide, notes, out = await run_fix(fake_llm, web, cfg, action=fix_action(slide=9))
    assert slide is None and out.code is OutcomeCode.DROPPED_INVALID and web.calls == []
    web.respond_for("/search", {"results": []})
    await run_fix(fake_llm, web, cfg, action=fix_action(query=""))
    assert "The test" in web.calls[0].params["q"]  # derived from the slide's headline


async def test_fix_fact_outage_propagates(fake_llm, web, cfg):
    web.respond(503, {})
    with pytest.raises(Retryforever):
        await run_fix(fake_llm, web, cfg)
    search_hits(web, "https://a.example/1")
    fake_llm.queue_each([GATE_OK, NOTE, Retryforever("good model down")])
    with pytest.raises(Retryforever):
        await run_fix(fake_llm, web, cfg)


# ============================================================== replace_image


IMG = "https://img.example/a.png"
IMG2 = "https://cdn.example/b.png"


async def run_replace(web, cfg, it=None, action=None):
    it = it or item()
    return await fetch.replace_image(it, it.slides, action or act(), cfg, web)


def saved_dir(cfg, item_id=7) -> Path:
    return Path(cfg.media_dir) / "researched" / str(item_id)


async def test_replace_image_happy_path(web, cfg):
    web.image_results(IMG)
    web.serve(IMG, GOOD)
    it = item()
    before = copy.deepcopy(it.slides)
    slide, out = await run_replace(web, cfg, it)
    assert out.code is OutcomeCode.APPLIED and out.op == "replace_image" and out.slide == 2 and out.ask == 1
    assert out.reason == fetch.REASON_IMAGE_UNCHECKED  # no T223 gate yet: said so, not silent
    path = Path(slide["image"])
    assert path.parent == saved_dir(cfg) and re.fullmatch(r"rev_[0-9a-f]{16}\.png", path.name)
    assert path.read_bytes() == GOOD and Image.open(path).size == (900, 500)
    assert slide["image_mode"] in IMAGE_MODES
    assert {k: v for k, v in slide.items() if k not in ("image", "image_mode")} == it.slides[1]
    assert it.slides == before  # input untouched; the result is a copy


async def test_saved_suffix_follows_the_decoded_format(web, cfg):
    jpg = io.BytesIO()
    Image.frombytes("RGB", (900, 500), random.Random(3).randbytes(900 * 500 * 3)).save(jpg, "JPEG", quality=100)
    web.image_results("https://img.example/looks-like.png")
    web.serve("https://img.example/looks-like.png", jpg.getvalue(), headers={"content-type": "image/jpeg"})
    slide, _ = await run_replace(web, cfg)
    assert slide["image"].endswith(".jpg")


async def test_replace_image_keeps_existing_mode_and_hook_default(web, cfg):
    web.image_results(IMG)
    web.serve(IMG, GOOD)
    slide, _ = await run_replace(web, cfg, action=act(slide=1))
    assert slide["image_mode"] == "background"  # kept from the hook's own
    it = item()
    del it.slides[0]["image_mode"]
    slide, _ = await run_replace(web, cfg, it, act(slide=1))
    assert slide["image_mode"] == fetch.HOOK_IMAGE_MODE
    assert fetch.HOOK_IMAGE_MODE in IMAGE_MODES and fetch.DEFAULT_IMAGE_MODE in IMAGE_MODES


async def test_same_image_is_unchanged_and_next_candidate_is_preferred(web, cfg):
    current = saved_dir(cfg) / "bg_123.png"
    current.parent.mkdir(parents=True)
    current.write_bytes(GOOD)
    it = item()
    it.slides[1]["image"] = str(current)
    it.slides[1]["image_mode"] = "inset"
    web.image_results(IMG)
    web.serve(IMG, GOOD)  # byte-identical to what is on the slide
    slide, out = await run_replace(web, cfg, it)
    assert slide is None and out.code is OutcomeCode.UNCHANGED and out.reason == fetch.REASON_SAME_IMAGE
    assert sorted(p.name for p in saved_dir(cfg).iterdir()) == ["bg_123.png"]  # nothing written
    web.image_results(IMG, IMG2)
    web.serve(IMG2, OTHER)
    slide, out = await run_replace(web, cfg, it)
    assert out.code is OutcomeCode.APPLIED and slide["image"] != str(current)
    assert Path(slide["image"]).read_bytes() == OTHER and slide["image_mode"] == "inset"


async def test_current_image_outside_media_dir_is_never_opened(web, cfg, tmp_path):
    secret = tmp_path / "outside.png"
    secret.write_bytes(GOOD)
    it = item()
    it.slides[1]["image"] = str(secret)
    web.image_results(IMG)
    web.serve(IMG, GOOD)
    slide, out = await run_replace(web, cfg, it)
    assert out.code is OutcomeCode.APPLIED  # identical bytes, but the outside file is not trusted/read


async def test_every_candidate_hostile_is_not_found_and_nothing_fetched(web, cfg, resolver):
    resolver["evil.example"] = ["10.0.0.1"]
    web.image_results("file:///etc/passwd", "http://127.0.0.1/a.png", "http://169.254.169.254/x.png",
                      "https://evil.example/a.png", "ftp://img.example/a.png", "http://img.example:6379/a.png")
    slide, out = await run_replace(web, cfg)
    assert slide is None and out.code is OutcomeCode.NOT_FOUND and out.reason == fetch.REASON_NO_USABLE_IMAGE
    assert web.stream_calls == [] and not saved_dir(cfg).exists()


async def test_hostile_candidate_skipped_good_one_used(web, cfg):
    web.image_results("http://127.0.0.1/a.png", IMG)
    web.serve(IMG, GOOD)
    slide, out = await run_replace(web, cfg)
    assert out.code is OutcomeCode.APPLIED and [c["url"] for c in web.stream_calls] == [IMG]


async def test_oversize_and_non_image_candidates_are_not_saved(web, cfg):
    web.image_results(IMG, IMG2)
    web.streams[IMG] = Stream(chunks=[b"x" * 1_000_000] * 20)
    web.serve(IMG2, b"<html>" + b"x" * 20_000, headers={"content-type": "text/html"})
    slide, out = await run_replace(web, cfg)
    assert slide is None and out.code is OutcomeCode.NOT_FOUND and out.reason == fetch.REASON_NO_USABLE_IMAGE
    assert not saved_dir(cfg).exists()


async def test_no_search_results_is_not_found(web, cfg):
    web.respond_for("/search", {"results": []})
    slide, out = await run_replace(web, cfg)
    assert slide is None and out.code is OutcomeCode.NOT_FOUND and out.reason == fetch.REASON_NO_CANDIDATES


async def test_image_search_outage_propagates_never_not_found(web, cfg):
    web.raise_on_request = RuntimeError("searx down")
    with pytest.raises(Retryforever):
        await run_replace(web, cfg)
    web.raise_on_request = None
    web.respond(503, {})
    with pytest.raises(Retryforever):
        await run_replace(web, cfg)


async def test_filename_never_derived_from_remote_input(web, cfg):
    evil = "https://img.example/..%2f..%2fetc/passwd.png?name=../../../evil.sh&x=%00"
    web.image_results(evil)
    web.serve(evil, GOOD, headers={"content-type": "image/png", "content-disposition": 'attachment; filename="../../evil.php"'})
    slide, out = await run_replace(web, cfg)
    assert out.code is OutcomeCode.APPLIED
    path = Path(slide["image"]).resolve()
    assert path.parent == saved_dir(cfg).resolve() and re.fullmatch(r"rev_[0-9a-f]{16}\.png", path.name)
    assert [p.name for p in (Path(cfg.media_dir) / "researched").rglob("*")] == [str(7), path.name]
    # two downloads of the same url never overwrite each other
    slide2, _ = await run_replace(web, cfg)
    assert slide2["image"] != slide["image"] and Path(slide["image"]).exists()


async def test_fix_fact_closing_slide_refused_before_research(fake_llm, web, cfg):
    slide, notes, out = await run_fix(fake_llm, web, cfg, action=fix_action(slide=4))
    assert slide is None and out.code is OutcomeCode.DROPPED_INVALID and out.reason == "closing_slide"
    assert web.calls == [] and fake_llm.calls == []


async def test_replace_image_bad_targets_never_search(web, cfg):
    for action, reason in [(act(slide=9), "out_of_range"), (act(slide=4), "closing_slide"),
                           (act(query="  "), "missing_arg")]:
        slide, out = await run_replace(web, cfg, action=action)
        assert slide is None and out.code is OutcomeCode.DROPPED_INVALID and out.reason == reason
    assert web.calls == []


async def test_no_exception_text_in_any_outcome(web, cfg):
    web.image_results(IMG)
    web.raise_on_request = None
    web.streams[IMG] = Stream(status=500)
    _, out = await run_replace(web, cfg)
    closed = {v for k, v in vars(fetch).items() if k.startswith("REASON_")} | {""}
    assert out.reason in closed


# ================================================================== retention


async def test_downloaded_image_is_covered_by_retention(web, cfg, db):
    media = Path(cfg.media_dir)
    cfg = replace(cfg, media_dir=media)
    item_id = await db.insert_item(source="channel", source_chat_id=1, source_msg_id=1, raw_text="t")
    web.image_results(IMG)
    web.serve(IMG, GOOD)
    it = item(id=item_id)
    slide, out = await fetch.replace_image(it, it.slides, act(), cfg, web)
    path = Path(slide["image"])
    assert path.is_file() and path.parent == media / "researched" / str(item_id)
    await db.conn.execute("UPDATE items SET status=?, status_updated_at=? WHERE id=?",
                          (Status.REJECTED.value, "2020-01-01T00:00:00+00:00", item_id))
    await db.conn.commit()
    other = media / "researched" / "9999" / "keep.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    rep = await run_retention(db, cfg, apply=True)
    assert not path.exists() and other.exists() and rep.files >= 1



# ======================================================== T299 tester defects


@pytest.mark.parametrize("w, h, expect", [
    (900, 500, "ok"), (1500, 500, "ok"), (1501, 500, sd.Refusal.BAD_ASPECT),
    (900, 499, sd.Refusal.LOW_RESOLUTION), (499, 900, sd.Refusal.LOW_RESOLUTION),
    (500, 1500, "ok"), (500, 1501, sd.Refusal.BAD_ASPECT), (5000, 4, sd.Refusal.LOW_RESOLUTION),
])
def test_decode_shape_boundaries(w, h, expect):
    got = sd._decode(png(width=w, height=h, seed=5))
    if expect == "ok":
        assert isinstance(got, sd.Downloaded)
    else:
        assert got is expect


def test_constants_are_what_the_comments_justify():
    assert sd.MIN_IMAGE_SHORT_EDGE == 500 and sd.MAX_ASPECT_RATIO == 3.0


from pipeline import search as search_mod  # noqa: E402
from pipeline.integrity import typed_numbers_in, unbacked_figures  # noqa: E402


@pytest.mark.parametrize("status, body, exc", [
    (500, {}, Retryforever), (429, {}, Retryforever), (403, {}, ConfigFault),
    (200, "<html>proxy</html>", Retryforever),
    (200, {"results": [], "unresponsive_engines": [["bing images", "Suspended: too many requests"]]}, EnginesBlocked),
    (200, {"results": [], "unresponsive_engines": [["google images", "CAPTCHA"]]}, EnginesBlocked),
])
async def test_image_search_outages_are_exceptions_like_searx(status, body, exc):
    http = FakeHTTP()
    http.respond(status, body)
    with pytest.raises(exc):
        await search_mod.image_search(http, "http://s", "q")
    with pytest.raises(exc):  # the text search classifies the same response identically
        await search_mod.searx(http, "http://s", "q")


async def test_image_search_unchanged_paths():
    http = FakeHTTP()
    http.respond(404, {})
    assert await search_mod.image_search(http, "http://s", "q") == []  # a bad query is not an outage
    http.respond(200, {"results": []})
    assert await search_mod.image_search(http, "http://s", "q") == []  # a genuine empty result
    http.respond(200, {"results": [{"img_src": "https://upload.wikimedia.org/a.jpg", "title": "t", "url": "https://p/"},
                                   {"img_src": "https://x.example/b.jpg"}, {"img_src": "ftp://x/c"}]})
    got = await search_mod.image_search(http, "http://s", "q")
    assert [c["url"] for c in got] == ["https://upload.wikimedia.org/a.jpg", "https://x.example/b.jpg"]
    assert http.calls[-1].params["categories"] == "images"


async def test_live_background_search_still_swallows_outages(cfg):
    """The research stage's hook-background hunt is optional and never raises: the
    new exceptions are swallowed there exactly as the old 5xx one was."""
    from pipeline.stages.research import _find_background

    for status in (429, 403, 500):
        http = FakeHTTP()
        http.respond(status, {})
        assert await _find_background("q", "q", item(), http, cfg) is None


@pytest.mark.parametrize("text, material, flagged", [
    ("2 billion", "Revenue hit 2 billion last year.", []),
    ("$2 billion", "Revenue hit 2 billion last year.", []),
    ("777 million", "777 million users", []),
    ("1.5 trillion", "worth 1.5 trillion", []),
    ("2 billion", "Revenue hit 2 million last year.", ["2e+09"]),
    ("2 billion", "nothing numeric here", ["2e+09"]),
    ("3 billion", "Revenue hit 2 billion", ["3e+09"]),
])
def test_magnitude_figures_backed_by_verbatim_material(text, material, flagged):
    assert unbacked_figures(text, typed_numbers_in(material)) == flagged
