"""Tests for scripts/eval_research.py.

T73's AC1 is the load-bearing claim of the whole file: two consecutive runs
against an identical config must produce byte-identical corpora with ZERO
network fetches on the second. That is proven here two ways --
``test_caching_http_never_touches_a_raising_backend_on_a_cache_hit`` proves
the HTTP cache layer alone, deterministically, with no model in the loop at
all; ``test_two_runs_against_the_same_cache_make_zero_network_calls_on_the_second``
proves the full assembly (``research()`` + ``Recorder`` + ``CachingHTTP``)
end to end, offline, with a stub LLM standing in for a live model.

Loaded the same way ``tests/test_eval_compose_rescore.py`` loads
``eval_compose.py`` -- ``scripts/`` is not an importable package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def eval_research():
    spec = importlib.util.spec_from_file_location(
        "eval_research_under_test", ROOT / "scripts" / "eval_research.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses.field() type resolution (from __future__ import annotations
    # turns every annotation into a string) looks the defining module up via
    # sys.modules[cls.__module__] -- a module loaded via exec_module() alone
    # is never registered there, so CacheStats/RecordedCall's own
    # @dataclass decorator raises AttributeError on a None module. Same
    # reason test_eval_compose_rescore.py never hit this: eval_compose.py
    # has no dataclass in it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- HTTP cache


class _FakeBackend:
    """A minimal real-ish GET backend: returns canned bytes for a URL
    fragment, and counts every call -- what a CachingHTTP MISS actually
    calls through to."""

    def __init__(self, module, pages: dict[str, tuple[int, bytes]]) -> None:
        self._Response = module._ReplayResponse
        self.pages = pages
        self.calls: list[str] = []

    async def get(self, url: str, params=None, **kw):
        self.calls.append(url)
        for fragment, (status, body) in self.pages.items():
            if fragment in url:
                return self._Response(status, body)
        return self._Response(404, b"")


class _ExplodingBackend:
    """If CachingHTTP ever calls through to this on a cache hit, the harness
    is silently re-fetching -- exactly what AC1 says makes the whole thing
    worthless. Raising immediately turns that into a loud test failure
    instead of a quiet, undetected re-fetch."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, *a, **kw):
        self.calls += 1
        raise AssertionError("network hit on a run that should be fully cached")


async def test_caching_http_never_touches_a_raising_backend_on_a_cache_hit(
    tmp_path, eval_research,
):
    """The core AC1 mechanism, isolated from research()/the model entirely."""
    stats1 = eval_research.CacheStats()
    backend = _FakeBackend(eval_research, {"/search": (200, b'{"results": []}')})
    http1 = eval_research.CachingHTTP(backend, tmp_path, stats1)

    body1 = await http1.get("https://searx.example/search", params={"q": "widget"})
    assert body1.status_code == 200
    assert stats1.hits == 0 and stats1.misses == 1
    assert len(backend.calls) == 1

    # A second, independent CachingHTTP instance against the SAME cache dir,
    # wrapping a backend that raises on any call at all.
    stats2 = eval_research.CacheStats()
    exploding = _ExplodingBackend()
    http2 = eval_research.CachingHTTP(exploding, tmp_path, stats2)

    body2 = await http2.get("https://searx.example/search", params={"q": "widget"})

    assert exploding.calls == 0, "the exploding backend must never be reached on a hit"
    assert stats2.hits == 1 and stats2.misses == 0
    assert body2.content == body1.content, "byte-identical corpus on replay"
    assert body2.status_code == body1.status_code


async def test_caching_http_distinguishes_by_url_and_params(tmp_path, eval_research):
    """A key bug that only hashed the url (ignoring params) would collide two
    different search queries against the same SearXNG endpoint into one
    cache entry -- silently serving query B's results for query A."""
    stats = eval_research.CacheStats()
    backend = _FakeBackend(eval_research, {})

    async def get(url, params=None, **kw):
        backend.calls.append(url)
        q = (params or {}).get("q", "")
        return eval_research._ReplayResponse(200, f'{{"q": "{q}"}}'.encode())

    backend.get = get
    http = eval_research.CachingHTTP(backend, tmp_path, stats)

    a = await http.get("https://searx.example/search", params={"q": "alpha"})
    b = await http.get("https://searx.example/search", params={"q": "beta"})

    assert a.content != b.content
    assert stats.misses == 2 and stats.hits == 0

    # Re-requesting "alpha" must hit its own entry, not beta's.
    a_again = await http.get("https://searx.example/search", params={"q": "alpha"})
    assert a_again.content == a.content
    assert stats.hits == 1 and stats.misses == 2


async def test_caching_http_post_is_refused_not_silently_uncached(tmp_path, eval_research):
    stats = eval_research.CacheStats()
    http = eval_research.CachingHTTP(_FakeBackend(eval_research, {}), tmp_path, stats)
    with pytest.raises(NotImplementedError):
        await http.post("https://x.example")


async def test_caching_http_never_writes_a_non_2xx_response_to_disk(tmp_path, eval_research):
    """B2 (T73 bounce 1): a 403 bot-block / Cloudflare interstitial /
    rate-limit used to be cached exactly like a real page (only a RAISED
    exception skipped the write) -- an audit of a shipped cache found ~19%
    of entries were an infrastructure failure permanently replayed as a
    "hit" while cache_misses read 0. A non-2xx response must never be
    written, so a second run re-attempts it as a fresh miss, and
    CacheStats.non_2xx must count it so it is visible in the summary."""
    stats1 = eval_research.CacheStats()
    backend = _FakeBackend(eval_research, {"/blocked": (403, b"<html>Forbidden</html>")})
    http1 = eval_research.CachingHTTP(backend, tmp_path, stats1)

    resp1 = await http1.get("https://provider.example/blocked")
    assert resp1.status_code == 403
    assert stats1.misses == 1 and stats1.non_2xx == 1
    assert len(backend.calls) == 1

    # Nothing was written for this (url, params) -- a second, independent
    # CachingHTTP against the SAME cache dir must be a MISS again, not a
    # replayed 403. Route it to a backend that now succeeds, simulating the
    # infrastructure failure clearing up.
    stats2 = eval_research.CacheStats()
    backend2 = _FakeBackend(eval_research, {"/blocked": (200, b"real content")})
    http2 = eval_research.CachingHTTP(backend2, tmp_path, stats2)

    resp2 = await http2.get("https://provider.example/blocked")
    assert resp2.status_code == 200
    assert stats2.hits == 0 and stats2.misses == 1, (
        "a non-2xx response must never be cached -- a permanent 403 in the "
        "corpus would otherwise silently pass forever as a cache hit"
    )
    assert len(backend2.calls) == 1, "the second run must actually re-fetch, not replay a cached 403"

    # And NOW that a 2xx landed, it must be cached normally.
    stats3 = eval_research.CacheStats()
    http3 = eval_research.CachingHTTP(_ExplodingBackend(), tmp_path, stats3)
    resp3 = await http3.get("https://provider.example/blocked")
    assert resp3.status_code == 200 and resp3.content == b"real content"
    assert stats3.hits == 1 and stats3.misses == 0


async def test_caching_http_boundary_status_codes_199_200_299_300(tmp_path, eval_research):
    """Exact boundary, not a loose 'truthy status' check -- 199 and 300 must
    NOT be cached (a mutant widening the range to `<= 300` would pass every
    other test here but fail this one), 200 and 299 must be."""
    for code, should_cache in [(199, False), (200, True), (299, True), (300, False)]:
        stats = eval_research.CacheStats()
        backend = _FakeBackend(eval_research, {"/x": (code, b"body")})
        cache_dir = tmp_path / str(code)
        http = eval_research.CachingHTTP(backend, cache_dir, stats)
        await http.get(f"https://boundary.example/x?code={code}")

        cached_files = list(cache_dir.glob("http-*.json"))
        if should_cache:
            assert len(cached_files) == 1, f"status {code} must be cached"
            assert stats.non_2xx == 0
        else:
            assert len(cached_files) == 0, f"status {code} must NOT be cached"
            assert stats.non_2xx == 1


# ------------------------------------------- challenge-page screening (T130)
#
# T73's status-code screen is 2xx-only, and D86 audited a post-fix corpus
# "51/51 HTTP 200, 0 non-2xx" and called it genuinely clean. But the
# SMALLEST file in that "clean" corpus (7.9KB) was an Anubis JS anti-bot
# CHALLENGE page, titled "Making sure you're not a bot!" -- invisible to the
# status-code screen (it IS a 200) and to a naive "captcha"/"cloudflare"
# keyword scan (it uses neither word). These tests prove the two-signal
# screen (title + visible-text-length, both required) catches known
# challenge shapes without flagging real content -- the short legitimate
# article case is the one that matters, because a length-only screen would
# have false-positived it.

