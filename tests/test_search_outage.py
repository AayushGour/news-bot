"""An engine that is rate-limited must not read as "the web has nothing".

SearXNG suspends an engine that 403s under load, then answers 200 with an
empty result list for the whole cooldown. Item 100 fired sixteen expanded
queries at the single github engine, tripped its limit, and told the operator
"found nothing worth posting — reply with a better search term". The search
term was fine, and answering the question just spent the cooldown again.
"""

import asyncio

import pytest

from pipeline.errors import Retryforever
from pipeline.search import (
    _all_engines_down,
    _unresponsive,
    search_many,
    searx,
)


@pytest.fixture(autouse=True)
def fast_pacing(monkeypatch):
    """Pacing is real wall-clock sleeping; 9s x 16 queries has no place in a
    unit test. Tests that assert ON the pacing set their own value."""
    import pipeline.search as mod
    monkeypatch.setattr(mod, "SINGLE_ENGINE_INTERVAL_S", 0.0)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeHTTP:
    """Returns a queued payload per call, recording when each call happened."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.at: list[float] = []

    async def get(self, url, params=None, timeout=None):
        self.at.append(asyncio.get_running_loop().time())
        return FakeResponse(self.payloads.pop(0) if self.payloads else {"results": []})


SUSPENDED = {"results": [], "unresponsive_engines": [["github", "Suspended: access denied"]]}
HIT = {"results": [{"url": "https://github.com/a/b", "title": "b", "content": "x"}]}


def test_unresponsive_accepts_pair_and_bare_string_shapes():
    assert _unresponsive({"unresponsive_engines": [["github", "why"]]}) == {"github"}
    assert _unresponsive({"unresponsive_engines": ["github"]}) == {"github"}
    assert _unresponsive({}) == set()


def test_all_engines_down_only_when_every_named_engine_failed():
    body = {"unresponsive_engines": [["github", "x"]]}
    assert _all_engines_down(body, "github", "") is True
    assert _all_engines_down(body, "github,gitlab", "") is False


def test_a_category_query_is_never_called_an_outage():
    """Which engines a category fans out to is not visible in the response, and
    one dead engine among healthy ones is a normal empty result."""
    body = {"unresponsive_engines": [["github", "x"]]}
    assert _all_engines_down(body, "", "general,it,news") is False


async def test_suspended_named_engine_raises_instead_of_returning_empty():
    http = FakeHTTP([SUSPENDED])
    with pytest.raises(Retryforever, match="engines unavailable"):
        await searx(http, "http://x", "q", engines="github")


async def test_a_genuinely_empty_result_still_returns_empty():
    """The engine answered; it just had no matches. Not an outage."""
    http = FakeHTTP([{"results": []}])
    assert await searx(http, "http://x", "q", engines="github") == []


async def test_partial_results_survive_an_outage_part_way_through():
    """The bug that made 107 repositories look like zero."""
    http = FakeHTTP([HIT, HIT, SUSPENDED, SUSPENDED])
    out = await search_many(http, "http://x", ["a", "b", "c", "d"], engines="github")
    assert len(out) == 1, "one unique url across the two hits"


async def test_a_total_outage_still_raises():
    """Nothing came back at all — the caller must not treat that as a result."""
    http = FakeHTTP([SUSPENDED, SUSPENDED])
    with pytest.raises(Retryforever):
        await search_many(http, "http://x", ["a", "b"], engines="github")


async def test_single_engine_queries_are_paced_apart(monkeypatch):
    """Concurrency is what earned the 403; single-engine work is serialised."""
    import pipeline.search as mod
    monkeypatch.setattr(mod, "SINGLE_ENGINE_INTERVAL_S", 0.05)
    http = FakeHTTP([HIT, HIT, HIT])
    await search_many(http, "http://x", ["a", "b", "c"], engines="github")
    gaps = [b - a for a, b in zip(http.at, http.at[1:])]
    assert all(g >= 0.04 for g in gaps), gaps


async def test_category_queries_are_not_paced(monkeypatch):
    """A category query spreads across engines, so it keeps its concurrency."""
    import pipeline.search as mod
    monkeypatch.setattr(mod, "SINGLE_ENGINE_INTERVAL_S", 5.0)
    http = FakeHTTP([HIT, HIT, HIT])
    await asyncio.wait_for(
        search_many(http, "http://x", ["a", "b", "c"], categories="general,news"),
        timeout=2.0,
    )
