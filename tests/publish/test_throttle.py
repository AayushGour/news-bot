"""T287: Instagram throttling defers items instead of killing them, and
publishes are paced so a burst cannot recur."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import (
    IG_MIN_PUBLISH_GAP_S,
    IG_POLICY_BLOCK_BACKOFF_S,
    IG_RATE_LIMIT_BACKOFF_S,
)
from pipeline.faults import ig_alerts
from pipeline.errors import Retryable, Retryforever, Terminal
from pipeline.publish import instagram
from pipeline.publish.instagram import publish_carousel
from pipeline.models import Status
from tests.publish.test_publish import live
from tests.publish.test_publish import media_host_serves_images  # noqa: F401

_msg = iter(range(1000, 2000))


async def _seed_approved(db):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=next(_msg), raw_text="news")
    await db.transition(i, Status.APPROVED, {
        "caption": "c",
        "media_urls": ["https://cdn.example/a.png", "https://cdn.example/b.png"],
    })
    return i

ITEM_520 = {"error": {
    "message": "User is performing too many actions", "type": "OAuthException",
    "code": 4, "error_subcode": 2207051}}


async def _publish(db, i, http, settings):
    return await publish_carousel(await db.get_item(i), http, db, live(settings))


async def test_item_520_exact_body_is_deferred_with_long_backoff(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, ITEM_520)
    with pytest.raises(Retryforever) as exc:
        await _publish(db, i, fake_http, settings)
    assert exc.value.retry_after_s == IG_RATE_LIMIT_BACKOFF_S
    assert IG_RATE_LIMIT_BACKOFF_S >= 3600


async def test_message_only_body_still_matches(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "User is performing too many actions"}})
    with pytest.raises(Retryforever):
        await _publish(db, i, fake_http, settings)


@pytest.mark.parametrize("code", [4, 9, 17, 32, 613, 80002])
async def test_graph_throttle_codes_defer(db, settings, fake_http, code):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "x", "code": code}})
    with pytest.raises(Retryforever):
        await _publish(db, i, fake_http, settings)


async def test_publish_limit_subcode_defers(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "limit", "code": 9007,
                                      "error_subcode": 2207042}})
    with pytest.raises(Retryforever):
        await _publish(db, i, fake_http, settings)


async def test_plain_invalid_param_stays_terminal(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "Invalid parameter", "code": 100}})
    with pytest.raises(Terminal):
        await _publish(db, i, fake_http, settings)


async def test_bad_token_stays_terminal(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "Invalid OAuth access token",
                                      "code": 190}})
    with pytest.raises(Terminal):
        await _publish(db, i, fake_http, settings)


async def test_unfetchable_stays_retryable_not_forever(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {
        "message": "Only photo or video can be accepted as media type."}})
    with pytest.raises(Retryable) as exc:
        await _publish(db, i, fake_http, settings)
    assert not isinstance(exc.value, Retryforever)


async def test_container_status_throttle_defers(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond_sequence([
        (200, {"id": "c1"}), (200, {"id": "c2"}), (200, {"id": "car"}),
        (400, {"error": {"message": "x", "code": 17}}),
    ])
    with pytest.raises(Retryforever):
        await _publish(db, i, fake_http, settings)


# ------------------------------------------------------------------ pacing


def _ok_sequence(http, post="P"):
    http.respond_sequence([
        (200, {"id": "c1"}), (200, {"id": "c2"}), (200, {"id": "car"}),
        (200, {"status_code": "FINISHED"}), (200, {"id": post}),
    ])


async def test_second_publish_inside_gap_is_deferred_without_api_calls(
        db, settings, fake_http):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    _ok_sequence(fake_http)
    await _publish(db, a, fake_http, settings)
    calls = len(fake_http.calls)

    with pytest.raises(Retryforever) as exc:
        await _publish(db, b, fake_http, settings)
    assert 0 < exc.value.retry_after_s <= IG_MIN_PUBLISH_GAP_S + 1
    assert len(fake_http.calls) == calls


async def test_publish_allowed_after_gap(db, settings, fake_http, monkeypatch):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    _ok_sequence(fake_http, "P1")
    await _publish(db, a, fake_http, settings)

    monkeypatch.setattr(instagram, "_last_publish_mono",
                        instagram.time.monotonic() - IG_MIN_PUBLISH_GAP_S - 1)
    _ok_sequence(fake_http, "P2")
    assert await _publish(db, b, fake_http, settings) == "P2"


async def test_pacing_survives_restart_via_db(db, settings, fake_http):
    """No in-memory state, but a recent published_at row still paces."""
    a = await _seed_approved(db)
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db.update_fields(a, {"published_at": recent.isoformat()})
    b = await _seed_approved(db)
    with pytest.raises(Retryforever):
        await _publish(db, b, fake_http, settings)
    old = datetime.now(timezone.utc) - timedelta(seconds=IG_MIN_PUBLISH_GAP_S + 5)
    await db.update_fields(a, {"published_at": old.isoformat()})
    _ok_sequence(fake_http)
    assert await _publish(db, b, fake_http, settings) == "P"


async def test_failed_publish_does_not_start_the_gap(db, settings, fake_http):
    a = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "Invalid parameter", "code": 100}})
    with pytest.raises(Terminal):
        await _publish(db, a, fake_http, settings)
    b = await _seed_approved(db)
    _ok_sequence(fake_http)
    assert await _publish(db, b, fake_http, settings) == "P"


async def test_concurrent_publishes_only_one_gets_through(db, settings, fake_http):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    _ok_sequence(fake_http)
    results = await asyncio.gather(
        _publish(db, a, fake_http, settings),
        _publish(db, b, fake_http, settings),
        return_exceptions=True,
    )
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, Retryforever) for r in results) == 1
    assert sum("media_publish" in c.url for c in fake_http.calls) == 1


async def test_dry_run_is_not_paced(db, settings, fake_http):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    assert await publish_carousel(await db.get_item(a), fake_http, db, settings) == f"DRYRUN-{a}"
    assert await publish_carousel(await db.get_item(b), fake_http, db, settings) == f"DRYRUN-{b}"


async def test_pacing_uses_the_most_recent_publish_not_the_oldest(db, settings, fake_http):
    old_item = await _seed_approved(db)
    new_item = await _seed_approved(db)
    now = datetime.now(timezone.utc)
    await db.update_fields(old_item, {"published_at": (now - timedelta(days=2)).isoformat()})
    await db.update_fields(new_item, {"published_at": (now - timedelta(seconds=5)).isoformat()})
    c = await _seed_approved(db)
    with pytest.raises(Retryforever):
        await _publish(db, c, fake_http, settings)


async def test_429_without_code_or_json_is_a_throttle(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(429, "Too Many Requests")
    with pytest.raises(Retryforever) as exc:
        await _publish(db, i, fake_http, settings)
    assert exc.value.retry_after_s == IG_RATE_LIMIT_BACKOFF_S


async def test_policy_block_368_gets_the_day_long_backoff(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "blocked", "code": 368}})
    with pytest.raises(Retryforever) as exc:
        await _publish(db, i, fake_http, settings)
    assert exc.value.retry_after_s == IG_POLICY_BLOCK_BACKOFF_S == 24 * 3600


@pytest.mark.parametrize("error", [
    {"message": "weird", "code": "4"},
    {"message": "weird", "code": [4], "error_subcode": {"x": 1}},
    {"message": "weird", "code": None, "error_subcode": "2207042"},
    {"message": "weird", "code": True},
])
async def test_malformed_error_fields_do_not_crash_and_stay_terminal(
        db, settings, fake_http, error):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": error})
    with pytest.raises(Terminal):
        await _publish(db, i, fake_http, settings)


@pytest.fixture
def alerts():
    sent = []

    async def send(text):
        sent.append(text)

    ig_alerts.send = send
    yield sent


async def test_first_throttle_alerts_once_with_no_exception_text(
        db, settings, fake_http, alerts):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    fake_http.respond(400, ITEM_520)
    for i in (a, b, a):
        with pytest.raises(Retryforever):
            await _publish(db, i, fake_http, settings)
    assert alerts == [f"Instagram rate-limited (item {a}), retrying in 2h"]
    assert "too many actions" not in alerts[0]


async def test_pacing_defer_does_not_alert(db, settings, fake_http, alerts):
    a = await _seed_approved(db)
    b = await _seed_approved(db)
    _ok_sequence(fake_http)
    await _publish(db, a, fake_http, settings)
    with pytest.raises(Retryforever):
        await _publish(db, b, fake_http, settings)
    assert alerts == []


async def test_alert_rearms_after_a_successful_publish(
        db, settings, fake_http, alerts, monkeypatch):
    a = await _seed_approved(db)
    fake_http.respond(400, ITEM_520)
    with pytest.raises(Retryforever):
        await _publish(db, a, fake_http, settings)
    _ok_sequence(fake_http)
    await _publish(db, a, fake_http, settings)
    monkeypatch.setattr(instagram, "_last_publish_mono", None)
    b = await _seed_approved(db)
    await db.update_fields(a, {"published_at": "2000-01-01T00:00:00+00:00"})
    fake_http.respond(400, ITEM_520)
    with pytest.raises(Retryforever):
        await _publish(db, b, fake_http, settings)
    assert len(alerts) == 2


async def test_failing_alert_send_does_not_mask_the_defer(db, settings, fake_http):
    async def boom(text):
        raise RuntimeError("telegram down")

    ig_alerts.send = boom
    i = await _seed_approved(db)
    fake_http.respond(400, ITEM_520)
    with pytest.raises(Retryforever):
        await _publish(db, i, fake_http, settings)