ANUBIS_STYLE_CHALLENGE = """<!DOCTYPE html>
<html><head><title>Making sure you're not a bot!</title>
<script>
// A real Anubis page ships a sizeable proof-of-work challenge script here.
(function(){for(var i=0;i<50;i++){/* padding to look like a real PoW bundle */}})();
</script>
</head>
<body>
<noscript>Enable JavaScript and cookies to continue.</noscript>
<div id="spinner">Checking your connection before proceeding...</div>
</body></html>"""

CLOUDFLARE_STYLE_CHALLENGE = """<html><head><title>Just a moment...</title></head>
<body>
<h1>Checking your browser before accessing example.com.</h1>
<div>This process is automatic. Your browser will redirect shortly.</div>
</body></html>"""

SHORT_LEGITIMATE_ARTICLE = """<html><head>
<title>Local Startup Raises $5M to Build Widget Factory</title>
</head><body><article>
<p>A local startup announced today it raised $5 million in seed funding to
build a new widget factory on the edge of town.</p>
<p>The company said the funds will go toward hiring engineers and building
its first production line, with shipments expected by next year.</p>
</article></body></html>"""

LONG_ARTICLE_MENTIONING_A_CHALLENGE_VENDOR = """<html><head>
<title>How Publishers Are Fighting AI Scrapers</title>
</head><body><article>
""" + ("<p>Many sites have started deploying Cloudflare's bot-management "
       "tools, and some smaller publishers use a captcha or a JavaScript "
       "proof-of-work challenge from projects like Anubis to slow down "
       "scrapers. </p>" * 15) + """
</article></body></html>"""


def test_is_challenge_page_flags_anubis_style_challenge(eval_research):
    assert eval_research.is_challenge_page(ANUBIS_STYLE_CHALLENGE.encode()) is True


def test_is_challenge_page_flags_cloudflare_style_challenge(eval_research):
    assert eval_research.is_challenge_page(CLOUDFLARE_STYLE_CHALLENGE.encode()) is True


def test_is_challenge_page_does_not_flag_a_short_legitimate_article(eval_research):
    """The failure mode that matters: a naive "is the body short?" screen
    would flag this too -- it is comfortably under
    ``_MAX_CHALLENGE_VISIBLE_TEXT_CHARS``. It must survive because its
    <title> is the story's own headline, not a challenge phrase."""
    assert len(SHORT_LEGITIMATE_ARTICLE) < eval_research._MAX_CHALLENGE_VISIBLE_TEXT_CHARS, (
        "sanity: this fixture must actually be short, or the test below "
        "proves nothing about the title gate specifically"
    )
    assert eval_research.is_challenge_page(SHORT_LEGITIMATE_ARTICLE.encode()) is False


def test_is_challenge_page_does_not_flag_a_long_article_about_challenge_vendors(eval_research):
    """A real article that discusses Cloudflare/captcha/Anubis BY NAME in
    its own reporting must not be flagged -- the screen reads the <title>,
    never body prose, and this article has plenty of real text."""
    assert eval_research.is_challenge_page(
        LONG_ARTICLE_MENTIONING_A_CHALLENGE_VENDOR.encode()
    ) is False


def test_is_challenge_page_requires_both_signals_not_title_alone(eval_research):
    """A page titled like a challenge but carrying a full article's worth of
    real text (e.g. a blog post literally called "Just a moment...") must
    not be flagged on the title alone -- both signals are required."""
    long_body_bad_title = (
        "<html><head><title>Just a moment...</title></head><body><article>"
        + ("<p>This is a real blog post about patience and waiting. </p>" * 60)
        + "</article></body></html>"
    )
    assert eval_research.is_challenge_page(long_body_bad_title.encode()) is False


def test_is_challenge_page_handles_a_missing_title_tag(eval_research):
    """Defensive: no <title> at all must never raise, and must not match."""
    assert eval_research.is_challenge_page(b"<html><body>no title here</body></html>") is False


def test_is_challenge_page_visible_text_length_boundary(eval_research):
    """Exact boundary on the length signal, given a matching title -- a
    mutant widening ``<`` to ``<=`` would pass every other test here but
    flip this one, the same shape as the status-code boundary test above."""
    limit = eval_research._MAX_CHALLENGE_VISIBLE_TEXT_CHARS

    def page(text_len: int) -> bytes:
        body = "x" * text_len
        return f"<html><head><title>Just a moment...</title></head><body>{body}</body></html>".encode()

    assert eval_research.is_challenge_page(page(limit - 1)) is True
    assert eval_research.is_challenge_page(page(limit)) is False


async def test_caching_http_screens_out_a_challenge_page(tmp_path, eval_research):
    """A challenge page must never be written to disk -- it gets the exact
    same treatment as a non-2xx (T73's own precedent): counted on its own
    stat, not cached, so a later run gets a fresh chance at real content."""
    stats = eval_research.CacheStats()
    backend = _FakeBackend(
        eval_research, {"/blocked": (200, ANUBIS_STYLE_CHALLENGE.encode())}
    )
    http = eval_research.CachingHTTP(backend, tmp_path, stats)

    resp = await http.get("https://provider.example/blocked")

    assert resp.status_code == 200
    assert stats.misses == 1 and stats.challenge_pages == 1
    assert stats.non_2xx == 0, "a challenge page is a 2xx, not a non-2xx"
    assert list(tmp_path.glob("http-*.json")) == [], (
        "a challenge page must never be written to the corpus cache"
    )


async def test_caching_http_still_caches_a_short_legitimate_article(tmp_path, eval_research):
    """The screen must not become a NEW false-positive source that starves
    the corpus of real short articles."""
    stats = eval_research.CacheStats()
    backend = _FakeBackend(
        eval_research, {"/story": (200, SHORT_LEGITIMATE_ARTICLE.encode())}
    )
    http = eval_research.CachingHTTP(backend, tmp_path, stats)

    resp = await http.get("https://news.example/story")

    assert resp.status_code == 200
    assert stats.challenge_pages == 0
    assert len(list(tmp_path.glob("http-*.json"))) == 1, (
        "a real short article must still be cached normally"
    )


def test_summarise_reports_challenge_pages(eval_research):
    stats = eval_research.CacheStats(hits=5, misses=2, challenge_pages=4)
    summary = eval_research.summarise([], stats, "fp", 1)
    assert summary["challenge_pages"] == 4


# --------------------------------------------------------- schema classifier


def test_call_kind_classifies_each_real_research_schema(eval_research):
    """Derived from the REAL schema objects research.py/coverage.py define
    (D21: derive, don't restate) -- not a hand-copied dict that could drift
    from what the pipeline actually sends."""
    from pipeline.coverage import COVERAGE_SCHEMA
    from pipeline.stages.research import NOTE_SCHEMA, PLAN_SCHEMA, RELEVANCE_SCHEMA

    def kind(schema):
        return eval_research._call_kind(
            eval_research.RecordedCall("cheap", schema, "u", {})
        )

    assert kind(PLAN_SCHEMA) == "plan"
    assert kind(RELEVANCE_SCHEMA) == "relevance"
    assert kind(NOTE_SCHEMA) == "note"
    assert kind(COVERAGE_SCHEMA) == "coverage"
    assert kind(None) == "other"
    assert kind({"properties": {"something_else": {}}}) == "other"


def test_failure_phrase_and_corpus_marker_literals_still_match_research_py(eval_research):
    """Pins the two literals this file depends on but does not import
    (research.py's exact "WEB EXCERPTS:\\n\\n" heading, and the wording its
    own tests show a null-result note actually uses) -- so a rename in
    research.py fails this test loudly instead of silently zeroing every
    grounding/code_preserved score in eval_research.py.
    """
    from pipeline.stages import research as research_mod

    assert '"WEB EXCERPTS:\\n\\n"' in Path(research_mod.__file__).read_text() \
        or "WEB EXCERPTS:\\n\\n" in Path(research_mod.__file__).read_text()
    assert eval_research.FAILURE_PHRASE_RE.search(
        "The provided web excerpts do not contain information."
    )
    assert eval_research.FAILURE_PHRASE_RE.search(
        "The excerpts do not contain information about signs."
    )
    assert not eval_research.FAILURE_PHRASE_RE.search(
        "OpenAI blocked Cursor from its models within three months."
    )


