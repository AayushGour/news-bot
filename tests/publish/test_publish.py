from dataclasses import replace

import pytest

from pipeline.errors import Retryable, Terminal
from pipeline.models import Status
from pipeline.publish.instagram import DAILY_POST_LIMIT, publish_carousel
from pipeline.publish.media_host import object_key, upload
from pipeline.publish.tokens import (
    ALERT_FAILED,
    REFRESH_AFTER_DAYS,
    TokenStore,
    refresh_token_if_due,
)

LIVE = dict(
    dry_run=False, ig_user_id="17841400000000000", ig_access_token="IGT",
    r2_bucket="bucket", r2_public_base="https://cdn.example",
    r2_account_id="acct", r2_access_key="k", r2_secret_key="s",
)


def live(settings, **over):
    return replace(settings, **{**LIVE, **over})


async def _seed_approved(db, **fields):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=1, raw_text="news")
    base = {
        "caption": "A caption",
        "rendered_paths": ["/tmp/a.png", "/tmp/b.png"],
        "media_urls": ["https://cdn.example/a.png", "https://cdn.example/b.png"],
    }
    base.update(fields)
    await db.transition(i, Status.APPROVED, base)
    return i


# ---------------------------------------------------------------- media host


def test_object_key_is_namespaced_per_item():
    assert object_key(42, 0, "/tmp/item42_slide_01.png") == "items/42/item42_slide_01.png"


async def test_dry_run_upload_makes_no_network_call(settings):
    urls = await upload(["/tmp/a.png", "/tmp/b.png"], 7, settings)
    assert len(urls) == 2
    assert all(u.startswith("https://dry-run.invalid/items/7/") for u in urls)


async def test_upload_without_slides_is_retryable(settings):
    with pytest.raises(Retryable):
        await upload([], 1, settings)


async def test_upload_without_r2_config_is_terminal(settings):
    with pytest.raises(Terminal, match="R2 is not configured"):
        await upload(["/tmp/a.png"], 1, replace(settings, dry_run=False))


# ----------------------------------------------------------------- instagram


async def test_dry_run_publish_makes_no_network_calls(db, settings, fake_http):
    i = await _seed_approved(db)
    post_id = await publish_carousel(await db.get_item(i), fake_http, db, settings)

    assert post_id == f"DRYRUN-{i}"
    assert fake_http.calls == []
    assert (await db.get_item(i)).ig_post_id == post_id


async def test_full_publish_sequence(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond_sequence([
        (200, {"id": "child1"}), (200, {"id": "child2"}),
        (200, {"id": "carousel1"}), (200, {"id": "POST1"}),
    ])

    post_id = await publish_carousel(await db.get_item(i), fake_http, db, live(settings))

    assert post_id == "POST1"
    item = await db.get_item(i)
    assert item.ig_child_ids == ["child1", "child2"]
    assert item.ig_carousel_id == "carousel1"
    assert item.ig_post_id == "POST1"


async def test_child_ids_are_persisted_as_they_are_created(db, settings, fake_http):
    """An interruption must resume, not restart."""
    i = await _seed_approved(db)
    fake_http.respond_sequence([
        (200, {"id": "child1"}),
        (500, {"error": {"message": "boom"}}),
    ])

    with pytest.raises(Retryable):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))

    assert (await db.get_item(i)).ig_child_ids == ["child1"]


async def test_retry_reuses_persisted_containers_instead_of_reposting(
    db, settings, fake_http
):
    """The core double-post guard."""
    i = await _seed_approved(
        db, ig_child_ids=["child1", "child2"], ig_carousel_id="carousel1"
    )
    fake_http.respond(200, {"id": "POST1"})

    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)
    ) == "POST1"

    creates = [c for c in fake_http.calls if c.url.endswith("/media")]
    assert creates == [], "no container may be recreated on retry"


async def test_already_published_item_is_never_republished(db, settings, fake_http):
    i = await _seed_approved(db, ig_post_id="POST1")
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)
    ) == "POST1"
    assert fake_http.calls == []


