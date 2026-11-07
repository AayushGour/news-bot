"""Publish a carousel via the Instagram API with Instagram Login.

No Facebook Page is involved — this is the Instagram-Login variant, which works
for a Business or Creator account that exists only on Instagram.

The significant hazard here is **double-posting**. If ``media_publish``
succeeds but the response is lost, a naive retry publishes the carousel twice.
Three defences:

1. Container ids are persisted the moment they are created and reused on retry
   rather than recreated.
2. ``ig_post_id`` is written the instant it returns, before anything else.
3. Before retrying a publish, recent media is checked for a post already
   matching the carousel container.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..config import (
    IG_MIN_PUBLISH_GAP_S,
    IG_POLICY_BLOCK_BACKOFF_S,
    IG_RATE_LIMIT_BACKOFF_S,
)
from ..db import Database, now_iso
from ..errors import PlatformThrottled, Retryable, Retryforever, Terminal
from ..faults import ig_alerts
from ..models import Item
from .media_host import current_urls

log = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com/v23.0"
#: Meta's own documentation contradicts itself: the Rate Limit section says 100
#: API-published posts per rolling 24 hours, while the Carousel Limitations
#: block on the same page says 50. This pipeline publishes carousels
#: exclusively, so it takes the lower number — exceeding the real cap fails at
#: publish time with an opaque error, and posting fewer costs nothing.
DAILY_POST_LIMIT = 50


async def _record_publication(db: Database, item: Item, post_id: str) -> None:
    """Write the post id and append it to the permanent publish history.

    ``ig_post_id`` is the double-post guard for the *current* attempt, which is
    why a requeue has to clear it: leave it set and a legitimate redo returns
    the old post instead of publishing the new deck. That makes it useless as a
    record of what has actually gone out — item 84 published, was requeued from
    the dashboard, and its row then claimed it had never been posted at all
    while the carousel sat on the account.

    ``publish_log`` is that record. No stage clears it, so the dashboard can
    warn that approving again puts a second carousel on a real account.
    """
    history = [*(item.publish_log or []), {"ig_post_id": post_id, "at": now_iso()}]
    await db.update_fields(item.id, {"ig_post_id": post_id, "publish_log": history})


#: Graph/IG throttle codes (Meta Graph API error reference): 4 app limit, 9 and
#: 80002 Instagram business-use-case limit, 17 user limit, 32 page limit, 613
#: custom limit. Subcode 2207042 is
#: the daily content-publishing cap. 190 (bad token) is deliberately absent.
_THROTTLE_CODES = frozenset({4, 9, 17, 32, 80002, 613})
#: Abuse/disallowed-action block: lasts days, so it gets its own long backoff.
_POLICY_BLOCK_CODES = frozenset({368})
_THROTTLE_ALERT_KEY = "ig-throttle"
_THROTTLE_SUBCODES = frozenset({2207042})
#: Fallback when only the message survives (item 520 logged just the text).
_THROTTLE_PHRASES = ("too many actions", "request limit", "rate limit")

#: Serialises the pacing check with the publish it guards, so two concurrent
#: workers cannot both see "gap elapsed". Contenders defer rather than wait.
_publish_lock = asyncio.Lock()
#: Monotonic time of this process's last publish; covers the window between
#: media_publish returning and the worker writing published_at to the DB.
_last_publish_mono: float | None = None


async def _pacing_remaining_s(db: Database) -> float:
    """Seconds still to wait before another publish is allowed (0 = go)."""
    remaining = 0.0
    if _last_publish_mono is not None:
        remaining = IG_MIN_PUBLISH_GAP_S - (time.monotonic() - _last_publish_mono)
    last = await db.last_published_at()
    if last is not None:
        since = (datetime.now(timezone.utc) - last).total_seconds()
        remaining = max(remaining, IG_MIN_PUBLISH_GAP_S - since)
    return max(remaining, 0.0)


async def publish_carousel(item: Item, http: Any, db: Database, settings: Any) -> str:
    """Publish the item's slides as one carousel. Returns the Instagram post id."""
    if item.ig_post_id:
        log.info("item %s already published as %s", item.id, item.ig_post_id)
        return item.ig_post_id

    urls = item.media_urls or []
    if not urls:
        raise Retryable("no media URLs to publish")
    if len(urls) > 10:
        raise Terminal(f"carousel has {len(urls)} slides; Instagram allows 10")

    if await db.published_since(24) >= DAILY_POST_LIMIT:
        raise Terminal(
            f"Instagram's {DAILY_POST_LIMIT} posts/24h limit reached; try later"
        )

    if settings.dry_run:
        fake = f"DRYRUN-{item.id}"
        log.info("DRY_RUN: would publish %d slides for item %s", len(urls), item.id)
        await _record_publication(db, item, fake)
        return fake

    if not (settings.ig_user_id and settings.ig_access_token):
        raise Terminal("Instagram is not configured; cannot publish")

    global _last_publish_mono
    if _publish_lock.locked():
        raise Retryforever("another publish is in flight; pacing",
                           retry_after_s=IG_MIN_PUBLISH_GAP_S)
    async with _publish_lock:
        wait = await _pacing_remaining_s(db)
        if wait > 0:
            raise Retryforever(
                f"pacing: last publish under {IG_MIN_PUBLISH_GAP_S}s ago",
                retry_after_s=int(wait) + 1)
        # Re-address before probing. The URLs were built when the item was
        # approved, and behind a quick tunnel the public hostname is withdrawn
        # after about a day — so an item approved before a rotation and published
        # after it holds URLs that can never become valid again. Retrying those
        # would defer forever; the objects are still in storage under the same
        # keys, so they are simply re-addressed against whatever host is live now.
        fresh = current_urls(item.id, urls, settings)
        if fresh != urls:
            await db.update_fields(item.id, {"media_urls": fresh})
            urls = fresh

        # Before handing Meta a list of URLs it will fetch from the public
        # internet, confirm they are actually being served.
        await _verify_media_reachable(urls, http)

        try:
            child_ids = await _ensure_children(item, urls, http, db, settings)
            carousel_id = await _ensure_carousel(item, child_ids, http, db, settings)
            post_id = await _publish(item, carousel_id, http, db, settings)
        except PlatformThrottled as exc:
            # Fixed text only: no exception detail reaches the operator chat.
            await ig_alerts.notify_once(
                _THROTTLE_ALERT_KEY,
                f"Instagram rate-limited (item {item.id}), retrying in "
                f"{exc.retry_after_s / 3600:g}h")
            raise
        ig_alerts.reset(_THROTTLE_ALERT_KEY)
        _last_publish_mono = time.monotonic()
        return post_id