def test_failure_phrase_regex_requires_negation_not_just_the_word_excerpts(eval_research):
    """A genuine finding that happens to use the word "excerpts" positively
    must not be flagged as a non-answer -- the regex requires "do/does NOT",
    not merely proximity to "excerpts"."""
    assert not eval_research.FAILURE_PHRASE_RE.search(
        "The excerpts contain detailed pricing information for the new plan."
    )


# --------------------------------------------------------------- fingerprint


def test_fingerprint_changes_with_model_and_temperature_but_not_otherwise(eval_research):
    from pipeline.config import Settings

    settings = Settings.load(env={})

    a = eval_research.prompt_fingerprint(settings, model="model-a", temperature=0.3)
    a_again = eval_research.prompt_fingerprint(settings, model="model-a", temperature=0.3)
    assert a == a_again, "identical config must fingerprint identically"

    b_model = eval_research.prompt_fingerprint(settings, model="model-b", temperature=0.3)
    assert b_model != a, "a different model slug must change the fingerprint"

    b_temp = eval_research.prompt_fingerprint(settings, model="model-a", temperature=0.9)
    assert b_temp != a, "a different temperature must change the fingerprint"

    assert b_model != b_temp


def test_fingerprint_defaults_to_the_configured_provider_model(eval_research):
    from pipeline.config import Settings

    ollama = Settings.load(env={})
    assert "qwen3:4b-instruct" in eval_research.prompt_fingerprint(ollama)

    openrouter = Settings.load(env={
        "LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-test",
    })
    assert eval_research._effective_model(openrouter) in eval_research.prompt_fingerprint(openrouter)


def test_fingerprint_differs_between_a_cheap_routed_and_a_good_routed_run(eval_research):
    """B3 (T73 bounce 1): a fingerprint that only hashed `cheap`'s model
    could not see the exact thing T71 ("route the research note to `good`")
    changes -- a baseline arm (note-writing on `cheap`) and a T71 arm (same
    note-writing prompt, now on `good`) would share a fingerprint even
    though a different model actually served the call, so `--compare`
    would silently pair two non-comparable runs. Simulated here as two
    configs differing ONLY in which model each role points at -- the
    fingerprint must differ, and must differ specifically because of the
    `good` side, not just because `cheap` also happened to move."""
    from pipeline.config import Settings

    baseline = Settings.load(env={"MODEL_CHEAP": "shared-cheap", "MODEL_GOOD": "shared-good"})
    good_routed = Settings.load(env={"MODEL_CHEAP": "shared-cheap", "MODEL_GOOD": "different-good"})

    fp_baseline = eval_research.prompt_fingerprint(baseline)
    fp_good_routed = eval_research.prompt_fingerprint(good_routed)

    assert fp_baseline != fp_good_routed, (
        "changing only the `good` model must move the fingerprint -- this is "
        "exactly the config a `good`-routed note call would depend on"
    )
    assert "shared-good" in fp_baseline
    assert "different-good" in fp_good_routed
    # And the cheap side is untouched between the two configs, proving the
    # difference really came from `good`, not incidental drift elsewhere.
    assert eval_research._effective_model(baseline, role="cheap") == \
        eval_research._effective_model(good_routed, role="cheap")


# ----------------------------------------------------------- Recorder / plan


async def test_recorder_applies_temperature_override_to_every_cheap_call(eval_research):
    class _TempSpy:
        def __init__(self):
            self.temperatures = []

        async def cheap(self, system, user, schema=None, temperature=0.3, **kw):
            self.temperatures.append(temperature)
            return {"ok": True}

    spy = _TempSpy()
    recorder = eval_research.Recorder(spy, temperature=0.9)
    await recorder.cheap("sys", "usr", schema={"properties": {}})
    await recorder.cheap("sys", "usr", schema={"properties": {}})
    assert spy.temperatures == [0.9, 0.9]


async def test_recorder_without_override_leaves_the_inner_default_alone(eval_research):
    class _TempSpy:
        def __init__(self):
            self.temperatures = []

        async def cheap(self, system, user, schema=None, temperature=0.3, **kw):
            self.temperatures.append(temperature)
            return {"ok": True}

    spy = _TempSpy()
    recorder = eval_research.Recorder(spy)
    await recorder.cheap("sys", "usr", schema={"properties": {}})
    assert spy.temperatures == [0.3]


async def test_recorder_logs_a_raised_exception_rather_than_swallowing_it(eval_research):
    class _Blows:
        async def cheap(self, system, user, schema=None, **kw):
            raise RuntimeError("boom")

    recorder = eval_research.Recorder(_Blows())
    with pytest.raises(RuntimeError):
        await recorder.cheap("sys", "usr", schema={"properties": {"relevant": {}}})
    assert len(recorder.calls) == 1
    assert isinstance(recorder.calls[0].response, RuntimeError)


async def test_recorder_freezes_the_plan_call_only(tmp_path, eval_research):
    """The plan call must be served from disk on a second Recorder against
    the same plan_cache_dir; a relevance-shaped call with the same schema
    property overlap risk must never be mistaken for one.

    T73 bounce 2 nit: plan-cache hits/misses are tracked on their OWN
    CacheStats fields (plan_hits/plan_misses), separate from the HTTP
    cache's hits/misses -- asserted here on both sides of that split, so a
    regression pooling them back together goes red either way."""
    calls = {"n": 0}

    class _CountingLLM:
        async def cheap(self, system, user, schema=None, **kw):
            calls["n"] += 1
            return {"entity": "E", "entity_context": "ctx",
                    "queries": [f"q{calls['n']}"], "image_query": "img", "clauses": []}

    plan_schema = {"properties": {"entity": {}, "queries": {}}}
    stats = eval_research.CacheStats()

    r1 = eval_research.Recorder(_CountingLLM(), plan_cache_dir=tmp_path, stats=stats)
    first = await r1.cheap("sys", "usr", schema=plan_schema)
    assert stats.plan_misses == 1 and stats.plan_hits == 0
    assert stats.hits == 0 and stats.misses == 0, "the HTTP-only counters must stay untouched"
    assert calls["n"] == 1

    r2 = eval_research.Recorder(_CountingLLM(), plan_cache_dir=tmp_path, stats=stats)
    second = await r2.cheap("sys", "usr", schema=plan_schema)
    assert stats.plan_misses == 1 and stats.plan_hits == 1, "the second plan call must be a cache hit"
    assert stats.hits == 0 and stats.misses == 0
    assert calls["n"] == 1, "the inner LLM must not have been called again"
    assert second == first


async def test_recorder_plan_cache_is_keyed_by_request_content_not_a_caller_id(
    tmp_path, eval_research,
):
    """Reviewer nit (T73 bounce 1): the old key was (item.id, fingerprint),
    which could not see a change in the item's OWN content
    (_item_context(item) -- research.py) if its id stayed the same. Keying
    by sha256(system+user) instead means: same request text -> same cache
    entry (content-addressed, no id needed at all), different request text
    -> a genuinely different entry, even with everything else equal."""
    calls = {"n": 0}

    class _CountingLLM:
        async def cheap(self, system, user, schema=None, **kw):
            calls["n"] += 1
            return {"entity": "E", "entity_context": "ctx", "queries": [f"q{calls['n']}"],
                    "image_query": "img", "clauses": []}

    plan_schema = {"properties": {"entity": {}, "queries": {}}}
    stats = eval_research.CacheStats()

    r1 = eval_research.Recorder(_CountingLLM(), plan_cache_dir=tmp_path, stats=stats)
    await r1.cheap("sys", "NEWS ITEM:\nOriginal content", schema=plan_schema)
    assert calls["n"] == 1

    # Same exact request text again (as if it were the SAME item.id) -- must hit.
    r2 = eval_research.Recorder(_CountingLLM(), plan_cache_dir=tmp_path, stats=stats)
    await r2.cheap("sys", "NEWS ITEM:\nOriginal content", schema=plan_schema)
    assert calls["n"] == 1, "identical request content must be a cache hit"

    # The item's content changed (e.g. research_golden.json edited in place,
    # id unchanged) -- the OLD key ((item.id, fingerprint)) would have
    # silently replayed the stale plan here; the new key must not.
    r3 = eval_research.Recorder(_CountingLLM(), plan_cache_dir=tmp_path, stats=stats)
    await r3.cheap("sys", "NEWS ITEM:\nEdited content, same item id", schema=plan_schema)
    assert calls["n"] == 2, "different request content must be a fresh call, not a stale replay"


