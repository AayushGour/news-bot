"""Compose: turn the brief into carousel slides, a caption, and hashtags.

The slide type is ``facts``, not ``compare``. The PoC used the latter name and
the model quite correctly ignored the comparison semantics, emitting a
label/value table under a "COMPARISON" heading. The output was right; the name
was wrong.

Per-field character limits are stated in the prompt but are hints, not
guarantees — the PoC produced a 243-character field against a stated limit of
110. Enforcement lives in the renderer's overflow guard.
"""

from __future__ import annotations

from ..errors import Retryable
from ..models import Item

MIN_SLIDES = 3
MAX_SLIDES = 10  # Instagram carousel hard maximum, and Telegram album maximum.

SLIDE_TYPES = ["hook", "point", "facts", "takeaway", "sources"]

SLIDES_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "minItems": MIN_SLIDES,
            "maxItems": MAX_SLIDES,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": SLIDE_TYPES},
                    "headline": {"type": "string"},
                    "sub": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "stat": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["value", "label"],
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "headline"],
            },
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["slides", "caption", "hashtags"],
}

SYSTEM = """You write Instagram carousel slides for a tech and AI news account.

Turn the brief into slides. Choose how many (3-10) based on how much the
research actually supports. Do not pad to reach a number.

Slide types and their character limits. These are what the template can fit;
going over means text gets cut:

- "hook":     headline <= 60, sub <= 90.  Exactly one, always first.
- "point":    headline <= 45, up to 4 "bullets" each <= 95.
              Optional "stat": {value <= 12, label <= 45}.
- "facts":    headline <= 45, up to 4 "rows", each row exactly
              [label <= 30, value <= 34]. A label/value table, not a comparison.
- "takeaway": headline <= 55, sub <= 110. Exactly one, near the end.
- "sources":  headline <= 45, up to 4 "urls". Exactly one, always last.

Style: declarative and specific. No hype, no rhetorical questions, no emoji
inside slides. Numbers beat adjectives. Never state a fact that is not in the
brief. If the brief flags a contradiction between sources, reflect that
uncertainty rather than picking a side.

Also write:
- "caption": <= 500 characters, may use emoji, summarises the story.
- "hashtags": 8-12 lowercase tags, no # symbol."""


async def compose(item: Item, llm, settings=None) -> dict:
    if not (item.brief or "").strip():
        raise Retryable("cannot compose slides without a brief")

    source_urls: list[str] = []
    for note in item.research or []:
        for url in note.get("sources", []):
            if url not in source_urls:
                source_urls.append(url)

    parts = [f"BRIEF:\n{item.brief}"]
    if source_urls:
        parts.append("AVAILABLE SOURCE URLS:\n" + "\n".join(source_urls[:8]))

    credit = getattr(settings, "source_credit", "") if settings else ""
    if credit:
        parts.append(f"Credit this source channel in the caption: {credit}")

    if item.regen_note:
        # The operator (or the overflow guard) asked for a change. Put it last
        # so it is the most recent thing the model reads.
        parts.append(f"REVISION REQUESTED — apply this: {item.regen_note}")

    doc = await llm.good(SYSTEM, "\n\n".join(parts), schema=SLIDES_SCHEMA, temperature=0.6)

    slides = normalise_slides(doc.get("slides") or [])
    if len(slides) < MIN_SLIDES:
        raise Retryable(f"composer produced only {len(slides)} usable slides")

    caption = build_caption(str(doc.get("caption", "")), doc.get("hashtags") or [], credit)

    return {
        "slides": slides,
        "caption": caption,
        # Clear the note now that it has been applied, so a later unrelated
        # regeneration does not silently reapply stale instructions.
        "regen_note": None,
    }


def normalise_slides(slides: list[dict]) -> list[dict]:
    """Enforce structure the JSON schema cannot express.

    The schema can constrain types and counts but not ordering or uniqueness,
    so the deck shape is fixed here instead of hoped for.
    """
    cleaned: list[dict] = []
    for slide in slides:
        kind = slide.get("type")
        if kind not in SLIDE_TYPES or not str(slide.get("headline", "")).strip():
            continue
        entry = {"type": kind, "headline": str(slide["headline"]).strip()}
        if slide.get("sub"):
            entry["sub"] = str(slide["sub"]).strip()
        if slide.get("bullets"):
            entry["bullets"] = [str(b).strip() for b in slide["bullets"][:4] if str(b).strip()]
        if isinstance(slide.get("stat"), dict) and slide["stat"].get("value"):
            entry["stat"] = {
                "value": str(slide["stat"].get("value", "")),
                "label": str(slide["stat"].get("label", "")),
            }
        if slide.get("rows"):
            rows = [
                [str(r[0]).strip(), str(r[1]).strip()]
                for r in slide["rows"][:4]
                if isinstance(r, (list, tuple)) and len(r) >= 2
            ]
            if rows:
                entry["rows"] = rows
        if slide.get("urls"):
            entry["urls"] = [str(u).strip() for u in slide["urls"][:4] if str(u).strip()]
        cleaned.append(entry)

    # Exactly one hook, first. Surplus hooks become points rather than being
    # discarded — they still carry researched content.
    hooks = [s for s in cleaned if s["type"] == "hook"]
    rest = [s for s in cleaned if s["type"] != "hook"]
    demoted = [{**s, "type": "point"} for s in hooks[1:]]
    ordered = ([hooks[0]] if hooks else []) + demoted + rest

    # Exactly one sources slide, last.
    sources = [s for s in ordered if s["type"] == "sources"]
    body = [s for s in ordered if s["type"] != "sources"]
    ordered = body + ([sources[0]] if sources else [])

    return ordered[:MAX_SLIDES]


def build_caption(caption: str, hashtags: list[str], credit: str = "") -> str:
    caption = caption.strip()
    if credit and credit.lower() not in caption.lower():
        caption = f"{caption}\n\nSource: {credit}"
    tags = " ".join(
        "#" + str(tag).lstrip("#").strip().lower().replace(" ", "")
        for tag in hashtags[:12]
        if str(tag).strip()
    )
    return f"{caption}\n\n{tags}".strip() if tags else caption
