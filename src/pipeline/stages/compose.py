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

SLIDE_TYPES = [
    "hook", "point", "facts", "kpi", "chart", "code", "flow", "compare",
    "quote", "photo", "takeaway", "sources",
]

#: How a reusable image may be placed on a slide.
IMAGE_MODES = ["hero", "inset", "background"]

#: Visual treatments the composer may choose between, matched to story character.
THEMES = ["signal", "newsprint", "blockprint", "aurora"]

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
                    "lang": {"type": "string"},
                    "code": {"type": "string"},
                    "caption": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "detail": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                    },
                    "left_title": {"type": "string"},
                    "right_title": {"type": "string"},
                    "quote": {"type": "string"},
                    "attribution": {"type": "string"},
                    "image": {"type": "integer"},
                    "image_mode": {"type": "string", "enum": IMAGE_MODES},
                    "tiles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string"},
                                "label": {"type": "string"},
                                "delta": {"type": "string"},
                            },
                            "required": ["value", "label"],
                        },
                    },
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "number"},
                                "display": {"type": "string"},
                            },
                            "required": ["label", "value"],
                        },
                    },
                    "unit": {"type": "string"},
                },
                "required": ["type", "headline"],
            },
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "theme": {"type": "string", "enum": THEMES},
    },
    "required": ["slides", "caption", "hashtags", "theme"],
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
- "code":     headline <= 45, "lang" (e.g. json, python, bash, yaml, text),
              "code" <= 420 chars and at most 12 lines, optional "caption"
              <= 90. Use REAL syntax for the thing being described - an actual
              config snippet, request body, file tree, or command. Never
              pseudocode, never invented API names.
- "flow":     headline <= 45, 3-5 "steps", each {label <= 26, detail <= 70}.
              For a process, pipeline, or sequence of events in order.
- "compare":  headline <= 45, "left_title" and "right_title" <= 22 each, up to
              4 "rows" as [left <= 40, right <= 40]. A genuine A-vs-B: before
              and after, us and them, old and new. Not a label/value table -
              that is "facts".
- "quote":    headline <= 40, "quote" <= 180, "attribution" <= 40. Only when
              the brief contains an actual quoted statement. Never fabricate
              or paraphrase one into quotation marks.
- "photo":    headline <= 45, optional "caption" <= 90, and "image" set to the
              index of an available image. Only for images rated "hero".
- "kpi":      headline <= 45, 2-4 "tiles", each {value <= 10 chars,
              label <= 28, optional delta <= 10 like "+38%" or "-2pts"}.
              For headline figures that stand on their own.
- "chart":    headline <= 45, 2-6 "series" entries, each {label <= 20,
              value: a NUMBER, optional display like "$65B"}, optional
              "unit" <= 12 and "caption" <= 80. Renders as a horizontal bar
              chart. Values must be COMPARABLE — the same measure on the same
              scale, since they share one axis. Never mix a count with a
              percentage.
- "takeaway": headline <= 55, sub <= 110. Exactly one, near the end.
- "sources":  headline <= 45, up to 4 "urls". Exactly one, always last.

If the item came with images, an AVAILABLE IMAGES list appears below, each with
an index and a rating. Use them — a real photograph or screenshot beats another
text slide, and these came with the story:

- rating "hero": give it a "photo" slide, or set "image" plus
  "image_mode": "hero" on a slide that has little other content.
- rating "inset": set "image" and "image_mode": "inset" on a slide whose text
  it supports. It renders alongside the copy.
- rating "background": set "image" and "image_mode": "background" on the hook
  slide only. It renders dimmed behind the headline.
- rating "none": do not reference it at all.

Never set "image" to an index that is not in the list, and never use an image
whose only content is text you are already putting on the slide.

Reach for the richer types whenever they explain better than prose does:

- Explaining a format, schema, config, API or file layout? Use "code" and show
  the real thing. A reader learns more from six lines of actual JSON than from
  three bullets describing it.
  If the brief contains a fenced block, that is verbatim source material —
  put it on a "code" slide. If it contains none, do NOT invent one; use
  "facts" or "flow" instead. A fabricated example is worse than no example.
