"""T337 blackbox: drive the worker's PUBLISHING stage against a scripted Graph API.

No real Instagram calls: ``GraphFake`` answers by route (status poll, media_publish,
recent-media guard, container creation) with the shapes Meta really returns.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.publish.instagram as ig
from pipeline.errors import PlatformThrottled, Terminal
from pipeline.models import Status
from pipeline.publish.instagram import publish_carousel
from pipeline.worker import Worker
from tests.conftest import FakeResponse

TOKEN = "IGQ-SECRET-TOKEN-T337-zzz"
CAPTION = "Item 581 caption é"
NOT_READY = {"error": {"message": "Media ID is not available", "code": 9007,
                       "error_subcode": 2207027}}
FINISHED = {"status_code": "FINISHED"}
PUBLISHED = {"status_code": "PUBLISHED"}


def _ago(seconds: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S+0000")


class GraphFake:
    """Route-based fake. Each route holds a script; the last entry repeats."""

    def __init__(self, **scripts):
        self.scripts = {"status": [(200, FINISHED)], "publish": [(200, {"id": "POST1"})],
                        "recent": [(200, {"data": []})], "create": None}
        self.scripts.update(scripts)
        self.calls: list[tuple[str, str]] = []
        self._n = {k: 0 for k in self.scripts}
        self._created = 0

    def _take(self, route):
        script = self.scripts[route]
        i = min(self._n[route], len(script) - 1)
        self._n[route] += 1
        step = script[i]
        if isinstance(step, Exception):
            raise step
        return FakeResponse(*step)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    async def get(self, url, params=None, timeout=None, headers=None, **kw):
        if "cdn.example" in url:
            return FakeResponse(200, "")
        route = "recent" if url.endswith("/media") else "status"
        self.calls.append((route, url))
        return self._take(route)

    async def post(self, url, json=None, data=None, timeout=None, headers=None, **kw):
        if url.endswith("/media_publish"):
            self.calls.append(("publish", url))
            return self._take("publish")
        self.calls.append(("create", url))
        self._created += 1
        return FakeResponse(200, {"id": f"cont{self._created}"})


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)


@pytest.fixture
def live(settings):
    return replace(settings, dry_run=False, ig_user_id="17841400000000000",
                   ig_access_token=TOKEN, r2_bucket="b",
                   r2_public_base="https://cdn.example", r2_account_id="a",
                   r2_access_key="k", r2_secret_key="s")


_MSG = iter(range(1, 10_000))


async def _seed(db, *, containers=True, **fields):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=next(_MSG), raw_text="news")
    base = {"caption": CAPTION,
            "rendered_paths": ["/tmp/a.png", "/tmp/b.png"],
            "media_urls": ["https://cdn.example/a.png", "https://cdn.example/b.png"]}
    if containers:
        base.update(ig_child_ids=["c1", "c2"], ig_carousel_id="car581")
    base.update(fields)
    await db.transition(i, Status.PUBLISHING, base)
    return i


def _worker(db, http, live):
    async def do_publish(item):
        from pipeline.db import now_iso
        post_id = await publish_carousel(item, http, db, live)
        return {"ig_post_id": post_id, "published_at": now_iso()}

    return Worker(db, {Status.PUBLISHING: (do_publish, Status.PUBLISHED)},
                  max_attempts=3, settings=live)


async def _make_due(db, i):
    await db.conn.execute("UPDATE items SET next_attempt_at=NULL WHERE id=?", (i,))
    await db.conn.commit()


async def _tick(db, w, i):
    await _make_due(db, i)
    n = await w.tick()
    return n, await db.get_item(i)


# 1 ------------------------------------------------------------------------
async def test_ac1_replay_item_581(db, live):
    i = await _seed(db)
    http = GraphFake(publish=[(400, NOT_READY), (200, {"id": "178999"})])
    n, item = await _tick(db, _worker(db, http, live), i)
    assert item.status == Status.PUBLISHED
    assert item.ig_post_id == "178999"
    assert http.count("publish") == 2
    assert [e["ig_post_id"] for e in item.publish_log] == ["178999"]
    assert item.attempts == 0


# 2 ------------------------------------------------------------------------
async def test_ac2_persistent_not_ready_stays_retryable_and_reuses_container(db, live):
    i = await _seed(db, containers=False)
    http = GraphFake(publish=[(400, NOT_READY)])
    w = _worker(db, http, live)

    _, item = await _tick(db, w, i)
    assert item.status == Status.PUBLISHING          # not failed
    assert item.attempts == 1
    assert item.next_attempt_at is not None           # backoff scheduled
    assert "Media ID is not available" in item.last_error
    assert http.count("publish") == ig.PUBLISH_NOT_READY_TRIES == 3
    cid = item.ig_carousel_id
    assert cid and item.ig_child_ids
    created_after_first = http.count("create")

    # attempt 2: guard looks (no match), same container reused, 3 more calls
    _, item = await _tick(db, w, i)
    assert item.status == Status.PUBLISHING and item.attempts == 2
    assert http.count("publish") == 6
    assert http.count("create") == created_after_first
    assert item.ig_carousel_id == cid
    assert http.count("recent") >= 1


# 3 ------------------------------------------------------------------------
async def test_ac3_lost_200_is_adopted_by_guard_with_no_extra_publish(db, live):
    i = await _seed(db)
    http = GraphFake(publish=[(0, None)])
    http.scripts["publish"] = [asyncio.TimeoutError("read timeout")]
    w = _worker(db, http, live)
    _, item = await _tick(db, w, i)
    assert item.status == Status.PUBLISHING and item.attempts == 1
    assert http.count("publish") == 1

    # IG actually published it.
    http.scripts["recent"] = [(200, {"data": [
        {"id": "REALPOST", "caption": CAPTION, "timestamp": _ago(30)}]})]
    _, item = await _tick(db, w, i)
    assert item.status == Status.PUBLISHED and item.ig_post_id == "REALPOST"
    assert http.count("publish") == 1                 # zero extra
    assert [e["ig_post_id"] for e in item.publish_log] == ["REALPOST"]


# 4 ------------------------------------------------------------------------
async def test_ac4_published_container_never_media_publishes(db, live):
    i = await _seed(db)
    http = GraphFake(status=[(200, PUBLISHED)],
                     recent=[(200, {"data": [
                         {"id": "LIVE1", "caption": CAPTION, "timestamp": _ago(5)}]})])
    _, item = await _tick(db, _worker(db, http, live), i)
    assert item.status == Status.PUBLISHED and item.ig_post_id == "LIVE1"
    assert http.count("publish") == 0


async def test_ac4b_published_container_without_match_defers_never_publishes(db, live):
    i = await _seed(db)
    http = GraphFake(status=[(200, PUBLISHED)])
    _, item = await _tick(db, _worker(db, http, live), i)
    assert item.status == Status.PUBLISHING and item.attempts == 1
    assert http.count("publish") == 0


# 5 ------------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    {"error": {"message": "x", "code": 4}},
    {"error": {"message": "Too many actions, slow down"}},
])
async def test_ac5_throttle_is_platformthrottled_no_retry_no_guard(db, live, body):
    i = await _seed(db)
    http = GraphFake(publish=[(400, body)])
    with pytest.raises(PlatformThrottled):
        await publish_carousel(await db.get_item(i), http, db, live)
    assert http.count("publish") == 1
    assert http.count("recent") == 0


async def test_ac5_throttle_via_worker_spends_no_attempt(db, live):
    i = await _seed(db)
    http = GraphFake(publish=[(400, {"error": {"message": "x", "code": 4}})])
    _, item = await _tick(db, _worker(db, http, live), i)
    assert item.status == Status.PUBLISHING and item.attempts == 0
    assert item.next_attempt_at is not None


async def test_ac5_bad_token_is_terminal(db, live):
    i = await _seed(db)
    http = GraphFake(publish=[(400, {"error": {"message": "Invalid OAuth access token.",
                                               "code": 190}})])
    with pytest.raises(Terminal):
        await publish_carousel(await db.get_item(i), http, db, live)
    assert http.count("publish") == 1


async def test_ac5_bad_token_via_worker_fails_item(db, live):
    i = await _seed(db)
    http = GraphFake(publish=[(400, {"error": {"message": "bad token", "code": 190}})])
    _, item = await _tick(db, _worker(db, http, live), i)
    assert item.status == Status.FAILED
    assert http.count("publish") == 1


# 6 ------------------------------------------------------------------------
GUARD_CASES = {
    "different caption": ({"id": "X", "caption": "other", "timestamp": _ago(10)}, CAPTION, []),
    "older than 24h": ({"id": "X", "caption": CAPTION, "timestamp": _ago(25 * 3600)}, CAPTION, []),
    "already in publish_log": ({"id": "X", "caption": CAPTION, "timestamp": _ago(10)}, CAPTION,
                               [{"ig_post_id": "X", "at": "t"}]),
    "empty caption": ({"id": "X", "caption": "", "timestamp": _ago(10)}, "", []),
}


@pytest.mark.parametrize("name", list(GUARD_CASES))
async def test_ac6_guard_negatives_do_not_adopt(db, live, name):
    entry, caption, log_ = GUARD_CASES[name]
    i = await _seed(db, caption=caption, publish_log=log_)
    await db.record_failure(i, "earlier")             # attempts=1 -> guard runs
    http = GraphFake(recent=[(200, {"data": [entry]})], publish=[(200, {"id": "NEW"})])
    post = await publish_carousel(await db.get_item(i), http, db, live)
    assert post == "NEW"
    assert http.count("publish") == 1


async def test_ac6_guard_api_500_does_not_adopt_or_block(db, live):
    i = await _seed(db)
    await db.record_failure(i, "earlier")
    http = GraphFake(recent=[(500, {"error": {"message": "down"}})])
    assert await publish_carousel(await db.get_item(i), http, db, live) == "POST1"


async def test_ac6_positive_control_whitespace_case_nfc_match(db, live):
    i = await _seed(db, caption="Café  Au\nLait")
    await db.record_failure(i, "earlier")
    http = GraphFake(recent=[(200, {"data": [
        {"id": "M", "caption": "café au lait", "timestamp": _ago(10)}]})])
    assert await publish_carousel(await db.get_item(i), http, db, live) == "M"
    assert http.count("publish") == 0


# 7 ------------------------------------------------------------------------
async def test_ac7_token_never_in_logs_or_errors(db, live, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    scenarios = [
        [(400, NOT_READY), (200, {"id": "P"})],
        [(400, NOT_READY)],
        [(400, {"error": {"message": "x", "code": 4}})],
        [(400, {"error": {"message": "bad", "code": 190}})],
        [(503, {"error": {"message": "down"}})],
    ]
    for script in scenarios:
        monkeypatch.setattr(ig, "_last_publish_mono", None)
        i = await _seed(db)
        http = GraphFake(publish=script)
        try:
            await publish_carousel(await db.get_item(i), http, db, live)
        except Exception as exc:
            texts.append(f"{type(exc).__name__}: {exc}")
        monkeypatch.setattr(ig, "_last_publish_mono", None)
    # and via the worker, which stores last_error / logs
    for script in scenarios:
        monkeypatch.setattr(ig, "_last_publish_mono", None)
        i = await _seed(db)
        _, item = await _tick(db, _worker(db, GraphFake(publish=script), live), i)
        texts.append(item.last_error or "")
    assert texts
    joined = "\n".join(texts + [r.getMessage() for r in caplog.records]
                       + [str(r.exc_info[1]) for r in caplog.records if r.exc_info])
    assert caplog.records and "Media ID is not available" in joined
    assert TOKEN not in joined