async def test_recorder_plan_cache_disabled_forces_every_call_live(tmp_path, eval_research):
    """--no-plan-cache (reviewer nit, for a future planner task): passing
    plan_cache_dir=None must never cache or replay a plan call, however
    many times the identical request is made."""
    calls = {"n": 0}

    class _CountingLLM:
        async def cheap(self, system, user, schema=None, **kw):
            calls["n"] += 1
            return {"entity": "E", "entity_context": "ctx", "queries": ["q"],
                    "image_query": "img", "clauses": []}

    plan_schema = {"properties": {"entity": {}, "queries": {}}}
    stats = eval_research.CacheStats()
    recorder = eval_research.Recorder(_CountingLLM(), plan_cache_dir=None, stats=stats)

    await recorder.cheap("sys", "usr", schema=plan_schema)
    await recorder.cheap("sys", "usr", schema=plan_schema)

    assert calls["n"] == 2, "with no plan_cache_dir, every plan call must go live"
    assert stats.plan_hits == 0


# ---------------------------------------------------------- F4: plan cache dir


def test_plan_cache_dir_is_namespaced_by_fingerprint(eval_research):
    """F4 (T73 bounce 2 mutation survivor): the reviewer's own repro --
    dropping the fingerprint from this path -- left 46/46 tests green
    because nothing exercised _score_all_items's path construction
    directly. Two different fingerprints must get two different plan-cache
    directories, so a baseline config and an arm config (even one sharing
    the exact same plan request text) can never share a cached plan."""
    a = eval_research._plan_cache_dir_for("fp-baseline", True)
    b = eval_research._plan_cache_dir_for("fp-arm", True)

    assert a != b
    assert "fp-baseline" in str(a)
    assert "fp-arm" in str(b)
    assert a.parent == b.parent == eval_research.CACHE_DIR


def test_plan_cache_dir_is_none_when_disabled(eval_research):
    """--no-plan-cache must return None regardless of fingerprint -- the
    caller's signal to force every plan call live."""
    assert eval_research._plan_cache_dir_for("fp-x", False) is None
    assert eval_research._plan_cache_dir_for("fp-y", False) is None


async def test_recorder_records_a_good_routed_call_post_t71_simulation(eval_research):
    """B1 (T73 bounce 1), the harness's own blocking bug: ``Recorder`` wrapped
    ``cheap`` only, so ``llm.good`` fell through ``__getattr__`` to the raw
    inner client -- unrecorded, no exception. T71 ("route the research note
    to `good`") is the exact task this harness exists to gate, so this
    simulates that world directly against the REAL ``NOTE_SCHEMA``: a plan
    call on `cheap` (today's role, unchanged) followed by a note call on
    `good` (T71's change), then feeds the recorded calls through the real
    ``score_item`` and asserts the note is counted -- not silently zeroed.
    """
    from pipeline.stages.research import NOTE_SCHEMA

    class _PostT71LLM:
        """Stands in for a `LLMClient` after a hypothetical T71 lands:
        `cheap` still serves the plan, `good` now serves note-writing."""

        async def cheap(self, system, user, schema=None, **kw):
            return {"entity": "E", "entity_context": "ctx", "queries": ["q1"],
                    "image_query": "img", "clauses": []}

        async def good(self, system, user, schema=None, **kw):
            assert schema == NOTE_SCHEMA, "must be called with the real research.py schema"
            return {"claim": "Widget shipped version 4.2",
                    "detail": "with 92 percent uptime", "confidence": "high"}

    recorder = eval_research.Recorder(_PostT71LLM())
    await recorder.cheap("plan-sys", "plan-usr", schema={"properties": {"entity": {}, "queries": {}}})
    await recorder.good(
        "note-sys", NOTE_CALL_TEMPLATE.format(corpus="widget shipped version 4.2 with 92 percent uptime"),
        schema=NOTE_SCHEMA,
    )

    assert len(recorder.calls) == 2, (
        "both the plan call AND the good-routed note call must be recorded -- "
        "pre-fix, only the plan call would show up here"
    )
    note_calls = [c for c in recorder.calls if eval_research._call_kind(c) == "note"]
    assert len(note_calls) == 1, "the good-routed call must classify as a note by schema shape"
    assert note_calls[0].role == "good", "RecordedCall.role must say which role actually served it"

    class _Item:
        id = 501

    row = eval_research.score_item(_Item(), recorder.calls)
    assert row["notes_produced"] == 1, (
        "pre-fix this was 0 -- the exact silent-zero B1 describes, with no exception raised"
    )
    assert row["yield"] == 1.0
    assert row["avg_grounding_ratio"] is not None and row["avg_grounding_ratio"] > 0


# ------------------------------------------------------------- note scoring


NOTE_CALL_TEMPLATE = (
    "ORIGINAL NEWS ITEM:\nsome item\n\nRESEARCH QUESTION: q\n\nWEB EXCERPTS:\n\n{corpus}"
)


def test_score_note_uses_pipeline_quality_directly_for_grounding_and_density(eval_research):
    """Proves delegation, not a re-derivation: the wrapper's numbers must
    equal pipeline.quality's own functions called on the same pseudo-doc,
    which is the only thing `_score_note` is supposed to do."""
    from pipeline.quality import grounding_ratio, numeric_density

    note = {"claim": "Widget shipped version 4.2", "detail": "with 92 percent uptime",
            "confidence": "high"}
    corpus = "The widget shipped version 4.2 today with 92 percent uptime reported."
    call = eval_research.RecordedCall(
        "cheap", {"properties": {"claim": {}, "confidence": {}}},
        NOTE_CALL_TEMPLATE.format(corpus=corpus), note,
    )

    scored = eval_research._score_note(call)

    expected_doc = eval_research._note_prose_doc(note)
    assert scored["numeric_density"] == numeric_density(expected_doc)
    assert scored["grounding_ratio"] == grounding_ratio(expected_doc, corpus)
    assert scored["grounding_ratio"] > 0, "a note quoting the corpus must show real grounding"


def test_note_prose_doc_maps_claim_to_headline_and_detail_to_sub(eval_research):
    """F3 (T73 bounce 2 mutation survivor): deleting `"sub":
    note.get("detail", "")` from `_note_prose_doc` -- halving the text
    EVERY note metric reads -- left 46/46 green, because the delegation
    test above builds its "expected" doc by calling `_note_prose_doc`
    itself: both sides of that comparison move together under the exact
    same mutation, so it proves delegation but not correctness of the
    mapping being delegated TO.

    This test is independent of `_score_note` by construction: it derives
    the field CONTRACT from `pipeline.quality._slide_text` (house rule 3 --
    derive the owner's contract, don't restate it) and proves BOTH claim
    and detail actually surface as fragments through the real reader.
    Deleting the "sub" mapping drops `detail` from the fragments and this
    goes red on its own, with no dependence on `_score_note`."""
    from pipeline.quality import _slide_text

    note = {"claim": "Widget shipped version 4.2", "detail": "with 92 percent uptime",
            "confidence": "high"}
    doc = eval_research._note_prose_doc(note)
    slide = doc["slides"][0]

    fragments = _slide_text(slide)
    assert note["claim"] in fragments, "claim must surface via quality.py's own field reader"
    assert note["detail"] in fragments, "detail must surface via quality.py's own field reader"


def test_score_note_detects_a_failure_phrase_note(eval_research):
    note = {"claim": "The provided web excerpts do not contain information.",
            "detail": "", "confidence": "low"}
    call = eval_research.RecordedCall(
        "cheap", {"properties": {"claim": {}, "confidence": {}}},
        NOTE_CALL_TEMPLATE.format(corpus="unrelated corpus text"), note,
    )
    assert eval_research._score_note(call)["is_failure_phrase"] is True


def test_score_note_code_preserved_true_when_a_fenced_block_survives(eval_research):
    corpus = "Some prose.\n```yaml\ntype: concept\ntitle: Onboarding\n```\nmore prose."
    note = {"claim": "c", "detail": "```yaml\ntype: concept\ntitle: Onboarding\n```",
            "confidence": "high"}
    call = eval_research.RecordedCall(
        "cheap", {"properties": {"claim": {}, "confidence": {}}},
        NOTE_CALL_TEMPLATE.format(corpus=corpus), note,
    )
    assert eval_research._score_note(call)["code_preserved"] is True


def test_score_note_code_preserved_none_when_corpus_has_no_fence(eval_research):
    note = {"claim": "c", "detail": "d", "confidence": "high"}
    call = eval_research.RecordedCall(
        "cheap", {"properties": {"claim": {}, "confidence": {}}},
        NOTE_CALL_TEMPLATE.format(corpus="plain prose, no code at all"), note,
    )
    assert eval_research._score_note(call)["code_preserved"] is None


