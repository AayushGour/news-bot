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
    the same plan_cache_path; a relevance-shaped call with the same schema
    property overlap risk must never be mistaken for one."""
    calls = {"n": 0}

    class _CountingLLM:
        async def cheap(self, system, user, schema=None, **kw):
            calls["n"] += 1
            return {"entity": "E", "entity_context": "ctx",
                    "queries": [f"q{calls['n']}"], "image_query": "img", "clauses": []}

    plan_schema = {"properties": {"entity": {}, "queries": {}}}
    path = tmp_path / "plan.json"
    stats = eval_research.CacheStats()

    r1 = eval_research.Recorder(_CountingLLM(), plan_cache_path=path, stats=stats)
    first = await r1.cheap("sys", "usr", schema=plan_schema)
    assert stats.misses == 1 and stats.hits == 0
    assert calls["n"] == 1

    r2 = eval_research.Recorder(_CountingLLM(), plan_cache_path=path, stats=stats)
    second = await r2.cheap("sys", "usr", schema=plan_schema)
    assert stats.misses == 1 and stats.hits == 1, "the second plan call must be a cache hit"
    assert calls["n"] == 1, "the inner LLM must not have been called again"
    assert second == first


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


def test_model_config_guard_is_a_noop_before_t85_lands(eval_research):
    """The real pipeline.config.Settings on main today has no
    validate_model_config yet (T85 unmerged) -- must not raise."""
    from pipeline.config import Settings

    settings = Settings.load(env={})
    assert not hasattr(settings, "validate_model_config"), (
        "this test's own premise (T85 unmerged) no longer holds -- once "
        "validate_model_config lands, delete this test and rely on the two "
        "above plus a real Settings-mutates-and-rejects integration test"
    )
    eval_research._apply_model_config_guard(settings)  # must not raise


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
    (tmp_path / "a.json").write_text(json.dumps({"summary": {
        "yield_overall": 0.5, "prompt": "fp-a", "items": 3}}))
    (tmp_path / "b.json").write_text(json.dumps({"summary": {
        "yield_overall": 0.75, "prompt": "fp-b", "items": 3}}))

    eval_research.compare("a", "b")
    out = capsys.readouterr().out
    assert "yield_overall" in out
    assert "+0.25" in out
    assert "prompt" not in out.split("\n")[0]  # header line only, not the string field


# ------------------------------------------------ full assembly, offline (AC1)


class _StubLLM:
    """Answers deterministically by schema shape rather than a FIFO queue,
    so a test can drive research() through the SAME item twice without
    re-queuing every call by hand -- exactly the "two consecutive runs
    against an identical config" AC1 asks for.
    """

    def __init__(self, module):
        self._kind = module._schema_kind

    async def cheap(self, system, user, schema=None, temperature=0.3, **kw):
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
    plan_path = tmp_path / "plan.json"
    llm1 = eval_research.Recorder(_StubLLM(eval_research), plan_cache_path=plan_path, stats=stats1)

    row1 = await eval_research.score_one_item(item, llm1, http1, s)

    assert row1["error"] is None
    assert row1["notes_produced"] == 2
    run1_total_attempts = stats1.hits + stats1.misses
    assert stats1.misses > 0

    exploding = _ExplodingBackend()
    stats2 = eval_research.CacheStats()
    http2 = eval_research.CachingHTTP(exploding, tmp_path / "cache", stats2)
    llm2 = eval_research.Recorder(_StubLLM(eval_research), plan_cache_path=plan_path, stats=stats2)

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
