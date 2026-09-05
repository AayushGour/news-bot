import pytest

from pipeline.models import Item, Status
from pipeline.search import dedupe_by_domain, extract_urls, is_blocked, searx
from pipeline.stages.extract import extract


def _item(text="", media=None):
    return Item(id=1, source="channel", status=Status.TRIAGED,
                raw_text=text, raw_media_paths=media or [])


# ------------------------------------------------------------------ url utils


def test_extract_urls_trims_trailing_punctuation():
    urls = extract_urls("see https://example.com/a, and https://b.example/x.")
    assert urls == ["https://example.com/a", "https://b.example/x"]


def test_extract_urls_dedupes():
    assert extract_urls("https://a.com https://a.com") == ["https://a.com"]


def test_blocklist_catches_the_poisoned_domains():
    """These are the real domains that polluted the PoC's research."""
    assert is_blocked("https://custom-cursor.com/en/collection/anime")
    assert is_blocked("https://www.rw-designer.com/cursor-set/anime")
    assert not is_blocked("https://www.teslarati.com/openai-cursor")


def test_dedupe_by_domain_keeps_one_per_site():
    results = [
        {"url": "https://a.com/1"}, {"url": "https://a.com/2"},
        {"url": "https://b.com/1"}, {"url": "https://c.com/1"},
    ]
    assert [r["url"] for r in dedupe_by_domain(results, keep=3)] == [
        "https://a.com/1", "https://b.com/1", "https://c.com/1",
    ]


# -------------------------------------------------------------------- searxng


async def test_searx_filters_blocked_domains(fake_http, settings):
    fake_http.respond({"results": [
        {"url": "https://custom-cursor.com/x", "title": "cursors", "content": ""},
        {"url": "https://teslarati.com/y", "title": "news", "content": ""},
    ]})
    results = await searx(fake_http, settings.searxng_url, "cursor openai")
    assert [r["url"] for r in results] == ["https://teslarati.com/y"]


async def test_searx_returns_empty_on_error_rather_than_raising(fake_http, settings):
    """Search being down must not fail the item."""
    fake_http.respond(500, "boom")
    assert await searx(fake_http, settings.searxng_url, "q") == []


async def test_searx_survives_a_connection_error(fake_http, settings):
    fake_http.raise_on_request = ConnectionError("refused")
    assert await searx(fake_http, settings.searxng_url, "q") == []


# -------------------------------------------------------------------- extract


async def test_failed_url_is_recorded_not_raised(fake_http, fake_llm):
    """Spec §10: a dead link is survivable, not a failure."""
    fake_http.respond(404, "gone")
    out = await extract(_item("see https://example.com/x"), fake_llm, fake_http)

    entry = out["extracted"]["url_texts"][0]
    assert entry["url"] == "https://example.com/x"
    assert "error" in entry


async def test_successful_url_yields_text(fake_http, fake_llm):
    fake_http.respond(200, "<html><body><article>"
                           + ("Real article body. " * 40)
                           + "</article></body></html>")
    out = await extract(_item("https://example.com/a"), fake_llm, fake_http)

    entry = out["extracted"]["url_texts"][0]
    assert "Real article body" in entry.get("text", "")


async def test_text_only_item_extracts_nothing_and_does_not_fail(fake_http, fake_llm):
    out = await extract(_item("no links here at all"), fake_llm, fake_http)
    assert out["extracted"] == {"url_texts": [], "image_descriptions": []}
    assert fake_llm.calls == []


async def test_images_go_through_the_vision_model(fake_http, fake_llm):
    fake_llm.queue("Headline reads 'OpenAI blocks Cursor'. Screenshot of a tweet.")
    out = await extract(_item("look", media=["/tmp/a.png"]), fake_llm, fake_http)

    described = out["extracted"]["image_descriptions"][0]
    assert "OpenAI blocks Cursor" in described["description"]
    assert fake_llm.calls[0].role == "vision"


async def test_vision_failure_is_survivable(fake_http, fake_llm):
    """The post text usually carries the story on its own."""
    class Boom:
        async def vision(self, *a, **k):
            raise RuntimeError("model missing")

    out = await extract(_item("look", media=["/tmp/a.png"]), Boom(), fake_http)
    assert "error" in out["extracted"]["image_descriptions"][0]


async def test_fetch_preserves_fenced_code_blocks(fake_http):
    """Regression: trafilatura's default extraction silently drops <pre>/<code>.

    A page documenting a file format then yields only prose describing it, so
    the composer has no real syntax to show and correctly refuses to invent
    any. The failure looked like a prompting problem three stages downstream.
    """
    from pipeline.search import fetch_text

    html = (
        "<html><body><article>"
        "<p>The layout looks like this, and here is a longer sentence so the "
        "extractor treats this as a real article body worth keeping.</p>"
        "<pre><code>---\ntype: concept\ntitle: What is OKF\n---</code></pre>"
        "<p>Each concept file carries YAML frontmatter followed by Markdown, "
        "which is what makes the format readable without any tooling.</p>"
        "</article></body></html>"
    )
    fake_http.respond(200, html)

    text = await fetch_text(fake_http, "https://okf.example/spec")

    assert text, "extraction returned nothing"
    assert "type: concept" in text, "code block was stripped"