# ------------------------------------------------------------------ score_item


def test_score_item_yield_gate_rate_and_confidence_mix(eval_research):
    """The money test for mutation resistance: exact arithmetic, not a
    shape/'>0' check, so an off-by-one in any denominator goes red."""
    plan_schema = {"properties": {"entity": {}, "queries": {}}}
    relevance_schema = {"properties": {"relevant": {}, "why": {}}}
    note_schema = {"properties": {"claim": {}, "detail": {}, "confidence": {}}}

    calls = [
        eval_research.RecordedCall("cheap", plan_schema, "plan-input",
                                    {"queries": ["q1", "q2", "q3", "q4"]}),
        # q1: rejected outright, no note.
        eval_research.RecordedCall("cheap", relevance_schema, "u", {"relevant": False, "why": "no"}),
        # q2: one relevant source, high-confidence real finding.
        eval_research.RecordedCall("cheap", relevance_schema, "u", {"relevant": True, "why": "yes"}),
        eval_research.RecordedCall(
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus="widget shipped in Q3 with 42 percent growth"),
            {"claim": "Widget shipped in Q3", "detail": "42 percent growth", "confidence": "high"},
        ),
        # q3: relevant source but a failure-phrase, low-confidence note.
        eval_research.RecordedCall("cheap", relevance_schema, "u", {"relevant": True, "why": "yes"}),
        eval_research.RecordedCall(
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus="unrelated corpus"),
            {"claim": "The excerpts do not contain information about this.",
             "detail": "", "confidence": "low"},
        ),
        # q4: relevant source, medium-confidence note.
        eval_research.RecordedCall("cheap", relevance_schema, "u", {"relevant": True, "why": "yes"}),
        eval_research.RecordedCall(
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus="widget medium finding here"),
            {"claim": "Widget medium finding", "detail": "here", "confidence": "medium"},
        ),
    ]

    class _Item:
        id = 42

    row = eval_research.score_item(_Item(), calls)

    assert row["queries_planned"] == 4
    assert row["notes_produced"] == 3
    assert row["yield"] == 0.75
    assert row["gate_checked"] == 4
    assert row["gate_accepted"] == 3
    assert row["gate_accept_rate"] == 0.75
    assert row["confidence_mix"] == {"high": 1, "low": 1, "medium": 1}
    assert row["failure_phrase_count"] == 1
    assert row["failure_phrase_rate"] == round(1 / 3, 4)


def test_score_item_handles_no_plan_call_without_crashing(eval_research):
    """research() can fail before the plan even resolves (e.g. a malformed
    plan response raising before disambiguate() runs); score_item must
    degrade to zeros, not raise."""

    class _Item:
        id = 1

    row = eval_research.score_item(_Item(), [])
    assert row["queries_planned"] == 0
    assert row["notes_produced"] == 0
    assert row["yield"] is None
    assert row["gate_accept_rate"] is None
    assert row["failure_phrase_rate"] is None
    assert row["code_preserved_rate"] == "0/0 (no fenced block in any corpus)"
    assert row["code_preserved_true"] == 0
    assert row["code_preserved_applicable"] == 0


def test_score_item_reports_code_preserved_true_and_applicable_counts(eval_research):
    """Reviewer nit (T73 bounce 1): code_preserved_rate was a display
    string only -- summarise() needs raw counts to aggregate across rows
    without re-parsing "N/M" text. One note preserves a fenced block, one
    doesn't, one has no fence to preserve at all (excluded, per
    code_preserved's own None-means-not-applicable contract)."""
    note_schema = {"properties": {"claim": {}, "detail": {}, "confidence": {}}}
    fenced_corpus = "prose\n```yaml\ntype: concept\ntitle: Onboarding\n```\nmore prose"

    calls = [
        eval_research.RecordedCall(  # preserved
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus=fenced_corpus),
            {"claim": "c", "detail": "```yaml\ntype: concept\ntitle: Onboarding\n```",
             "confidence": "high"},
        ),
        eval_research.RecordedCall(  # NOT preserved -- unrelated detail
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus=fenced_corpus),
            {"claim": "c", "detail": "completely unrelated prose", "confidence": "high"},
        ),
        eval_research.RecordedCall(  # not applicable -- no fence in corpus
            "cheap", note_schema,
            NOTE_CALL_TEMPLATE.format(corpus="plain prose, no code at all"),
            {"claim": "c", "detail": "d", "confidence": "high"},
        ),
    ]

    class _Item:
        id = 7

    row = eval_research.score_item(_Item(), calls)
    assert row["code_preserved_true"] == 1
    assert row["code_preserved_applicable"] == 2
    assert row["code_preserved_rate"] == "1/2"


def test_score_item_reports_note_roles_from_recorded_calls(eval_research):
    """F1 (T73 bounce 2): a saved artifact must be able to tell a
    cheap-routed run from a good-routed run on disk. T71 ("route the note
    call to `good`") is a CODE change (research.py), not a Settings one --
    baseline and arm share a byte-identical prompt_fingerprint() by
    construction (D87), so the fingerprint alone cannot distinguish them.
    The row itself must."""
    note_schema = {"properties": {"claim": {}, "detail": {}, "confidence": {}}}
    calls = [
        eval_research.RecordedCall(
            "cheap", note_schema, NOTE_CALL_TEMPLATE.format(corpus="c1"),
            {"claim": "a", "detail": "b", "confidence": "high"}),
        eval_research.RecordedCall(
            "good", note_schema, NOTE_CALL_TEMPLATE.format(corpus="c2"),
            {"claim": "c", "detail": "d", "confidence": "high"}),
        eval_research.RecordedCall(
            "good", note_schema, NOTE_CALL_TEMPLATE.format(corpus="c3"),
            {"claim": "e", "detail": "f", "confidence": "high"}),
    ]

    class _Item:
        id = 1

    row = eval_research.score_item(_Item(), calls)
    assert row["note_roles"] == {"cheap": 1, "good": 2}


def test_note_roles_makes_a_post_t71_artifact_distinguishable_from_its_baseline(eval_research):
    """D87/F1's direct AC proof (T73 bounce 2): same claim/detail/
    confidence/corpus on both rows -- ONLY the serving role differs -- and
    that difference must survive into the saved row while everything else
    stays identical (proving note_roles is what carries the distinction,
    not an accidental side effect of something else changing)."""
    note_schema = {"properties": {"claim": {}, "detail": {}, "confidence": {}}}

    def _note_call(role):
        return eval_research.RecordedCall(
            role, note_schema, NOTE_CALL_TEMPLATE.format(corpus="widget shipped v4.2"),
            {"claim": "Widget shipped v4.2", "detail": "on schedule", "confidence": "high"},
        )

    class _Item:
        id = 501

    baseline_row = eval_research.score_item(_Item(), [_note_call("cheap")])
    arm_row = eval_research.score_item(_Item(), [_note_call("good")])

    for key in baseline_row:
        if key == "note_roles":
            continue
        assert baseline_row[key] == arm_row[key], f"{key} should be unaffected by role"

    assert baseline_row["note_roles"] != arm_row["note_roles"]
    assert baseline_row["note_roles"] == {"cheap": 1}
    assert arm_row["note_roles"] == {"good": 1}


# ------------------------------------------------------------------- summary


def test_summarise_aggregates_across_rows_and_reports_cache_stats(eval_research):
    rows = [
        {"queries_planned": 4, "notes_produced": 3, "gate_checked": 4, "gate_accepted": 3,
         "failure_phrase_count": 1, "confidence_mix": {"high": 1, "low": 1, "medium": 1},
         "avg_numeric_density": 2.0, "avg_proper_noun_density": 4.0, "avg_grounding_ratio": 0.5,
         "error": None},
        {"queries_planned": 2, "notes_produced": 2, "gate_checked": 2, "gate_accepted": 2,
         "failure_phrase_count": 0, "confidence_mix": {"high": 2},
         "avg_numeric_density": 4.0, "avg_proper_noun_density": None, "avg_grounding_ratio": 0.3,
         "error": "Retryable: only 1 of 4 researchers produced notes; need at least 2"},
    ]
    stats = eval_research.CacheStats(hits=10, misses=0)
    summary = eval_research.summarise(rows, stats, "fp123", 1)

    assert summary["items"] == 2
    assert summary["items_completed"] == 1
    assert summary["queries_planned_total"] == 6
    assert summary["notes_produced_total"] == 5
    assert summary["yield_overall"] == round(5 / 6, 4)
    assert summary["gate_accept_rate_overall"] == round(5 / 6, 4)
    assert summary["confidence_mix_overall"] == {"high": 3, "low": 1, "medium": 1}
    assert summary["failure_phrase_rate_overall"] == round(1 / 5, 4)
    assert summary["cache_hits"] == 10
    assert summary["cache_misses"] == 0
    assert summary["avg_numeric_density"] == 3.0
    # Rows above predate code_preserved_true/applicable (older-shaped rows,
    # or a hand-built test row like this one) -- must default to 0/0, not KeyError.
    assert summary["code_preserved_rate_overall"] == "0/0 (no fenced block in any corpus)"
    # Renamed from "set_version" (T73 bounce 2 nit) -- was easily confused
    # with pipeline.quality's unrelated "metric_set_version" (D15/D23).
    assert summary["golden_set_version"] == 1
    assert "set_version" not in summary
    # Rows above predate note_roles too -- must default to {}, not KeyError.
    assert summary["note_roles_overall"] == {}
    assert summary["harness"] == "research"