async def test_retry_checks_for_an_existing_post_before_publishing(
    db, settings, fake_http
):
    """If a previous publish succeeded but its response was lost, do not repost."""
    i = await _seed_approved(
        db, ig_child_ids=["c1", "c2"], ig_carousel_id="carousel1"
    )
    await db.record_failure(i, "lost response")  # attempts = 1
    fake_http.respond_for("/media", {"data": [{"id": "carousel1"}]})

    post_id = await publish_carousel(await db.get_item(i), fake_http, db, live(settings))

    assert post_id == "carousel1"
    assert not any("media_publish" in c.url for c in fake_http.calls)


async def test_instagram_4xx_is_terminal_not_retryable(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(400, {"error": {"message": "Invalid media URL"}})

    with pytest.raises(Terminal, match="Invalid media URL"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_instagram_5xx_is_retryable(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond(503, {"error": {"message": "try later"}})

    with pytest.raises(Retryable):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_more_than_ten_slides_is_terminal(db, settings, fake_http):
    i = await _seed_approved(db, media_urls=[f"https://cdn.example/{n}.png"
                                             for n in range(11)])
    with pytest.raises(Terminal, match="Instagram allows 10"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_daily_rate_limit_is_terminal(db, settings, fake_http, monkeypatch):
    i = await _seed_approved(db)

    async def at_limit(hours=24):
        return DAILY_POST_LIMIT

    monkeypatch.setattr(db, "published_since", at_limit)
    with pytest.raises(Terminal, match="posts/24h limit"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_missing_media_urls_is_retryable(db, settings, fake_http):
    i = await _seed_approved(db, media_urls=[])
    with pytest.raises(Retryable, match="no media URLs"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


# --------------------------------------------------------------------- tokens


def test_token_store_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    assert store.current_token("fallback") == "fallback"

    store.save("NEWTOKEN", expires_in=5184000)
    assert store.current_token("fallback") == "NEWTOKEN"
    assert store.age_days() < 0.01


async def test_refresh_is_skipped_when_token_is_young(settings, fake_http, tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.save("TOK")

    assert await refresh_token_if_due(
        fake_http, live(settings), store, age_days=10
    ) is False
    assert fake_http.calls == []


async def test_refresh_happens_once_due(settings, fake_http, tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.save("OLD")
    fake_http.respond(200, {"access_token": "FRESH", "expires_in": 5184000})

    assert await refresh_token_if_due(
        fake_http, live(settings), store, age_days=REFRESH_AFTER_DAYS + 1
    ) is True
    assert store.current_token("x") == "FRESH"


async def test_refresh_failure_alerts_the_operator(settings, fake_http, tmp_path):
    """Silent token expiry stops publishing with no visible symptom."""
    store = TokenStore(tmp_path / "t.json")
    store.save("OLD")
    fake_http.respond(400, {"error": {"message": "expired"}})
    alerts = []

    async def notify(text):
        alerts.append(text)

    result = await refresh_token_if_due(
        fake_http, live(settings), store, notify=notify, age_days=58
    )

    assert result is False
    assert alerts and ALERT_FAILED.split("\n")[0] in alerts[0]
    assert store.current_token("x") == "OLD", "the working token must be kept"


async def test_refresh_network_error_alerts_rather_than_raising(
    settings, fake_http, tmp_path
):
    store = TokenStore(tmp_path / "t.json")
    store.save("OLD")
    fake_http.raise_on_request = ConnectionError("no route")
    alerts = []

    assert await refresh_token_if_due(
        fake_http, live(settings), store,
        notify=lambda t: _append(alerts, t), age_days=58,
    ) is False
    assert alerts


async def test_dry_run_does_not_refresh(settings, fake_http, tmp_path):
    store = TokenStore(tmp_path / "t.json")
    assert await refresh_token_if_due(fake_http, settings, store, age_days=99) is False
    assert fake_http.calls == []


async def _append(sink, text):
    sink.append(text)
