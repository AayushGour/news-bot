"""T287 tester blackbox: Instagram throttling through a real Worker tick and a
real temp Database. Asserts DB row state, not just the exception type."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import (
    IG_MIN_PUBLISH_GAP_S,
    IG_POLICY_BLOCK_BACKOFF_S,
    IG_RATE_LIMIT_BACKOFF_S,
)
from pipeline.faults import ig_alerts
from pipeline.models import Status
from pipeline.publish.instagram import publish_carousel
from pipeline.worker import Worker
from tests.publish.test_publish import live
from tests.publish.test_publish import media_host_serves_images  # noqa: F401

TOKEN = "IGT"  # LIVE access token used by live()
_msg = iter(range(5000, 6000))


async def _seed(db):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=next(_msg), raw_text="news")
    await db.transition(i, Status.APPROVED, {
        "caption": "c",
        "media_urls": ["https://cdn.example/a.png", "https://cdn.example/b.png"],
    })
    await db.transition(i, Status.PUBLISHING)
    return i


def _worker(db, http, settings):
    s = live(settings)

    async def do_publish(item):
        post_id = await publish_carousel(item, http, db, s)
        return {"ig_post_id": post_id,
                "published_at": datetime.now(timezone.utc).isoformat()}

    return Worker(db, {Status.PUBLISHING: (do_publish, Status.PUBLISHED)})


def _secs_ahead(item):
    return (datetime.fromisoformat(item.next_attempt_at)
            - datetime.now(timezone.utc)).total_seconds()


async def _assert_deferred(db, i, seconds):
    it = await db.get_item(i)
    assert it.status == Status.PUBLISHING
    assert it.status != Status.FAILED
    assert it.attempts == 0
    assert abs(_secs_ahead(it) - seconds) < 60
    assert TOKEN not in (it.last_error or "").replace("Instagram", "")
    assert "access_token" not in (it.last_error or "")


async def test_ac1_item_520_exact_message_no_code(db, settings, fake_http):
    i = await _seed(db)
    fake_http.respond(400, {"error": {"message": "User is performing too many actions"}})
    assert await _worker(db, fake_http, settings).tick() == 1
    await _assert_deferred(db, i, IG_RATE_LIMIT_BACKOFF_S)
    assert IG_RATE_LIMIT_BACKOFF_S == 2 * 3600


async def test_ac1_item_520_full_body_with_code(db, settings, fake_http):
    i = await _seed(db)
    fake_http.respond(400, {"error": {
        "message": "User is performing too many actions", "type": "OAuthException",
        "code": 4, "error_subcode": 2207051}})
    await _worker(db, fake_http, settings).tick()
    await _assert_deferred(db, i, IG_RATE_LIMIT_BACKOFF_S)


@pytest.mark.parametrize("status,body", [
    (400, {"error": {"message": "x", "code": 17}}),
    (400, {"error": {"message": "x", "code": 80002}}),
    (400, {"error": {"message": "x", "code": 9007, "error_subcode": 2207042}}),
    (429, "Too Many Requests"),
    (400, {"error": {"message": "Application request limit reached"}}),
    (400, {"error": {"message": "hit the rate limit"}}),
])
async def test_ac1_variants(db, settings, fake_http, status, body):
    i = await _seed(db)
    fake_http.respond(status, body)
    await _worker(db, fake_http, settings).tick()
    await _assert_deferred(db, i, IG_RATE_LIMIT_BACKOFF_S)


async def test_ac2_code_368_defers_24h(db, settings, fake_http):
    i = await _seed(db)
    fake_http.respond(400, {"error": {"message": "blocked", "code": 368}})
    await _worker(db, fake_http, settings).tick()
    await _assert_deferred(db, i, IG_POLICY_BLOCK_BACKOFF_S)
    assert IG_POLICY_BLOCK_BACKOFF_S == 24 * 3600


@pytest.mark.parametrize("code,msg", [(100, "Invalid parameter"),
                                      (190, "Invalid OAuth access token")])
async def test_ac3_genuine_terminal_still_fails(db, settings, fake_http, code, msg):
    i = await _seed(db)
    fake_http.respond(400, {"error": {"message": msg, "code": code}})
    await _worker(db, fake_http, settings).tick()
    it = await db.get_item(i)
    assert it.status == Status.FAILED


async def test_ac6_unfetchable_media_still_retryable(db, settings, fake_http):
    i = await _seed(db)
    fake_http.respond(400, {"error": {
        "message": "Only photo or video can be accepted as media type."}})
    await _worker(db, fake_http, settings).tick()
    it = await db.get_item(i)
    assert it.status == Status.PUBLISHING   # not failed
    assert it.attempts == 1                 # Retryable spends an attempt
    assert it.next_attempt_at is not None


def _ok(http, post):
    http.respond_sequence([
        (200, {"id": "c1"}), (200, {"id": "c2"}), (200, {"id": "car"}),
        (200, {"status_code": "FINISHED"}), (200, {"id": post}),
    ])


async def test_ac4_pacing_defer_spends_no_attempt_no_alert(db, settings, fake_http):
    sent = []

    async def send(t):
        sent.append(t)
    ig_alerts.send = send

    a = await _seed(db)
    b = await _seed(db)
    _ok(fake_http, "P1")
    w = _worker(db, fake_http, settings)
    await w.tick()  # one tick claims both: a publishes, b hits the gap
    first = await db.get_item(a)
    assert first.status == Status.PUBLISHED and first.ig_post_id == "P1"
    calls = 5  # exactly one carousel publish (5 Graph calls); b made none

    second = await db.get_item(b)
    assert second.status == Status.PUBLISHING
    assert second.attempts == 0
    assert 0 < _secs_ahead(second) <= IG_MIN_PUBLISH_GAP_S + 5
    assert len([c for c in fake_http.calls if "media" in c.url]) <= calls  # no API call spent
    assert sent == []                         # pacing never alerts


async def test_ac5_one_alert_per_stall_and_rearm(db, settings, fake_http):
    sent = []

    async def send(t):
        sent.append(t)
    ig_alerts.send = send

    a = await _seed(db)
    fake_http.respond(400, {"error": {
        "message": f"User is performing too many actions {TOKEN}", "code": 4}})
    w = _worker(db, fake_http, settings)
    await w.tick()
    await db.clear_backoff(a)
    await w.tick()
    await db.clear_backoff(a)
    await w.tick()
    assert sent == [f"Instagram rate-limited (item {a}), retrying in 2h"]
    assert "too many actions" not in sent[0] and TOKEN not in sent[0]

    # Stall clears: a publish succeeds (pacing gap long past) -> alert re-armed.
    from pipeline.publish import instagram
    instagram._last_publish_mono = None
    _ok(fake_http, "PX")
    fake_http.respond(200, {})
    await db.clear_backoff(a)
    await w.tick()
    assert (await db.get_item(a)).status == Status.PUBLISHED

    instagram._last_publish_mono = None
    old = datetime.now(timezone.utc) - timedelta(seconds=IG_MIN_PUBLISH_GAP_S + 60)
    await db.update_fields(a, {"published_at": old.isoformat()})
    c = await _seed(db)
    fake_http._queue.clear()
    fake_http.respond(400, {"error": {"message": "too many actions", "code": 4}})
    await w.tick()
    assert sent == [f"Instagram rate-limited (item {a}), retrying in 2h",
                    f"Instagram rate-limited (item {c}), retrying in 2h"]