def test_summarise_aggregates_note_roles_across_rows(eval_research):
    """F1 (T73 bounce 2): rolls score_item's per-row note_roles up the same
    way confidence_mix_overall already does, so a saved run's summary
    reports the role split for the WHOLE golden set, not just per item."""
    rows = [
        {"queries_planned": 1, "notes_produced": 1, "gate_checked": 1, "gate_accepted": 1,
         "failure_phrase_count": 0, "confidence_mix": {"high": 1}, "note_roles": {"cheap": 1},
         "avg_numeric_density": None, "avg_proper_noun_density": None, "avg_grounding_ratio": None,
         "error": None},
        {"queries_planned": 1, "notes_produced": 1, "gate_checked": 1, "gate_accepted": 1,
         "failure_phrase_count": 0, "confidence_mix": {"high": 1},
         "note_roles": {"good": 2, "cheap": 1},
         "avg_numeric_density": None, "avg_proper_noun_density": None, "avg_grounding_ratio": None,
         "error": None},
    ]
    summary = eval_research.summarise(rows, eval_research.CacheStats(), "fp", 1)
    assert summary["note_roles_overall"] == {"cheap": 2, "good": 2}


def test_summarise_reports_plan_cache_hits_separately_from_http_cache(eval_research):
    """Nit (T73 bounce 2): cache_hits/cache_misses used to pool the HTTP
    page cache and the plan-call cache into one shared counter, so a reader
    could not tell "every page was a fresh fetch, only the plan replayed"
    from the reverse. Four distinct, pairwise-different values proves both
    pairs are tracked, and reported, independently."""
    stats = eval_research.CacheStats(hits=7, misses=2, plan_hits=3, plan_misses=1)
    summary = eval_research.summarise([], stats, "fp", 1)
    assert summary["cache_hits"] == 7
    assert summary["cache_misses"] == 2
    assert summary["plan_cache_hits"] == 3
    assert summary["plan_cache_misses"] == 1


def test_summarise_aggregates_code_preserved_across_rows(eval_research):
    """Reviewer nit (T73 bounce 1): code_preserved_rate lived on each row
    but never reached the overall summary. Two rows, non-trivial counts on
    both, to catch a mutant that only sums one row or drops one operand."""
    rows = [
        {"queries_planned": 1, "notes_produced": 1, "gate_checked": 1, "gate_accepted": 1,
         "failure_phrase_count": 0, "confidence_mix": {"high": 1},
         "avg_numeric_density": None, "avg_proper_noun_density": None, "avg_grounding_ratio": None,
         "code_preserved_true": 2, "code_preserved_applicable": 3, "error": None},
        {"queries_planned": 1, "notes_produced": 1, "gate_checked": 1, "gate_accepted": 1,
         "failure_phrase_count": 0, "confidence_mix": {"high": 1},
         "avg_numeric_density": None, "avg_proper_noun_density": None, "avg_grounding_ratio": None,
         "code_preserved_true": 1, "code_preserved_applicable": 2, "error": None},
    ]
    summary = eval_research.summarise(rows, eval_research.CacheStats(), "fp", 1)
    assert summary["code_preserved_rate_overall"] == "3/5"


def test_summarise_writes_the_d14_caveat_into_the_saved_summary(eval_research):
    """T73 bounce 2 nit: the D14 caveat lived only in the module docstring
    and run()'s own stdout -- absent from the durable saved artifact, so a
    reader loading data/eval/research-*.json later never saw it. Must be a
    real key on the summary dict (the thing that actually gets written to
    disk), not just printed once at run time."""
    summary = eval_research.summarise([], eval_research.CacheStats(), "fp", 1)
    assert summary["grounding_comparability_caveat"] == eval_research.GROUNDING_COMPARABILITY_CAVEAT
    assert "D14" in summary["grounding_comparability_caveat"]


def test_summarise_with_zero_notes_does_not_divide_by_zero(eval_research):
    rows = [{"queries_planned": 3, "notes_produced": 0, "gate_checked": 3, "gate_accepted": 0,
             "failure_phrase_count": 0, "confidence_mix": {}, "avg_numeric_density": None,
             "avg_proper_noun_density": None, "avg_grounding_ratio": None, "error": None}]
    summary = eval_research.summarise(rows, eval_research.CacheStats(), "fp", 1)
    assert summary["yield_overall"] == 0.0
    assert summary["failure_phrase_rate_overall"] is None


def test_summarise_with_no_rows_at_all_does_not_crash(eval_research):
    """--items filtered to an id absent from the golden set is a real,
    reachable empty-list path, not a hypothetical."""
    summary = eval_research.summarise([], eval_research.CacheStats(), "fp", 1)
    assert summary["items"] == 0
    assert summary["yield_overall"] is None
    assert summary["gate_accept_rate_overall"] is None
    assert summary["failure_phrase_rate_overall"] is None
    assert summary["confidence_mix_overall"] == {}


def test_summarise_reports_non_2xx_responses(eval_research):
    """B2's counter must actually reach the printed summary, not just live
    on CacheStats unused."""
    stats = eval_research.CacheStats(hits=5, misses=2, non_2xx=3)
    summary = eval_research.summarise([], stats, "fp", 1)
    assert summary["non_2xx_responses"] == 3


# --------------------------------------------------------------- --reps (B4)


def test_spread_reports_min_max_mean_stdev(eval_research):
    import statistics as stats_mod

    spread = eval_research._spread([1.0, 2.0, 3.0])
    assert spread == {
        "n": 3, "min": 1.0, "max": 3.0, "mean": 2.0,
        "stdev": round(stats_mod.stdev([1.0, 2.0, 3.0]), 4),
    }


def test_spread_with_a_single_value_has_zero_stdev_not_none(eval_research):
    """stdev of one sample is mathematically undefined -- 0.0 (no observed
    spread yet) is the right degenerate value to report, not a crash and
    not None (which would print as indistinguishable from "no data")."""
    assert eval_research._spread([0.5]) == {
        "n": 1, "min": 0.5, "max": 0.5, "mean": 0.5, "stdev": 0.0,
    }


def test_spread_ignores_none_values(eval_research):
    """A rep whose summary key is None (e.g. a set with zero notes) must not
    crash the spread or be silently coerced to 0 -- it is simply excluded,
    and `n` says so."""
    spread = eval_research._spread([0.2, None, 0.4, None])
    assert spread["n"] == 2
    assert spread["min"] == 0.2 and spread["max"] == 0.4


def test_spread_with_no_values_at_all(eval_research):
    assert eval_research._spread([]) == {
        "n": 0, "min": None, "max": None, "mean": None, "stdev": None,
    }