- Describing how something works step by step, or a sequence of events? Use
  "flow".
- Two options, two eras, two companies, before and after? Use "compare".
- Two to four headline numbers worth reading on their own? Use "kpi".
- Several comparable quantities the reader should rank at a glance? Use
  "chart". Only when the brief states real figures — never estimate a value to
  fill a bar, and never chart numbers measured differently from each other.
- Someone said something notable and the brief quotes it? Use "quote".

A deck of nothing but headline-and-bullets is the failure mode. Aim for at
least one non-bullet slide in every deck where the subject allows it, and more
when the subject is technical.

EVERY SLIDE MUST HAVE BODY CONTENT. A headline on its own renders as a heading
floating on an empty page and is discarded. The headline is a label, not the
content:

- "hook" and "takeaway" need "sub".
- "point" needs "bullets" (or a "stat").
- "facts" needs "rows", "flow" needs "steps", "compare" needs "rows",
  "quote" needs "quote", "sources" needs "urls".

Do not put the substance in the headline. "Three climbers trusted Gemini to
plan a Mount Shasta route and were rescued" is a sentence, not a headline.
Write the headline as "AI-planned climb goes wrong" and put the detail in the
bullets where it belongs. If you cannot think of body content for a slide, the
slide should not exist — write fewer, fuller slides.

Style: declarative and specific. No hype, no rhetorical questions, no emoji
inside slides. Numbers beat adjectives. Never state a fact that is not in the
brief. If the brief flags a contradiction between sources, reflect that
uncertainty rather than picking a side.

Also write:
- "caption": <= 500 characters, may use emoji, summarises the story.
- "hashtags": 8-12 lowercase tags, no # symbol.

Finally pick "theme" — the visual treatment that suits THIS story. Match the
look to the substance, do not just alternate:

- "blockprint": loud, high-contrast, uppercase. For conflict, bans, lawsuits,
  shutdowns, dramatic reversals, anything confrontational.
- "newsprint": restrained serif on grey, like a wire dispatch. For policy,
  regulation, legal rulings, government, research findings — anything where
  sober authority suits the subject better than noise.
- "aurora": soft gradient and glass. For product launches, creative and
  consumer AI, design, media, anything visual or optimistic.
