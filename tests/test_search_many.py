"""Parallel search: the queries are independent, so they run together."""

import asyncio

import pytest

from pipeline.errors import Retryforever
from pipeline.search import search_many


class FakeHTTP:
    def __init__(self, by_query, delay=0.0, fail=None):
        self.by_query = by_query
        self.delay = delay
        self.fail = fail or {}
        self.queries = []
        self.concurrent = 0
        self.peak = 0

    async def get(self, url, params=None, timeout=None):
        q = params["q"]
        self.queries.append(q)
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            if q in self.fail:
                raise self.fail[q]
            return FakeResponse(self.by_query.get(q, []))
        finally:
            self.concurrent -= 1


class FakeResponse:
    status_code = 200

    def __init__(self, results):
        self._results = results

    def json(self):
        return {"results": self._results}


def _r(url):
    return {"url": url, "title": "t", "content": "c"}


async def test_results_from_every_query_are_merged():
    http = FakeHTTP({"a": [_r("https://x.example/1")],
                     "b": [_r("https://y.example/2")]})
    out = await search_many(http, "http://searx", ["a", "b"])
    assert {r["url"] for r in out} == {"https://x.example/1", "https://y.example/2"}


async def test_duplicate_urls_are_collapsed_first_seen_first():
    http = FakeHTTP({"a": [_r("https://x.example/1")],
                     "b": [_r("https://x.example/1"), _r("https://y.example/2")]})
    out = await search_many(http, "http://searx", ["a", "b"])
    assert [r["url"] for r in out] == ["https://x.example/1", "https://y.example/2"]


async def test_queries_actually_run_concurrently():
    http = FakeHTTP({q: [] for q in "abcd"}, delay=0.02)
    await search_many(http, "http://searx", list("abcd"), concurrency=4)
    assert http.peak > 1, "searching in series was the thing being fixed"


async def test_concurrency_is_bounded():
    http = FakeHTTP({q: [] for q in "abcdef"}, delay=0.02)
    await search_many(http, "http://searx", list("abcdef"), concurrency=2)
    assert http.peak <= 2


async def test_one_empty_query_does_not_sink_the_rest():
    http = FakeHTTP({"a": [], "b": [_r("https://y.example/2")]})
    out = await search_many(http, "http://searx", ["a", "b"])
    assert len(out) == 1


async def test_an_outage_propagates_rather_than_looking_like_no_results():
    """SearXNG being down is down for every query. Flattening that to [] would
    report an outage as a research shortfall."""
    http = FakeHTTP({"a": [], "b": []}, fail={"a": ConnectionError("refused")})
    with pytest.raises(Retryforever):
        await search_many(http, "http://searx", ["a", "b"])


async def test_blank_queries_are_skipped():
    http = FakeHTTP({"a": []})
    await search_many(http, "http://searx", ["a", "", "   "])
    assert http.queries == ["a"]


async def test_no_queries_means_no_requests():
    http = FakeHTTP({})
    assert await search_many(http, "http://searx", []) == []
    assert http.queries == []


def test_the_default_is_actually_parallel():
    """Callers that pass no concurrency — the enumeration path among them —
    must still fan out rather than quietly running in series."""
    import inspect

    default = inspect.signature(search_many).parameters["concurrency"].default
    assert default > 1
