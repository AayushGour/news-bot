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

import logging
from typing import Any

from ..db import Database
from ..errors import Retryable, Terminal
from ..models import Item

log = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com/v23.0"
#: Meta's own documentation contradicts itself: the Rate Limit section says 100
#: API-published posts per rolling 24 hours, while the Carousel Limitations
#: block on the same page says 50. This pipeline publishes carousels
#: exclusively, so it takes the lower number — exceeding the real cap fails at
#: publish time with an opaque error, and posting fewer costs nothing.
DAILY_POST_LIMIT = 50


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
        await db.update_fields(item.id, {"ig_post_id": fake})
        return fake

    if not (settings.ig_user_id and settings.ig_access_token):
        raise Terminal("Instagram is not configured; cannot publish")

    child_ids = await _ensure_children(item, urls, http, db, settings)
    carousel_id = await _ensure_carousel(item, child_ids, http, db, settings)
    return await _publish(item, carousel_id, http, db, settings)


# ----------------------------------------------------------------- internals


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


async def _publish(
    item: Item, carousel_id: str, http: Any, db: Database, settings: Any
) -> str:
    # If a previous attempt published but lost the response, do not post again.
    if item.attempts:
        already = await _find_existing_post(carousel_id, http, settings)
        if already:
            log.warning("item %s was already published as %s; not reposting",
                        item.id, already)
            await db.update_fields(item.id, {"ig_post_id": already})
            return already

    response = await _post(http, f"{GRAPH}/{settings.ig_user_id}/media_publish", {
        "creation_id": carousel_id,
        "access_token": settings.ig_access_token,
    })
    post_id = str(response["id"])
    await db.update_fields(item.id, {"ig_post_id": post_id})
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
        # 4xx will not succeed on retry — bad token, bad media, rate limit.
        raise Terminal(f"instagram {status}: {_error_text(response)}")

    body = response.json()
    if "id" not in body:
        raise Retryable(f"instagram response had no id: {str(body)[:200]}")
    return body


def _error_text(response: Any) -> str:
    try:
        error = response.json().get("error", {})
        return str(error.get("message") or error)[:300]
    except Exception:
        return (getattr(response, "text", "") or "")[:300]