# ----------------------------------------------------------------- internals


#: Meta's wording when it could not fetch the URL at all. It names a media
#: type, so it reads as "the file is the wrong format" and sends debugging at
#: the renderer — when what actually happened is that the host serving the
#: image was unreachable.
_UNFETCHABLE = "only photo or video can be accepted as media type"


async def _verify_media_reachable(urls: list[str], http: Any) -> None:
    """Confirm the media host is serving images before Meta is asked to fetch.

    Meta fetches these URLs from the public internet. When the host is down its
    error says nothing about the host: item 100 died on "Only photo or video
    can be accepted as media type" while every slide sat correctly in MinIO —
    the ephemeral tunnel in front of it had expired, so Meta fetched nothing
    and guessed at the media type.

    One URL is enough: the failure being guarded against is the host being
    gone, not an individual object, and eight probes would cost eight
    round-trips per publish to learn the same fact.
    """
    probe = urls[0]
    host = urlparse(probe).netloc or probe
    # Retryforever, not Retryable: the media host is down for every item, so
    # charging this one an attempt for it turns an outage into permanent
    # content loss. Items 106 and 109 were killed exactly that way — three
    # attempts against a tunnel hostname Cloudflare had withdrawn, then
    # `failed`, while both decks sat rendered and correct on disk.
    try:
        response = await http.get(probe, timeout=30, follow_redirects=True)
    except Exception as exc:
        raise Retryforever(
            f"media host {host} is unreachable ({type(exc).__name__}); "
            f"Instagram fetches slides from there, so publishing cannot start"
        ) from exc

    if response.status_code != 200:
        raise Retryforever(
            f"media host {host} returned HTTP {response.status_code} for a "
            f"slide; Instagram would see the same and refuse the upload"
        )

    kind = (response.headers.get("content-type") or "").split(";")[0].strip()
    if not kind.startswith("image/"):
        raise Retryforever(
            f"media host {host} served {kind or 'no content-type'} instead of "
            f"an image; Instagram rejects anything that is not photo or video"
        )


