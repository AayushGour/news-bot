"""Extract: turn attachments and links into plain text the researcher can use.

Everything here is best-effort. A dead link or an unreadable image records the
problem and the pipeline carries on — the original message text is usually
enough on its own.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..models import Item
from ..search import extract_urls, fetch_text

log = logging.getLogger(__name__)

IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "description": {"type": "string"},
        "usable": {
            "type": "string",
            "enum": ["hero", "inset", "background", "none"],
        },
        "why": {"type": "string"},
    },
    "required": ["transcript", "description", "usable", "why"],
}

VISION_SYSTEM = """You are reading an image attached to a tech-news post, for a
publisher who may reuse it in an Instagram carousel.

Return four things.

"transcript": any text visible in the image, transcribed exactly — headlines,
tweet text, chart labels, numbers, code, UI labels. Only what is actually
legible. Never guess at blurred text.

"description": one sentence on what the image shows.

"usable": how this image could be reused in a slide. Be strict — a bad image
hurts a post more than no image.
  "hero"       a strong standalone visual: a real photograph, product shot,
               chart, diagram, or screenshot that carries information on its
               own and would fill a slide well.
  "inset"      worth showing small inside a slide alongside text: a tweet
               screenshot, a small chart, a UI fragment, a logo lockup.
  "background" only atmospheric. Fine dimmed behind a headline, but says
               nothing on its own.
  "none"       do not use. Choose this for watermarks, channel branding,
               stock filler, blurry or low-resolution images, collages,
               anything with visible other-brand watermarks, or an image whose
               entire content is text you have already transcribed — showing
               that duplicates the slide copy.

"why": one short sentence justifying the rating."""


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
            verdict = await llm.vision(
                VISION_SYSTEM, "Transcribe, describe, and rate this image.",
                [path], schema=IMAGE_SCHEMA,
            )
        except Exception as exc:
            # Vision failure is survivable; the post text usually carries the story.
            log.warning("vision failed for %s: %s", path, exc)
            descriptions.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue

        if isinstance(verdict, str):  # a model that ignored the schema
            verdict = {"transcript": verdict, "description": verdict,
                       "usable": "none", "why": "unstructured reply"}

        usable = str(verdict.get("usable", "none")).lower()
        if usable not in ("hero", "inset", "background", "none"):
            usable = "none"

        descriptions.append({
            "path": str(path),
            # Kept as `description` so downstream stages that read it are
            # unchanged; it carries both the transcript and the summary.
            "description": (
                f"{verdict.get('transcript', '')}\n\n{verdict.get('description', '')}"
            ).strip(),
            "usable": usable,
            "why": str(verdict.get("why", ""))[:200],
        })
        log.info("image %s rated %s: %s", Path(path).name, usable,
                 str(verdict.get("why", ""))[:80])
    return descriptions