async def test_run_with_reps_scores_multiple_times_and_reports_a_noise_floor(
    eval_research, monkeypatch, tmp_path, capsys,
):
    """B4, end to end through run()'s own wiring: `--reps N` must actually
    invoke the scoring pass N times (not just label one pass "rep 1/3") and
    print a spread for every NOISE_FLOOR_KEYS metric -- not just the raw
    per-rep numbers, which a reader could eyeball into anything."""
    from pipeline.config import Settings

    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"set_version": 3, "cases": [
        {"failure_mode": "control",
         "item": {"id": 1, "source": "channel", "raw_text": "x", "extracted": {}}},
    ]}))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, env=None: Settings()))

    call_count = {"n": 0}
    # A different yield each rep -- proves the reported spread reflects real
    # variation across calls, not a constant repeated and mislabeled.
    canned_yields = [0.5, 1.0, 0.75]

    async def fake_score_all(items, reasons, settings, effective_temp, fingerprint, stats,
                              use_plan_cache=True):
        i = call_count["n"]
        call_count["n"] += 1
        return [{"id": items[0].id, "queries_planned": 4,
                  "notes_produced": round(4 * canned_yields[i]), "yield": canned_yields[i],
                  "gate_accepted": 3, "gate_checked": 4, "gate_accept_rate": 0.75,
                  "confidence_mix": {"high": 1}, "failure_phrase_count": 0,
                  "failure_phrase_rate": 0.0, "avg_numeric_density": 1.0,
                  "avg_proper_noun_density": 1.0, "avg_grounding_ratio": 0.5,
                  "code_preserved_rate": "0/0", "error": None, "seconds": 0.1,
                  "why": "control"}]

    monkeypatch.setattr(eval_research, "_score_all_items", fake_score_all)

    await eval_research.run(tag=None, reps=3)

    assert call_count["n"] == 3, "reps=3 must actually score three times, not once"
    out = capsys.readouterr().out
    assert "noise floor across 3 reps" in out
    for key in eval_research.NOISE_FLOOR_KEYS:
        assert key in out
    # The reported yield spread must actually reflect the three canned
    # values, not a degenerate/rounded-away artifact.
    assert "'min': 0.5" in out and "'max': 1.0" in out


async def test_run_prints_the_d14_grounding_caveat_in_every_summary(
    eval_research, monkeypatch, tmp_path, capsys,
):
    """D14's caveat lived only in a docstring pre-fix -- a reader of the
    actual CLI output never saw it. Must appear in the printed summary
    itself, every rep, not just once at the end or only in source."""
    from pipeline.config import Settings

    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"set_version": 1, "cases": []}))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, env=None: Settings()))

    await eval_research.run(tag=None)

    out = capsys.readouterr().out
    assert "D14" in out
    assert "NOT the same measurement" in out
    assert "0.221-0.275" in out


# ------------------------------------------------------- model-config guard
#
# Mid-task requirement (D16 / T85): validate_for_run() -- the T69 auto-router
# ban and T28 paid-model ban -- was never called from eval_compose.py,
# smoke.py, build_golden.py or dashboard.py. A reviewer proved that gap had
# ZERO test coverage by replacing each call with `pass` and watching 827/827
# tests stay green. These tests assert the guard is actually CALLED by
# run()'s wiring (_apply_model_config_guard), not merely present somewhere
# importable -- the exact distinction that let the other four sites rot
# silently.


def test_model_config_guard_calls_it_when_settings_provides_one(eval_research):
    calls = {"n": 0}

    class _SettingsWithGuard:
        def validate_model_config(self):
            calls["n"] += 1

    eval_research._apply_model_config_guard(_SettingsWithGuard())
    assert calls["n"] == 1


def test_model_config_guard_propagates_a_rejection(eval_research):
    """Must not swallow a real auto-router/paid-model rejection -- proves
    this is a real call, not a bare hasattr no-op that always succeeds."""

    class _SettingsThatRejects:
        def validate_model_config(self):
            raise RuntimeError("auto-router slug rejected")

    with pytest.raises(RuntimeError, match="auto-router slug rejected"):
        eval_research._apply_model_config_guard(_SettingsThatRejects())


def test_model_config_guard_is_a_noop_on_valid_real_settings(eval_research):
    """T85 has landed on main (validate_model_config exists on the real
    Settings) -- this replaces the self-expiring pre-T85 placeholder test
    per its own instructions ("delete this test and rely on the two above
    plus a real Settings-mutates-and-rejects integration test"). Default
    real Settings (ollama, no env) must pass the guard cleanly."""
    from pipeline.config import Settings

    settings = Settings.load(env={})
    assert hasattr(settings, "validate_model_config")
    eval_research._apply_model_config_guard(settings)  # must not raise


def test_model_config_guard_rejects_a_real_invalid_setting(eval_research):
    """The real Settings-mutates-and-rejects integration test T85's landing
    calls for: an OpenRouter auto-router slug (T69's ban) reaches
    _apply_model_config_guard through the REAL validate_model_config, not a
    mock -- proving the guard is wired to the actual production check, not
    just a stand-in that always agrees with the test."""
    from pipeline.config import MissingConfig, Settings

    settings = Settings.load(env={
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-test",
        "OPENROUTER_MODEL_CHEAP": "openrouter/auto",
    })
    with pytest.raises(MissingConfig, match="auto-router"):
        eval_research._apply_model_config_guard(settings)


def test_run_wires_the_guard_before_scoring_any_item(eval_research, monkeypatch, tmp_path):
    """Proves run() itself calls the guard (order: after any --model
    override, before scoring starts) -- not just that the helper function
    works in isolation. Mutating run() to skip this call must turn this
    red; replacing the call with `pass` is exactly the regression T85
    exists to prevent."""
    from pipeline.config import Settings

    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"set_version": 1, "cases": []}))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)

    guard_calls = []

    class _Rejecting(Settings):
        def validate_model_config(self):
            guard_calls.append(self.model_cheap)
            raise RuntimeError("rejected for this test")

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, env=None: _Rejecting()))

    import asyncio

    with pytest.raises(RuntimeError, match="rejected for this test"):
        asyncio.run(eval_research.run(tag=None, model="some/model"))

    assert guard_calls == ["some/model"], (
        "the guard must see the FINAL model (after --model override), and "
        "must actually run before any item is scored"
    )


# --------------------------------------------------------------- golden set


def test_load_golden_rebuilds_items_at_extracted_status(eval_research, monkeypatch, tmp_path):
    from pipeline.models import Status

    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({
        "set_version": 7,
        "cases": [
            {"failure_mode": "m1", "why": "w1",
             "item": {"id": 1, "source": "channel", "raw_text": "t1", "extracted": {}}},
            {"failure_mode": "m2", "why": "w2",
             "item": {"id": 2, "source": "dm", "raw_text": "t2", "extracted": {}}},
        ],
    }))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)

    items, reasons, version = eval_research.load_golden()
    assert version == 7
    assert [i.id for i in items] == [1, 2]
    assert all(i.status == Status.EXTRACTED for i in items)
    assert reasons == {1: "m1", 2: "m2"}


def test_load_golden_filters_by_only(eval_research, monkeypatch, tmp_path):
    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({
        "set_version": 1,
        "cases": [
            {"failure_mode": "m", "why": "w",
             "item": {"id": 1, "source": "channel", "raw_text": "t", "extracted": {}}},
            {"failure_mode": "m", "why": "w",
             "item": {"id": 2, "source": "channel", "raw_text": "t", "extracted": {}}},
        ],
    }))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)

    items, _reasons, _version = eval_research.load_golden(only=[2])
    assert [i.id for i in items] == [2]


def test_the_committed_research_golden_json_loads_and_has_distinct_ids(eval_research):
    """The actual fixture this task ships, not a synthetic stand-in."""
    items, reasons, version = eval_research.load_golden()
    assert version == 1
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids)) >= 3
    assert all(i.raw_text.strip() for i in items)
    assert set(reasons.values()) >= {"ambiguous-entity", "compound-clauses"}


# ---------------------------------------------------------------- compare()


def test_compare_prints_numeric_deltas_only(tmp_path, eval_research, capsys, monkeypatch):
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    (tmp_path / "research-a.json").write_text(json.dumps({"summary": {
        "harness": "research", "yield_overall": 0.5, "prompt": "fp-a", "items": 3}}))
    (tmp_path / "research-b.json").write_text(json.dumps({"summary": {
        "harness": "research", "yield_overall": 0.75, "prompt": "fp-b", "items": 3}}))

    eval_research.compare("a", "b")
    out = capsys.readouterr().out
    assert "yield_overall" in out
    assert "+0.25" in out
    assert "prompt" not in out.split("\n")[0]  # header line only, not the string field


# ------------------------------------------------------ F2: harness identity


def test_result_path_is_research_prefixed(eval_research, monkeypatch, tmp_path):
    """F2 (T73 bounce 2): eval_compose.py ALSO documents `--tag baseline` as
    its first example and ALSO writes `{tag}.json` into the same
    data/eval/ directory -- this harness must never write to that same bare
    path."""
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    path = eval_research._result_path("baseline")
    assert path == tmp_path / "research-baseline.json"
    assert path.name != "baseline.json"


def test_result_path_sanitizes_the_tag(eval_research, monkeypatch, tmp_path):
    """`_safe_filename_component` used to sit unused two functions away
    while --tag/--compare reached a path as a raw CLI string (nit, T73
    bounce 2) -- now applied here too."""
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    path = eval_research._result_path("../../etc/passwd")
    assert "/" not in path.name.removeprefix("research-").removesuffix(".json")
    assert path.parent == tmp_path