async def _ensure_children(
    item: Item, urls: list[str], http: Any, db: Database, settings: Any
) -> list[str]:
    """Create one container per slide, reusing any already created."""
    existing = list(item.ig_child_ids or [])
    if len(existing) == len(urls):
        log.info("item %s reusing %d child containers", item.id, len(existing))
        return existing

    child_ids = existing
    for url in urls[len(existing):]:
        response = await _post(http, f"{GRAPH}/{settings.ig_user_id}/media", {
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": settings.ig_access_token,
        })
        child_ids.append(str(response["id"]))
        # Persist after every single creation, so an interruption resumes here
        # instead of creating duplicates.
        await db.update_fields(item.id, {"ig_child_ids": child_ids})
    return child_ids


async def _ensure_carousel(
    item: Item, child_ids: list[str], http: Any, db: Database, settings: Any
) -> str:
    if item.ig_carousel_id:
        log.info("item %s reusing carousel container %s", item.id, item.ig_carousel_id)
        return item.ig_carousel_id

    response = await _post(http, f"{GRAPH}/{settings.ig_user_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": item.caption or "",
        "access_token": settings.ig_access_token,
    })
    carousel_id = str(response["id"])
    await db.update_fields(item.id, {"ig_carousel_id": carousel_id})
    return carousel_id


#: Meta: "We recommend querying a container's status once per minute, for no
#: more than 5 minutes." Polled faster than that because a carousel of already
#: uploaded images is usually ready in seconds, and an operator is waiting.
CONTAINER_POLL_S = 5
CONTAINER_TIMEOUT_S = 300


