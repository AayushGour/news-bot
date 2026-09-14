"""SearXNG queries and article extraction.

SearXNG is self-hosted specifically because public instances do not expose the
JSON API and rate-limit automated traffic.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import Retryforever

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

MAX_BODY_BYTES = 2_000_000
#: No ceiling on extracted article text. It was 6000 characters, which cut
#: the back half off any long-form piece before a model ever saw it.
MAX_TEXT_CHARS = 0
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


#: SearXNG only queries the "general" category by default, and its general
#: engines — DuckDuckGo, Brave, Startpage, Mojeek — all CAPTCHA or rate-limit
#: under sustained automated querying. When they suspend, general returns zero
#: results and research starves while the service still answers 200.
#:
#: "it" and "news" stay healthy because they use APIs rather than scraping, and
#: for a tech-news pipeline they are better sources anyway: GitHub, Hacker News
#: and Stack Overflow carry the technical detail and code samples that a
#: general web engine rarely surfaces.
DEFAULT_CATEGORIES = "general,it,news"


#: Engines to hit directly when the task is finding repositories rather than
#: reading about them. Verified live: categories=it returns only MDN and Docker
#: Hub because the github engine, though enabled and declaring itself in the
#: "it" category, never fires through a category query. Naming it explicitly
#: returns star-ranked repositories. The parameter is real but undocumented —
#: implemented in searx/webadapter.py, absent from the search API docs — and
#: it also overrides an engine's disabled-by-default flag.
REPO_ENGINES = "github"

#: Minimum gap between requests when every query targets ONE engine. GitHub's
#: unauthenticated search API is roughly ten requests a minute; going over
#: earns a 403 and a 180-second suspension, which costs far more than the wait.
#: 6.5s sat right on the boundary and still tripped on the tenth query, so
#: this leaves real headroom rather than aiming at the documented rate.
SINGLE_ENGINE_INTERVAL_S = 9.0


async def search_many(
    http: Any, base_url: str, queries: list[str], limit: int = 6,
    categories: str = DEFAULT_CATEGORIES, engines: str = "",
    concurrency: int = 4,
) -> list[dict]:
    """Run several queries at once and merge their results, first seen first.

    Searching one query at a time made an item wait on the slowest engine once
    per query, in series. The queries are independent — nothing in one informs
    the next — so waiting for them sequentially bought nothing.

    An empty result from one query is survivable and simply contributes
    nothing. SearXNG being unreachable is not: it is down for every query, so
    that raise propagates rather than being flattened into "no results", which
    would report an outage as a research shortfall.
    """
    wanted = [q.strip() for q in queries if str(q or "").strip()]
    if not wanted:
        return []

    # A category query fans out across many engines, so concurrency spreads the
    # load. Naming a single engine concentrates all of it on one upstream, and
    # GitHub's unauthenticated search API allows about ten requests a minute:
    # item 100's sixteen expanded queries went out four at a time, earned a 403,
    # and SearXNG suspended the engine for 180 seconds. Every query after that
    # returned 200 with nothing, which read as "this subject has no repos".
    #
    # The queries are all still run — none is dropped — they are simply spaced
    # far enough apart to stay inside the upstream's budget.
    single_engine = len([e for e in engines.split(",") if e.strip()]) == 1
    if single_engine:
        concurrency = 1
    pace = SINGLE_ENGINE_INTERVAL_S if single_engine else 0.0

    semaphore = asyncio.Semaphore(max(1, concurrency))
    clock = {"next": 0.0}

    async def one(query: str) -> list[dict]:
        async with semaphore:
            if pace:
                now = asyncio.get_running_loop().time()
                wait = clock["next"] - now
                if wait > 0:
                    await asyncio.sleep(wait)
                clock["next"] = asyncio.get_running_loop().time() + pace
            return await searx(http, base_url, query, limit, categories, engines)

    batches = await asyncio.gather(
        *(one(q) for q in wanted), return_exceptions=True
    )

    merged: list[dict] = []
    seen: set[str] = set()
    outage: Retryforever | None = None
    for query, batch in zip(wanted, batches):
        if isinstance(batch, Retryforever):
            # Remember it, but keep reading the rest. An engine that hits its
            # rate limit part-way through a batch fails every query after that
            # point, and re-raising immediately discarded the results the
            # earlier queries had already returned — sixteen queries would find
            # nine pages of repositories and report none, because the tenth
            # tripped a cooldown.
            outage = batch
            continue
        if isinstance(batch, BaseException):
            log.warning("search failed for %r: %s", query[:60], batch)
            continue
        for result in batch:
            url = (result.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                merged.append(result)

    if outage is not None:
        if not merged:
            # Nothing at all came back: this is the outage the caller must not
            # mistake for "the web has nothing on this subject".
            raise outage
        log.warning(
            "search hit an engine outage part-way (%s); keeping %d result(s) "
            "from %d query(ies)", outage, len(merged), len(wanted),
        )
    return merged


def _unresponsive(body: dict) -> set[str]:
    """Engine names SearXNG reported as failing for this query.

    The entries are ``[name, reason]`` pairs, but older builds return bare
    strings, so both shapes are accepted rather than assuming one.
    """
    names: set[str] = set()
    for entry in body.get("unresponsive_engines") or []:
        if isinstance(entry, (list, tuple)) and entry:
            names.add(str(entry[0]).strip().lower())
        elif isinstance(entry, str):
            names.add(entry.strip().lower())
    return names


def _all_engines_down(body: dict, engines: str, categories: str) -> bool:
    """True when nothing that could have answered this query actually did.

    Only decidable when the caller named its engines: with a category query the
    responding set is whatever SearXNG has enabled for it, which is not visible
    from the response, and one dead engine among several healthy ones is a
    normal empty result rather than an outage.
    """
    asked = {e.strip().lower() for e in engines.split(",") if e.strip()}
    if not asked:
        return False
    return asked.issubset(_unresponsive(body))


async def searx(
    http: Any, base_url: str, query: str, limit: int = 6,
    categories: str = DEFAULT_CATEGORIES,
    engines: str = "",
) -> list[dict]:
    """Query SearXNG's JSON API.

    A query that returns nothing is survivable and yields []. SearXNG being
    unreachable is not: it is down for every query, so returning [] makes the
    item fail as "0 of 5 researchers produced notes" and burn its retry budget
    on an outage that has nothing to do with the item. That surfaces as a
    research problem and sends anyone debugging it to the wrong subsystem.
    """
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/search",
            params=(
                {"q": query, "format": "json", "engines": engines}
                if engines
                else {"q": query, "format": "json", "categories": categories}
            ),
            timeout=30,
        )
    except Exception as exc:
        raise Retryforever(f"searxng unreachable: {exc}") from exc

    try:
        if response.status_code >= 500:
            raise Retryforever(f"searxng {response.status_code}")
        if response.status_code != 200:
            # 4xx is a bad query, not a dead service.
            log.warning("searxng %s for %r", response.status_code, query[:60])
            return []
        body = response.json()
        results = body.get("results", [])
    except Retryforever:
        raise
    except Exception as exc:
        log.warning("searxng returned unusable data for %r: %s", query[:60], exc)
        return []

    # An engine that 403s under load is suspended by SearXNG for a cooldown,
    # after which every query still answers 200 with an empty result list. That
    # is indistinguishable from "the web has nothing" unless this field is
    # read, and the difference matters: item 100 fired 16 expanded queries at
    # the single github engine, tripped its rate limit, and reported "found
    # nothing worth posting — reply with a better search term". The search term
    # was fine. Asking the operator to fix an upstream cooldown is worse than
    # useless, because answering it just spends the cooldown again.
    if not results and _all_engines_down(body, engines, categories):
        down = ", ".join(sorted(_unresponsive(body))) or "all engines"
        raise Retryforever(f"searxng engines unavailable ({down}) for {query[:60]!r}")

    out: list[dict] = []
    for result in results:
        url = result.get("url", "")
        if not url or is_blocked(url):
            continue
        out.append({
            "url": url,
            "title": result.get("title", ""),
            "content": result.get("content", ""),
            # SearXNG already returns these for repository results and they
            # were being discarded. Stars are the only ranking signal available
            # without a second API call to GitHub.
            "popularity": result.get("popularity"),
            "tags": result.get("tags") or [],
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


def dedupe_by_path(results: list[dict], keep: int) -> list[dict]:
    """One result per distinct URL path, keeping many from the same host.

    Enumerating repositories means twenty results from github.com are twenty
    different answers, not one source repeated. dedupe_by_domain would collapse
    them to a single entry, which is correct for reading about a story and
    exactly wrong for listing things.
    """
    seen: set[str] = set()
    picked: list[dict] = []
    for result in results:
        parsed = urlparse(result["url"])
        key = f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
        if not key or key in seen:
            continue
        seen.add(key)
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

        # include_formatting keeps fenced code blocks, which the default
        # extraction silently discards. Without it a page documenting a file
        # format yields prose describing the format and never the format
        # itself, so no amount of prompting downstream can produce a real
        # code slide.
        text = await asyncio.to_thread(
            trafilatura.extract, body, include_formatting=True
        )
    except Exception as exc:  # pragma: no cover - trafilatura internals
        log.debug("extraction failed %s: %s", url, exc)
        return None

    if not text:
        return None
    return text[:MAX_TEXT_CHARS] if MAX_TEXT_CHARS else text


# --- image search, for the hook background -----------------------------------

#: Minimum pixels on the long edge. A slide is 1080x1350, and the background is
#: scaled up 8% to hide the blur edge, so anything smaller visibly degrades.
MIN_IMAGE_EDGE = 900
MAX_IMAGE_BYTES = 8_000_000

#: Sources whose licensing is predictable. Wikimedia and openverse index
#: material that is free to reuse; the rest of the web is not, and a news
#: account republishing an arbitrary photograph is a real risk rather than a
#: theoretical one. Reordering this list is a licensing decision, not a tuning
#: knob.
PREFERRED_IMAGE_DOMAINS = (
    "upload.wikimedia.org",
    "commons.wikimedia.org",
    "wikimedia.org",
    "openverse.org",
    "nasa.gov",
    "pexels.com",
    "unsplash.com",
)


async def image_search(
    http: Any, base_url: str, query: str, limit: int = 12
) -> list[dict]:
    """Search SearXNG's image category. Returns candidates, best-licensed first."""
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "categories": "images"},
            timeout=30,
        )
    except Exception as exc:
        raise Retryforever(f"searxng unreachable: {exc}") from exc

    if response.status_code >= 500:
        raise Retryforever(f"searxng {response.status_code}")
    if response.status_code != 200:
        return []

    try:
        results = response.json().get("results", [])
    except Exception:
        return []

    out = []
    for r in results[: limit * 3]:
        src = r.get("img_src") or r.get("thumbnail_src")
        if not src or not src.startswith("http"):
            continue
        out.append({
            "url": src,
            "title": r.get("title", ""),
            "source": r.get("url", ""),
            "engine": r.get("engine", ""),
        })

    def rank(entry: dict) -> int:
        host = urlparse(entry["url"]).netloc.lower()
        for index, domain in enumerate(PREFERRED_IMAGE_DOMAINS):
            if host.endswith(domain):
                return index
        return len(PREFERRED_IMAGE_DOMAINS)

    out.sort(key=rank)
    return out[:limit]


