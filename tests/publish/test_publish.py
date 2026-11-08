from dataclasses import replace

import pytest

from pipeline.errors import Retryable, Retryforever, Terminal
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


@pytest.fixture(autouse=True)
def media_host_serves_images(fake_http):
    """Publishing now GETs one slide before handing Meta the URLs.

    Registered against the URL map rather than the response queue: the queue
    models Meta's side of the conversation, and letting an unrelated probe
    consume from it would shift every queued reply by one.
    """
    fake_http.respond_for("cdn.example", "", status=200)


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """Poll and retry pauses are zero; a guard miss must not cost real minutes."""
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)


def _ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S+0000")


NOT_READY = {"error": {"message": "Media ID is not available", "code": 9007,
                       "error_subcode": 2207027}}


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


async def test_upload_without_storage_config_is_terminal(settings):
    with pytest.raises(Terminal, match="object storage is not configured"):
        await upload(["/tmp/a.png"], 1, replace(settings, dry_run=False))


async def test_loopback_public_base_is_rejected_before_upload(settings):
    """Instagram fetches these URLs from its own servers, so localhost fails
    there with an opaque media error. Catch it here with a useful message."""
    live = replace(settings, dry_run=False, r2_bucket="b",
                   r2_public_base="http://localhost:9000/media")
    with pytest.raises(Terminal, match="cannot reach"):
        await upload(["/tmp/a.png"], 1, live)


def test_endpoint_url_prefers_explicit_s3_endpoint():
    """MinIO, B2, Wasabi and real S3 all work via S3_ENDPOINT."""
    from pipeline.publish.media_host import endpoint_url

    assert endpoint_url(replace(settings_stub(), s3_endpoint="http://localhost:9000/")) \
        == "http://localhost:9000"
    assert endpoint_url(replace(settings_stub(), r2_account_id="acct")) \
        == "https://acct.r2.cloudflarestorage.com"


def settings_stub():
    from pipeline.config import Settings

    return Settings.load(env={})


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
        (200, {"id": "carousel1"}),
        # Instagram builds containers asynchronously; publishing one that is
        # still IN_PROGRESS returns "Media ID is not available".
        (200, {"status_code": "FINISHED"}),
        (200, {"id": "POST1"}),
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
    fake_http.respond_sequence([
        (200, {"status_code": "FINISHED"}),
        (200, {"id": "POST1"}),
    ])

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
    fake_http.respond_for("/media", {"data": [
        {"id": "POST9", "caption": "A  CAPTION\n", "timestamp": _ago(60)}]})

    post_id = await publish_carousel(await db.get_item(i), fake_http, db, live(settings))

    assert post_id == "POST9"
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


# --- the container must be ready before publishing --------------------------
#
# Two real posts were lost to this. The carousel container was created and
# media_publish called one second later, returning 400 "Media ID is not
# available" — which reads like a bad id rather than a race. Meta builds
# containers asynchronously and documents polling status_code first.

async def test_publish_waits_for_the_container_to_finish(db, settings, fake_http,
                                                         monkeypatch):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond_sequence([
        (200, {"status_code": "IN_PROGRESS"}),
        (200, {"status_code": "IN_PROGRESS"}),
        (200, {"status_code": "FINISHED"}),
        (200, {"id": "POST1"}),
    ])

    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "POST1"

    publishes = [c for c in fake_http.calls if c.url.endswith("/media_publish")]
    assert len(publishes) == 1, "published exactly once, after it was ready"


async def test_a_container_that_errors_is_terminal(db, settings, fake_http,
                                                   monkeypatch):
    """Retrying a container Meta failed to build never succeeds."""
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond(200, {"status_code": "ERROR", "status": "unsupported format"})

    with pytest.raises(Terminal, match="failed"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))

    assert not [c for c in fake_http.calls if c.url.endswith("/media_publish")]


