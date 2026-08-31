"""SearXNG queries and article extraction.

SearXNG is self-hosted specifically because public instances do not expose the
JSON API and rate-limit automated traffic.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

MAX_BODY_BYTES = 2_000_000
MAX_TEXT_CHARS = 6_000
FETCH_TIMEOUT_S = 15

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

#: Domains that are never useful as research sources. Seeded from a real
#: failure: searching "Cursor" returned mouse-cursor download sites, and one of
#: them was then cited as the source for a statement by Cursor's leadership.
DOMAIN_BLOCKLIST = {
    "custom-cursor.com",
    "www.custom-cursor.com",
    "rw-designer.com",
    "www.rw-designer.com",
    "cursor.cc",
    "myactivity.google.com",
    "accounts.google.com",
    "policies.google.com",
    "support.google.com",
    "translate.google.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "pinterest.com",
    "www.pinterest.com",
}


def extract_urls(text: str) -> list[str]:
    """Pull URLs out of message text, trimming trailing punctuation."""
    found: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found


def is_blocked(url: str) -> bool:
    return urlparse(url).netloc.lower() in DOMAIN_BLOCKLIST


async def searx(http: Any, base_url: str, query: str, limit: int = 6) -> list[dict]:
    """Query SearXNG's JSON API. Search failure is survivable — returns []."""
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json"},
            timeout=30,
        )
        if response.status_code != 200:
            log.warning("searxng %s for %r", response.status_code, query[:60])
            return []
        results = response.json().get("results", [])
    except Exception as exc:
        log.warning("searxng failed for %r: %s", query[:60], exc)
        return []

    out: list[dict] = []
    for result in results:
        url = result.get("url", "")
        if not url or is_blocked(url):
            continue
        out.append({
            "url": url,
            "title": result.get("title", ""),
            "content": result.get("content", ""),
        })
        if len(out) >= limit:
            break
    return out


def dedupe_by_domain(results: list[dict], keep: int) -> list[dict]:
    """One result per domain — five pages of the same site is not research."""
    seen: set[str] = set()
    picked: list[dict] = []
    for result in results:
        domain = urlparse(result["url"]).netloc.lower()
        if domain in seen:
            continue
        seen.add(domain)
        picked.append(result)
        if len(picked) >= keep:
            break
    return picked


async def fetch_text(http: Any, url: str) -> str | None:
    """Fetch a page and extract its main text.

    Returns ``None`` on any failure. Callers treat that as survivable — a
    paywalled or dead link must never fail the whole item.
    """
    try:
        response = await http.get(
            url, timeout=FETCH_TIMEOUT_S,
            follow_redirects=True, headers={"User-Agent": UA},
        )
    except Exception as exc:
        log.debug("fetch failed %s: %s", url, exc)
        return None

    if response.status_code != 200:
        return None

    try:
        body = response.text or ""
    except Exception:
        return None
    if len(body.encode("utf-8", "ignore")) > MAX_BODY_BYTES:
        return None

    try:
        import trafilatura

        text = await asyncio.to_thread(trafilatura.extract, body)
    except Exception as exc:  # pragma: no cover - trafilatura internals
        log.debug("extraction failed %s: %s", url, exc)
        return None

    return text[:MAX_TEXT_CHARS] if text else None
