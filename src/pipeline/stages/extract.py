"""Extract: turn attachments and links into plain text the researcher can use.

Everything here is best-effort. A dead link or an unreadable image records the
problem and the pipeline carries on — the original message text is usually
enough on its own.
"""

from __future__ import annotations

import asyncio
import logging

from ..models import Item
from ..search import extract_urls, fetch_text

log = logging.getLogger(__name__)

VISION_SYSTEM = """You are reading an image attached to a tech-news post.

Return two things in plain prose:
1. Any text visible in the image, transcribed exactly — headlines, tweet text,
   chart labels, numbers, code, UI labels.
2. A one-sentence description of what the image shows.

Transcribe only what is actually legible. Do not guess at blurred text, and do
not speculate about context that is not visible."""


async def extract(item: Item, llm, http, settings=None) -> dict:
    """Produce ``{"extracted": {"image_descriptions": [...], "url_texts": [...]}}``."""
    urls = extract_urls(item.raw_text)

    url_texts, image_descriptions = await asyncio.gather(
        _extract_urls(http, urls),
        _extract_images(llm, item.raw_media_paths),
    )

    return {
        "extracted": {
            "url_texts": url_texts,
            "image_descriptions": image_descriptions,
        }
    }


async def _extract_urls(http, urls: list[str]) -> list[dict]:
    if not urls:
        return []

    async def one(url: str) -> dict:
        text = await fetch_text(http, url)
        if not text:
            return {"url": url, "error": "unreachable or no extractable text"}
        return {"url": url, "text": text}

    results = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    out: list[dict] = []
    for url, result in zip(urls, results):
        if isinstance(result, BaseException):
            out.append({"url": url, "error": f"{type(result).__name__}: {result}"})
        else:
            out.append(result)
    return out


async def _extract_images(llm, paths: list[str]) -> list[dict]:
    if not paths:
        return []

    descriptions: list[dict] = []
    for path in paths:
        try:
            text = await llm.vision(
                VISION_SYSTEM, "Transcribe and describe this image.", [path]
            )
            descriptions.append({"path": str(path), "description": text})
        except Exception as exc:
            # Vision failure is survivable; the post text usually carries the story.
            log.warning("vision failed for %s: %s", path, exc)
            descriptions.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return descriptions
