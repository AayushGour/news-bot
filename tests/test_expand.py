"""Query expansion: widen the search, not the research."""

import pytest

from pipeline.expand import EXPANSION_SYSTEM, expand_queries, widen


class FakeLLM:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def cheap(self, system, user, schema=None, **kw):
        self.calls.append(user)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def test_variants_are_returned_per_query():
    llm = FakeLLM({"expansions": [
        {"query": "early signs of burnout",
         "variants": ["occupational burnout symptoms", "employee exhaustion indicators"]},
    ]})
    out = await expand_queries(llm, ["early signs of burnout"])
    assert out["early signs of burnout"] == [
        "occupational burnout symptoms", "employee exhaustion indicators"]


async def test_a_variant_group_for_an_unasked_query_is_dropped():
    """Otherwise a search runs off topic under an original query's banner."""
    llm = FakeLLM({"expansions": [
        {"query": "something nobody asked", "variants": ["a", "b"]},
    ]})
    assert await expand_queries(llm, ["the real query"]) == {}


async def test_a_variant_identical_to_the_original_is_dropped():
    llm = FakeLLM({"expansions": [
        {"query": "burnout signs", "variants": ["Burnout Signs", "exhaustion markers"]},
    ]})
    out = await expand_queries(llm, ["burnout signs"])
    assert out["burnout signs"] == ["exhaustion markers"]


async def test_variants_are_capped():
    llm = FakeLLM({"expansions": [
        {"query": "q", "variants": [f"v{i}" for i in range(20)]},
    ]})
    out = await expand_queries(llm, ["q"], per_query=3)
    assert len(out["q"]) == 3


async def test_expansion_failure_leaves_the_search_unchanged():
    """Expansion improves recall; it must never stop a search happening."""
    llm = FakeLLM(RuntimeError("model down"))
    assert await expand_queries(llm, ["q"]) == {}


async def test_no_queries_means_no_call():
    llm = FakeLLM()
    assert await expand_queries(llm, []) == {}
    assert llm.calls == []


def test_widen_puts_the_original_first():
    assert widen("q", {"q": ["a", "b"]}) == ["q", "a", "b"]
    assert widen("q", {}) == ["q"]


def test_the_prompt_forbids_the_failures_that_caused_this():
    collapsed = " ".join(EXPANSION_SYSTEM.split())
    assert "Do NOT weld two topics together" in collapsed
    assert "Do NOT expand an acronym you were not given" in collapsed
    assert "Do NOT narrow" in collapsed