async def _await_container(
    carousel_id: str, http: Any, settings: Any
) -> None:
    """Block until Instagram has finished building the container.

    Containers are assembled asynchronously. Publishing one that is still
    IN_PROGRESS returns "Media ID is not available" — which reads like a bad id
    rather than a race, and cost two real posts before this existed.
    """
    # Bounded by attempts, not by accumulated sleep. Deriving the bound from
    # the interval meant a zero interval never advanced the counter and the
    # loop ran forever — termination must not depend on a tunable's value.
    attempts = max(1, CONTAINER_TIMEOUT_S // max(CONTAINER_POLL_S, 1))
    for attempt in range(attempts):
        response = await http.get(
            f"{GRAPH}/{carousel_id}",
            params={"fields": "status_code,status",
                    "access_token": settings.ig_access_token},
        )
        if response.status_code != 200:
            _raise_if_throttled(response, response.status_code,
                                _error_text(response))
            raise Retryable(f"container status unreadable: {_error_text(response)}")

        body = response.json()
        state = body.get("status_code")
        if state in ("FINISHED", "PUBLISHED"):
            log.info("container %s ready after %d poll(s)", carousel_id, attempt + 1)
            return
        if state == "ERROR":
            # Terminal: Meta could not build it, and retrying the same
            # container will never succeed.
            raise Terminal(f"container {carousel_id} failed: "
                           f"{body.get('status') or 'no detail'}")
        if state == "EXPIRED":
            raise Terminal(f"container {carousel_id} expired unpublished")

        await asyncio.sleep(CONTAINER_POLL_S)

    raise Retryable(
        f"container {carousel_id} still not ready after {attempts} polls"
    )


async def _publish(
    item: Item, carousel_id: str, http: Any, db: Database, settings: Any
) -> str:
    # If a previous attempt published but lost the response, do not post again.
    if item.attempts:
        already = await _find_existing_post(carousel_id, http, settings)
        if already:
            log.warning("item %s was already published as %s; not reposting",
                        item.id, already)
            await _record_publication(db, item, already)
            return already

    await _await_container(carousel_id, http, settings)

    response = await _post(http, f"{GRAPH}/{settings.ig_user_id}/media_publish", {
        "creation_id": carousel_id,
        "access_token": settings.ig_access_token,
    })
    post_id = str(response["id"])
    await _record_publication(db, item, post_id)
    log.info("published item %s as %s", item.id, post_id)
    return post_id


async def _find_existing_post(carousel_id: str, http: Any, settings: Any) -> str | None:
    """Look for a post already created from this container."""
    try:
        response = await http.get(
            f"{GRAPH}/{settings.ig_user_id}/media",
            params={"fields": "id", "limit": 5,
                    "access_token": settings.ig_access_token},
            timeout=30,
        )
        if response.status_code != 200:
            return None
        for entry in response.json().get("data", []):
            if str(entry.get("id")) == str(carousel_id):
                return str(entry["id"])
    except Exception as exc:  # a failed check must not block the retry
        log.warning("could not check for an existing post: %s", exc)
    return None


async def _post(http: Any, url: str, data: dict) -> dict:
    try:
        response = await http.post(url, data=data, timeout=120)
    except Exception as exc:
        raise Retryable(f"instagram request failed: {exc}") from exc

    status = response.status_code
    if status >= 500:
        raise Retryable(f"instagram {status}: {_error_text(response)}")
    if status >= 400:
        detail = _error_text(response)
        _raise_if_throttled(response, status, detail)
        if _UNFETCHABLE in detail.lower():
            # Meta names a media type, but this is also exactly what it says
            # when it could not fetch the URL at all. Reported as Terminal it
            # killed item 100 permanently over an expired tunnel, and pointed
            # debugging at the renderer instead of the host. The preflight
            # above normally catches this first; if the host dies between that
            # check and this call, the item must still be able to recover.
            raise Retryable(
                f"instagram {status}: {detail} — this is also what Meta "
                f"returns when it cannot fetch the image URL at all; check "
                f"that the media host is publicly reachable"
            )
        # Other 4xx will not succeed on retry — bad token, bad request.
        raise Terminal(f"instagram {status}: {detail}")

    body = response.json()
    if "id" not in body:
        raise Retryable(f"instagram response had no id: {str(body)[:200]}")
    return body


def _error_fields(response: Any) -> dict:
    try:
        error = response.json().get("error", {})
        return error if isinstance(error, dict) else {}
    except Exception:
        return {}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _throttle_backoff_s(response: Any, status: int, detail: str) -> int | None:
    """Backoff for a throttle/block response, or None if it is not one."""
    error = _error_fields(response)
    code = _int_or_none(error.get("code"))
    if code in _POLICY_BLOCK_CODES:
        return IG_POLICY_BLOCK_BACKOFF_S
    if (status == 429 or code in _THROTTLE_CODES
            or _int_or_none(error.get("error_subcode")) in _THROTTLE_SUBCODES
            or any(p in detail.lower() for p in _THROTTLE_PHRASES)):
        return IG_RATE_LIMIT_BACKOFF_S
    return None


def _raise_if_throttled(response: Any, status: int, detail: str) -> None:
    """Throttling is the account's state, not the item's: defer, spend nothing."""
    backoff = _throttle_backoff_s(response, status, detail)
    if backoff is not None:
        raise PlatformThrottled(
            f"instagram {status}: {detail} (rate limited; backing off)",
            retry_after_s=backoff,
        )


def _error_text(response: Any) -> str:
    try:
        error = response.json().get("error", {})
        return str(error.get("message") or error)[:300]
    except Exception:
        return (getattr(response, "text", "") or "")[:300]