async def download_image(http: Any, url: str, target_dir: Path) -> Path | None:
    """Fetch one candidate. Returns None for anything unusable.

    Rejects rather than raises: a background is a bonus, and no image must ever
    stop a post going out.
    """
    try:
        response = await http.get(
            url, timeout=20, follow_redirects=True, headers={"User-Agent": UA}
        )
        if response.status_code != 200:
            return None
        blob = response.content
    except Exception as exc:
        log.debug("image download failed %s: %s", url, exc)
        return None

    if len(blob) > MAX_IMAGE_BYTES or len(blob) < 15_000:
        return None

    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(blob)) as img:
            width, height = img.size
            fmt = (img.format or "").lower()
        if max(width, height) < MIN_IMAGE_EDGE:
            return None
        if fmt not in ("jpeg", "jpg", "png", "webp"):
            return None
    except Exception:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"jpeg": ".jpg"}.get(fmt, f".{fmt}")
    path = target_dir / f"bg_{abs(hash(url)) % 10**10}{suffix}"
    path.write_bytes(blob)
    log.info("background candidate %dx%d from %s", width, height, urlparse(url).netloc)
    return path


async def download_avatar(http: Any, url: str, target_dir: Path) -> Path | None:
    """Fetch a small identity image such as a GitHub owner avatar.

    Separate from download_image because that one enforces a 900px floor for
    backgrounds, which every avatar fails. Returns None on any problem: a
    missing logo must never stop a slide rendering.
    """
    try:
        response = await http.get(
            url, timeout=12, follow_redirects=True, headers={"User-Agent": UA}
        )
        if response.status_code != 200:
            return None
        blob = response.content
    except Exception:
        return None

    if not blob or len(blob) > 3_000_000:
        return None

    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(blob)) as img:
            fmt = (img.format or "").lower()
            if min(img.size) < 40:
                return None
    except Exception:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"jpeg": ".jpg"}.get(fmt, f".{fmt}")
    path = target_dir / f"logo_{abs(hash(url)) % 10**10}{suffix}"
    path.write_bytes(blob)
    return path