def test_run_writes_the_research_prefixed_filename_not_the_bare_tag(
    eval_research, monkeypatch, tmp_path,
):
    """F2 (T73 bounce 2), end to end through run()'s own save path: the
    literal collision the reviewer demonstrated was eval_compose.py and
    eval_research.py both defaulting to data/eval/{tag}.json -- this proves
    run() itself never writes that bare path."""
    from pipeline.config import Settings

    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"set_version": 1, "cases": []}))
    monkeypatch.setattr(eval_research, "GOLDEN", golden)
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_research, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, env=None: Settings()))

    import asyncio

    asyncio.run(eval_research.run(tag="baseline"))

    assert (tmp_path / "research-baseline.json").exists()
    assert not (tmp_path / "baseline.json").exists(), (
        "must never write the bare tag filename eval_compose.py owns"
    )


def test_summarise_stamps_the_harness_name(eval_research):
    """F2: every saved summary must self-identify as this harness's own --
    the key compare()/_load_harness_result use to refuse a foreign file."""
    summary = eval_research.summarise([], eval_research.CacheStats(), "fp", 1)
    assert summary["harness"] == "research"
    assert summary["harness"] == eval_research.HARNESS_NAME


def test_compare_refuses_a_foreign_harness_file(eval_research, monkeypatch, tmp_path):
    """F2 (T73 bounce 2): reproduces the reviewer's exact demonstration --
    a compose-shaped summary (no "harness" key at all, eval_compose.py's
    real summarise() shape) reaching compare() used to print one
    meaningless row (`set_version 1 1`) and raise nothing. compare() must
    now refuse it by name."""
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    # A compose-shaped summary: no "harness" key, matching eval_compose.py's
    # real summarise() output.
    (tmp_path / "research-baseline.json").write_text(json.dumps({"summary": {
        "set_version": 1, "avg_substance_composite": 39.57}}))
    (tmp_path / "research-arm.json").write_text(json.dumps({"summary": {
        "harness": "research", "golden_set_version": 1, "yield_overall": 0.8}}))

    with pytest.raises(ValueError, match="not produced by this harness"):
        eval_research.compare("baseline", "arm")


def test_compare_also_refuses_a_file_with_the_wrong_harness_value(
    eval_research, monkeypatch, tmp_path,
):
    """Not just a missing key -- an explicitly wrong value must be refused
    too (guards a future second harness reusing this same mechanism)."""
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    (tmp_path / "research-a.json").write_text(json.dumps({"summary": {
        "harness": "compose", "yield_overall": 0.5}}))
    (tmp_path / "research-b.json").write_text(json.dumps({"summary": {
        "harness": "research", "yield_overall": 0.7}}))

    with pytest.raises(ValueError, match="not produced by this harness"):
        eval_research.compare("a", "b")


def test_compare_prints_the_d14_caveat_read_back_from_the_saved_file(
    eval_research, monkeypatch, tmp_path, capsys,
):
    """T73 bounce 2 nit: D14's caveat used to live only in run()'s own
    per-rep stdout -- a reader running --compare later, against artifacts
    from a run they never watched, never saw it. Must be read back off the
    saved summary and reprinted here too."""
    monkeypatch.setattr(eval_research, "RESULTS", tmp_path)
    (tmp_path / "research-a.json").write_text(json.dumps({"summary": {
        "harness": "research", "yield_overall": 0.5,
        "grounding_comparability_caveat": "NOTE (D14): test caveat text"}}))
    (tmp_path / "research-b.json").write_text(json.dumps({"summary": {
        "harness": "research", "yield_overall": 0.6}}))

    eval_research.compare("a", "b")
    out = capsys.readouterr().out
    assert "NOTE (D14): test caveat text" in out


# ------------------------------------------------ full assembly, offline (AC1)


class _StubLLM:
    """Answers deterministically by schema shape rather than a FIFO queue,
    so a test can drive research() through the SAME item twice without
    re-queuing every call by hand -- exactly the "two consecutive runs
    against an identical config" AC1 asks for.

    T71 (research.py): the note call now runs on whichever role
    ``settings.research_note_role`` names -- ``cheap`` by default, pending
    T135 (D94) -- while the plan and relevance-gate calls stay on ``cheap``
    unconditionally.
    Both roles share the SAME schema-kind dispatch (``_answer``) rather than
    ``cheap`` alone, so this stub answers correctly regardless of which role
    a given ``Settings`` actually routes the note call through, exactly
    like the real ``LLMClient``'s two methods differ only in model/prompt,
    never in which schema kinds they can answer.
    """

    def __init__(self, module):
        self._kind = module._schema_kind

    def _answer(self, schema):
        kind = self._kind(schema)
        if kind == "plan":
            return {"entity": "Widget", "entity_context": "test widget",
                    "queries": ["widget update", "widget background"],
                    "image_query": "widget photo", "clauses": []}
        if kind == "relevance":
            return {"relevant": True, "why": "on topic"}
        if kind == "note":
            return {"claim": "Widget shipped an update", "detail": "in Q3",
                    "confidence": "high"}
        if kind == "coverage":
            return {"uncovered": []}
        raise AssertionError(f"unexpected schema kind: {kind}")

    async def cheap(self, system, user, schema=None, temperature=0.3, **kw):
        return self._answer(schema)

    async def good(self, system, user, schema=None, temperature=0.5, **kw):
        return self._answer(schema)


def _widget_item():
    from pipeline.models import Item, Status

    return Item(id=901, source="channel", status=Status.EXTRACTED,
                raw_text="Widget Corp shipped a new update.", extracted={})


async def test_two_runs_against_the_same_cache_make_zero_network_calls_on_the_second(
    tmp_path, settings, eval_research,
):
    """AC1, end to end: research() + Recorder + CachingHTTP, twice, offline.

    Two queries (MIN_NOTES=2 needs at least two notes to succeed) that both
    end up fetching the same single article -- a realistic overlap, not an
    artificially non-overlapping scenario -- so the invariant asserted below
    (``stats2.hits == total attempts made in run 1``) holds regardless of
    whether a request happened to repeat within run 1 itself.
    """
    from dataclasses import replace as dc_replace

    s = dc_replace(settings, research_concurrency=1, docs_per_query=1, results_per_query=3)
    item = _widget_item()
    article_html = ("<html><body><article>" + ("Widget shipped a new update. " * 30)
                     + "</article></body></html>")
    search_json = json.dumps({"results": [
        {"url": "https://widget-news.example/a", "title": "t", "content": ""}
    ]}).encode()

    backend1 = _FakeBackend(eval_research, {
        "/search": (200, search_json),
        "widget-news.example": (200, article_html.encode()),
    })
    stats1 = eval_research.CacheStats()
    http1 = eval_research.CachingHTTP(backend1, tmp_path / "cache", stats1)
    plan_cache_dir = tmp_path / "plan-cache"
    llm1 = eval_research.Recorder(_StubLLM(eval_research), plan_cache_dir=plan_cache_dir, stats=stats1)

    row1 = await eval_research.score_one_item(item, llm1, http1, s)

    assert row1["error"] is None
    assert row1["notes_produced"] == 2
    run1_total_attempts = stats1.hits + stats1.misses
    assert stats1.misses > 0

    exploding = _ExplodingBackend()
    stats2 = eval_research.CacheStats()
    http2 = eval_research.CachingHTTP(exploding, tmp_path / "cache", stats2)
    llm2 = eval_research.Recorder(_StubLLM(eval_research), plan_cache_dir=plan_cache_dir, stats=stats2)

    row2 = await eval_research.score_one_item(item, llm2, http2, s)

    assert row2["error"] is None, "a silent re-fetch would surface here as a swallowed exception"
    assert exploding.calls == 0, "the exploding backend must never be reached"
    assert stats2.misses == 0
    assert stats2.hits == run1_total_attempts, (
        "every request made in run 1 (hit or miss) must be served from cache in run 2"
    )

    # Byte-identical corpus: the exact packed web excerpts each note call saw.
    note_calls_1 = [c for c in llm1.calls if eval_research._call_kind(c) == "note"]
    note_calls_2 = [c for c in llm2.calls if eval_research._call_kind(c) == "note"]
    assert len(note_calls_1) == len(note_calls_2) == 2
    assert [c.request_user for c in note_calls_1] == [c.request_user for c in note_calls_2]
    assert row1["notes_produced"] == row2["notes_produced"]
    assert row1["queries_planned"] == row2["queries_planned"]