async def test_an_expired_container_is_terminal(db, settings, fake_http,
                                                monkeypatch):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond(200, {"status_code": "EXPIRED"})

    with pytest.raises(Terminal, match="expired"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_a_container_stuck_in_progress_is_retryable_not_terminal(
    db, settings, fake_http, monkeypatch
):
    """Still building after the window is worth another attempt later; failing
    it permanently would throw away a deck over a slow build."""
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)
    monkeypatch.setattr(ig, "CONTAINER_TIMEOUT_S", 1)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond(200, {"status_code": "IN_PROGRESS"})

    with pytest.raises(Retryable, match="not ready"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_an_already_published_container_proceeds(db, settings, fake_http,
                                                       monkeypatch):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond_sequence([
        (200, {"status_code": "PUBLISHED"}),
        (200, {"data": [{"id": "POST1", "caption": "A caption",
                         "timestamp": _ago(30)}]}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "POST1"
    assert not _publishes(fake_http), "a PUBLISHED container is never re-published"


async def test_a_published_container_with_no_matching_post_defers_never_publishes(
    db, settings, fake_http
):
    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond_sequence([
        (200, {"status_code": "PUBLISHED"}),
        (200, {"data": []}),
    ])
    with pytest.raises(Retryable, match="PUBLISHED"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert not _publishes(fake_http)


async def test_guard_matches_across_unicode_normalization(db, settings, fake_http):
    i = await _seed_ready(db, caption="Caf\u00e9 news")  # precomposed
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (200, {"data": [{"id": "P2", "caption": "Cafe\u0301 news",  # decomposed
                         "timestamp": _ago(5)}]}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "P2"


async def test_the_wait_terminates_even_with_a_zero_poll_interval(
    db, settings, fake_http, monkeypatch
):
    """The bound is a number of attempts. Deriving it from accumulated sleep
    meant a zero interval never advanced the counter and the loop hung."""
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "CONTAINER_POLL_S", 0)

    i = await _seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond(200, {"status_code": "IN_PROGRESS"})

    with pytest.raises(Retryable, match="not ready"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


# ------------------------------------------------- media host reachability


async def test_an_unreachable_media_host_does_not_spend_an_attempt(db, settings, fake_http):
    """Retryforever, not Retryable. The media host is down for every item, so
    charging this one an attempt for it turns an outage into permanent content
    loss — items 106 and 109 were killed exactly that way, three attempts
    against a withdrawn tunnel hostname and then `failed`, while both decks sat
    rendered and correct on disk."""
    i = await _seed_approved(db)
    fake_http.raise_on_request = ConnectionError("no route to host")
    with pytest.raises(Retryforever):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_a_media_host_http_error_does_not_spend_an_attempt(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond_for("cdn.example", "", status=502)
    with pytest.raises(Retryforever):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_an_unreachable_media_host_is_retryable_and_says_so(db, settings, fake_http):
    """Item 100 died on "Only photo or video can be accepted as media type"
    while every slide sat correctly in MinIO — the tunnel in front of it had
    expired. The error must name the host, not the media type, and must not be
    terminal: the tunnel comes back."""
    i = await _seed_approved(db)
    fake_http.raise_on_request = ConnectionError("no route to host")
    with pytest.raises(Retryable, match="unreachable"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_a_media_host_serving_html_is_retryable(db, settings, fake_http):
    """A tunnel that answers with an error page, not an image."""
    i = await _seed_approved(db)
    fake_http.respond_for("cdn.example", "<html>404</html>", status=200)
    fake_http._by_url["cdn.example"].headers = {"content-type": "text/html"}
    with pytest.raises(Retryable, match="instead of an image"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_a_media_host_returning_404_is_retryable(db, settings, fake_http):
    i = await _seed_approved(db)
    fake_http.respond_for("cdn.example", "", status=404)
    with pytest.raises(Retryable, match="HTTP 404"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_no_container_is_created_when_the_host_is_down(db, settings, fake_http):
    """The whole point of a preflight: do not leave orphaned containers on
    Meta's side for images it can never fetch."""
    i = await _seed_approved(db)
    fake_http.respond_for("cdn.example", "", status=503)
    with pytest.raises(Retryable):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert not [c for c in fake_http.calls if c.method == "POST"]


async def test_metas_unfetchable_wording_is_retryable_not_terminal(db, settings, fake_http):
    """If the host dies between the preflight and the upload, Meta returns a
    400 whose text names a media type. Treated as terminal it killed the item
    permanently over a transient outage."""
    i = await _seed_approved(db)
    fake_http.respond_for("graph.instagram.com", {
        "error": {"message": "Only photo or video can be accepted as media type."}
    }, status=400)
    with pytest.raises(Retryable, match="publicly reachable"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


async def test_other_4xx_from_meta_stays_terminal(db, settings, fake_http):
    """Only the unfetchable-URL wording is reclassified; a bad token must not
    sit in the retry queue pretending to be transient."""
    i = await _seed_approved(db)
    fake_http.respond_for("graph.instagram.com", {
        "error": {"message": "Invalid OAuth access token."}
    }, status=400)
    with pytest.raises(Terminal, match="Invalid OAuth"):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))


# --- T337: Meta's not-ready-yet race after FINISHED + the double-post guard --


def _publishes(fake_http):
    return [c for c in fake_http.calls if c.url.endswith("/media_publish")]


async def _seed_ready(db, **kw):
    return await _seed_approved(db, ig_child_ids=["c1", "c2"],
                                ig_carousel_id="car1", **kw)


async def test_not_ready_400_after_finished_is_retried_in_place(
    db, settings, fake_http, monkeypatch
):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)
    i = await _seed_ready(db)
    fake_http.respond_sequence([
        (200, {"status_code": "FINISHED"}),
        (400, NOT_READY), (400, NOT_READY), (200, {"id": "POST1"}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "POST1"
    assert len(_publishes(fake_http)) == 3
    assert (await db.get_item(i)).ig_post_id == "POST1"


@pytest.mark.parametrize("body", [
    {"error": {"message": "x", "code": 9007}},
    {"error": {"message": "x", "code": 1, "error_subcode": 2207027}},
    {"error": {"message": "The media is not ready for publishing, please wait"}},
    {"error": {"message": "Media ID is not available"}},
])
async def test_each_not_ready_signal_is_recognised(
    db, settings, fake_http, monkeypatch, body
):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)
    i = await _seed_ready(db)
    fake_http.respond_sequence([
        (200, {"status_code": "FINISHED"}), (400, body), (200, {"id": "P"})])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "P"


async def test_persistent_not_ready_is_retryable_after_exactly_n_tries(
    db, settings, fake_http, monkeypatch
):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)
    i = await _seed_ready(db)
    fake_http.respond_for("media_publish", NOT_READY, status=400)
    fake_http.respond(200, {"status_code": "FINISHED"})
    with pytest.raises(Retryable) as exc:
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert not isinstance(exc.value, Terminal)
    assert len(_publishes(fake_http)) == ig.PUBLISH_NOT_READY_TRIES
    # The container survives for the worker's later retry.
    assert (await db.get_item(i)).ig_carousel_id == "car1"


async def test_not_ready_waits_between_tries(db, settings, fake_http, monkeypatch):
    import pipeline.publish.instagram as ig
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(ig.asyncio, "sleep", fake_sleep)
    i = await _seed_ready(db)
    fake_http.respond_for("media_publish", NOT_READY, status=400)
    fake_http.respond(200, {"status_code": "FINISHED"})
    with pytest.raises(Retryable):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert slept == [ig.PUBLISH_NOT_READY_GAP_S] * (ig.PUBLISH_NOT_READY_TRIES - 1)


async def test_throttle_on_media_publish_is_not_retried_in_place(
    db, settings, fake_http, monkeypatch
):
    from pipeline.errors import PlatformThrottled
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)
    i = await _seed_ready(db)
    fake_http.respond_for("media_publish", {"error": {
        "message": "x", "code": 9, "error_subcode": 2207042}}, status=400)
    fake_http.respond(200, {"status_code": "FINISHED"})
    with pytest.raises(PlatformThrottled):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert len(_publishes(fake_http)) == 1
    # Throttled means refused, so there is nothing to look for.
    assert not [c for c in fake_http.calls
                if c.method == "GET" and c.url.endswith("/media")]


async def test_other_4xx_on_media_publish_is_terminal_without_retry(
    db, settings, fake_http, monkeypatch
):
    import pipeline.publish.instagram as ig
    monkeypatch.setattr(ig, "PUBLISH_NOT_READY_GAP_S", 0)
    i = await _seed_ready(db)
    fake_http.respond_for("media_publish", {"error": {
        "message": "Invalid parameter", "code": 100}}, status=400)
    fake_http.respond(200, {"status_code": "FINISHED"})
    with pytest.raises(Terminal):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert len(_publishes(fake_http)) == 1


async def test_guard_ignores_posts_with_other_captions_or_old_timestamps(
    db, settings, fake_http
):
    i = await _seed_ready(db)
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (200, {"data": [
            {"id": "OTHER", "caption": "something else", "timestamp": _ago(5)},
            {"id": "OLD", "caption": "A caption", "timestamp": _ago(3 * 86400)},
        ]}),
        (200, {"status_code": "FINISHED"}), (200, {"id": "NEW"}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "NEW"
    assert len(_publishes(fake_http)) == 1


async def test_guard_skips_posts_already_in_publish_log(db, settings, fake_http):
    """A requeued item re-publishes the same caption legitimately."""
    i = await _seed_ready(db, publish_log=[{"ig_post_id": "PREV", "at": "t"}])
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (200, {"data": [{"id": "PREV", "caption": "A caption",
                         "timestamp": _ago(60)}]}),
        (200, {"status_code": "FINISHED"}), (200, {"id": "NEW"}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "NEW"


async def test_guard_tolerates_api_failure(db, settings, fake_http):
    i = await _seed_ready(db)
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (500, {"error": {"message": "down"}}),
        (200, {"status_code": "FINISHED"}), (200, {"id": "NEW"}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "NEW"


async def test_ambiguous_publish_failure_checks_for_the_post_before_giving_up(
    db, settings, fake_http
):
    """media_publish 5xx may hide a success: look before surfacing Retryable."""
    i = await _seed_ready(db)
    fake_http.respond_sequence([
        (200, {"status_code": "FINISHED"}),
        (503, {"error": {"message": "unavailable"}}),
        (200, {"data": [{"id": "POST7", "caption": "A caption",
                         "timestamp": _ago(5)}]}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "POST7"
    assert len(_publishes(fake_http)) == 1


async def test_ambiguous_publish_failure_without_a_post_stays_retryable(
    db, settings, fake_http
):
    i = await _seed_ready(db)
    fake_http.respond_sequence([
        (200, {"status_code": "FINISHED"}),
        (503, {"error": {"message": "unavailable"}}),
        (200, {"data": []}),
    ])
    with pytest.raises(Retryable):
        await publish_carousel(await db.get_item(i), fake_http, db, live(settings))
    assert len(_publishes(fake_http)) == 1


async def test_guard_never_matches_on_an_empty_caption(db, settings, fake_http):
    """Every caption-less post would otherwise look like the lost one."""
    i = await _seed_ready(db, caption="")
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (200, {"data": [{"id": "OTHER", "timestamp": _ago(5)}]}),
        (200, {"status_code": "FINISHED"}), (200, {"id": "NEW"}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "NEW"


async def test_guard_treats_an_unparseable_timestamp_as_in_window(
    db, settings, fake_http
):
    """Errs toward adopting: a missed duplicate costs more than a skipped post."""
    i = await _seed_ready(db)
    await db.record_failure(i, "x")
    fake_http.respond_sequence([
        (200, {"data": [{"id": "P3", "caption": "A caption", "timestamp": "??"}]}),
    ])
    assert await publish_carousel(
        await db.get_item(i), fake_http, db, live(settings)) == "P3"