- "signal": dark technical. For infrastructure, models, benchmarks, funding,
  chips, engineering — the default when none of the others clearly fits."""


async def compose(item: Item, llm, settings=None) -> dict:
    if not (item.brief or "").strip():
        raise Retryable("cannot compose slides without a brief")

    source_urls: list[str] = []
    for note in item.research or []:
        for url in note.get("sources", []):
            if url not in source_urls:
                source_urls.append(url)

    images = usable_images(item)
    parts = [f"BRIEF:\n{item.brief}"]
    if images:
        listing = "\n".join(
            f"[{i}] rating={img['usable']} — {img['description'][:180]}"
            for i, img in enumerate(images)
        )
        parts.append(f"AVAILABLE IMAGES:\n{listing}")
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

    slides = normalise_slides(doc.get("slides") or [], images)
    if len(slides) < MIN_SLIDES:
        raise Retryable(f"composer produced only {len(slides)} usable slides")

    caption = build_caption(str(doc.get("caption", "")), doc.get("hashtags") or [], credit)

    theme = str(doc.get("theme", "")).strip()
    if theme not in THEMES:
        theme = "signal"

    return {
        "slides": slides,
        "caption": caption,
        "theme": theme,
        # Clear the note now that it has been applied, so a later unrelated
        # regeneration does not silently reapply stale instructions.
        "regen_note": None,
    }


def _fmt(value: float) -> str:
    """A readable default when the model gives a number but no display string."""
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B".replace(".0B", "B")
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value:g}"


def usable_images(item: Item) -> list[dict]:
    """Attached images the vision pass judged worth reusing, in order."""
    out = []
    for described in (item.extracted or {}).get("image_descriptions", []):
        if described.get("usable") in IMAGE_MODES and described.get("path"):
            out.append(described)
    return out


def normalise_slides(slides: list[dict], images: list[dict] | None = None) -> list[dict]:
    """Enforce structure the JSON schema cannot express.

    The schema can constrain types and counts but not ordering or uniqueness,
    so the deck shape is fixed here instead of hoped for.
    """
    cleaned: list[dict] = []
    for slide in slides:
        kind = slide.get("type")
        if kind not in SLIDE_TYPES or not str(slide.get("headline", "")).strip():
            continue
        # A slide with nothing but a headline renders as a heading on an empty
        # 1080x1350 field. Every type needs body content; several are satisfied
        # by more than one field.
        required_any = {
            "kpi": ("tiles",),
            "chart": ("series",),
            "hook": ("sub",),
            "point": ("bullets", "stat", "sub"),
            "facts": ("rows",),
            "code": ("code",),
            "flow": ("steps",),
            "compare": ("rows",),
            "quote": ("quote",),
            "photo": ("image",),
            "takeaway": ("sub",),
            "sources": ("urls",),
        }.get(kind, ())
        if required_any and not any(
            slide.get(field) not in (None, "", [], {}) for field in required_any
        ):
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
        if slide.get("code"):
            # Keep newlines and indentation; they are the content here.
            entry["code"] = str(slide["code"]).rstrip()[:900]
            entry["lang"] = str(slide.get("lang", "")).strip().lower()[:12]
        if slide.get("caption"):
            entry["caption"] = str(slide["caption"]).strip()
        if slide.get("steps"):
            steps = [
                {"label": str(x.get("label", "")).strip(),
                 "detail": str(x.get("detail", "")).strip()}
                for x in slide["steps"][:5]
                if isinstance(x, dict) and str(x.get("label", "")).strip()
            ]
            if steps:
                entry["steps"] = steps
        if slide.get("tiles"):
            tiles = [
                {"value": str(t.get("value", "")).strip(),
                 "label": str(t.get("label", "")).strip(),
                 "delta": str(t.get("delta", "")).strip()}
                for t in slide["tiles"][:4]
                if isinstance(t, dict) and str(t.get("value", "")).strip()
            ]
            if tiles:
                entry["tiles"] = tiles
        if slide.get("series"):
            entry_series = []
            for point in slide["series"][:6]:
                if not isinstance(point, dict):
                    continue
                try:
                    value = float(point["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                label = str(point.get("label", "")).strip()
                if not label:
                    continue
                entry_series.append({
                    "label": label,
                    "value": value,
                    "display": str(point.get("display", "")).strip() or _fmt(value),
                })
            if entry_series:
                # Bar length is computed here so the template stays declarative
                # and cannot divide by zero on a flat series.
                widest = max(abs(p["value"]) for p in entry_series) or 1.0
                for point in entry_series:
                    point["pct"] = round(abs(point["value"]) / widest * 100, 1)
                entry["series"] = entry_series
                if slide.get("unit"):
                    entry["unit"] = str(slide["unit"]).strip()[:12]
        for key in ("left_title", "right_title", "quote", "attribution"):
            if slide.get(key):
                entry[key] = str(slide[key]).strip()

        # Resolve an image index to a real path. A hallucinated index, or a
        # mode the vision pass did not sanction, silently drops the image
        # rather than rendering a broken <img>.
        available = images or []
        index = slide.get("image")
        if isinstance(index, int) and 0 <= index < len(available):
            picked = available[index]
            mode = str(slide.get("image_mode", "")).lower()
            if mode not in IMAGE_MODES:
                mode = picked["usable"]
            # Never place an image more prominently than vision allowed.
            rank = {"background": 0, "inset": 1, "hero": 2}
            if rank[mode] <= rank[picked["usable"]]:
                entry["image"] = picked["path"]
                entry["image_mode"] = mode
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
