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

import logging
import re

from ..conversation import NeedsInput
from ..coverage import unaddressed
from ..integrity import check_deck
from ..errors import Retryable
from ..models import Item, Status

log = logging.getLogger(__name__)

#: A hashtag written into the caption prose. Requires a letter first, so "#1"
#: and a trailing "C#" are left alone — those are text, not tags.
_INLINE_HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]*)")

MIN_SLIDES = 3
MAX_SLIDES = 10  # Instagram carousel hard maximum, and Telegram album maximum.

SLIDE_TYPES = [
    "hook", "point", "facts", "kpi", "chart", "code", "flow", "compare",
    "quote", "photo", "repo", "links", "takeaway", "sources", "follow",
]

#: The tail of every deck, in the order they must appear. A deck ending on a
#: body slide has no attribution, so one of these is guaranteed, not requested.
CLOSING_TYPES = ("links", "sources", "follow")

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
                    "owner": {"type": "string"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "stars": {"type": "integer"},
                    "language": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
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
  slide. DO THIS WHENEVER an image is available and no stronger use fits — it
  renders blurred and darkened behind the headline, and a photographic hook is
  far more arresting in a feed than a flat colour field. A hook slide with an
  available background image and no image set is a missed opportunity.
  You may also set it on the takeaway slide to bookend the deck.
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
- "hashtags": 3-5 lowercase tags, no # symbol. Five is the hard maximum.
  Choose the most specific ones — a precise tag reaches an interested
  audience, a broad one like #ai reaches nobody in a feed of millions.

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

    if item.intent == "list" and not (item.research or []):
        raise Retryable("enumeration has no items to compose")

    source_urls: list[str] = []
    for note in item.research or []:
        for url in note.get("sources", []):
            if url not in source_urls:
                source_urls.append(url)

    images = usable_images(item)
    parts = []

    # Off by default until the eval says it helps. Prose rules in this prompt
    # have been ignored repeatedly — limits exceeded twofold, headlines
    # swallowing the body — and an example is a shape to imitate rather than a
    # rule to reason around. Whether that is actually true here is measurable,
    # so it is measured before it ships on.
    # Scoped by intent rather than switched on globally: on the golden set the
    # examples took enumeration from 2/4 composed to 4/4, and news from 10/10
    # to 8/10. Applying them everywhere trades one for the other.
    from ..config import few_shot_enabled

    if few_shot_enabled(getattr(settings, "few_shot_examples", "off"), item.intent):
        from .examples import block

        parts.append(block(item.intent, item.raw_text or ""))

    parts.append(f"BRIEF:\n{item.brief}")
    if images:
        listing = "\n".join(
            f"[{i}] rating={img['usable']} — {img['description']}"
            for i, img in enumerate(images)
        )
        parts.append(f"AVAILABLE IMAGES:\n{listing}")
    if source_urls:
        parts.append("AVAILABLE SOURCE URLS:\n" + "\n".join(source_urls[:8]))

    credit = getattr(settings, "source_credit", "") if settings else ""
    if credit and item.source != "dm":
        parts.append(f"Credit this source channel in the caption: {credit}")

    if item.source == "dm":
        # The operator asked for this directly, so there is no channel to
        # credit and no reason to spend the last slide on a source list they
        # already know. Close on the account instead.
        parts.append(
            "CLOSING SLIDE: this was requested directly, not taken from the "
            "source channel. End with a \"follow\" slide instead of a "
            "\"sources\" slide."
        )
    else:
        parts.append('CLOSING SLIDE: end with a "sources" slide.')

    if item.intent == "list":
        # An enumeration is one slide per thing. The news guidance about
        # hooks, flow and comparison does not apply — the reader wants the
        # list, and every slide they have to swap into is one item.
        parts.append(
            "DECK SHAPE — this is an enumeration, not a news story.\n"
            "Each research note is ONE thing to feature. Build:\n"
            "  1. a \"hook\" slide naming what the list is and how many\n"
            "  2. one \"repo\" slide per note, in the order given. Copy\n"
            "     \"owner\", \"name\", \"url\", \"stars\" and \"language\" from the\n"
            "     note VERBATIM — they are facts, not things to rewrite — and\n"
            "     put your own one-sentence summary in \"sub\" (<= 110 chars),\n"
            "     saying what it is and who it is for, plus 3-4 short\n"
            "     \"bullets\" (<= 60 chars each) covering what it does, what\n"
            "     stands out, and who should reach for it. A card with only a\n"
            "     name and a star count wastes most of the slide.\n"
            "  3. a \"links\" slide listing every url in the same order, so the\n"
            "     reader can find them all from one screenshot\n"
            "  4. a \"follow\" slide last\n"
            "Use every note. Do not merge them, do not add items that are not\n"
            "in the notes, and do not reorder — they arrive ranked. Skip\n"
            "facts, flow, compare and chart slides entirely."
        )

    if item.gaps:
        # The deck is being built knowing part of the request is unanswered —
        # normally because the operator sent /post. Said plainly, that is an
        # honest limitation. Left unsaid, the model finds a hole it was not
        # told about and fills it: item 116 produced "What the research
        # doesn't show" and a caption asserting the research "simply hasn't
        # been done yet", from eight general articles it had merely failed to
        # search past.
        parts.append(
            "PARTIAL MATERIAL — the research did not answer these parts of the "
            "request:\n"
            + "\n".join(f"  - {g}" for g in item.gaps)
            + "\n\nCover what the notes DO support and say, in one line, that "
              "the rest is not covered here. You must NOT claim that research "
              "on it does not exist, has not been done, or is missing from the "
              "literature: a handful of pages came back thin, which says "
              "something about this search and nothing about the field. Do not "
              "build the deck around the absence."
        )

    if item.regen_note:
        # The operator (or the overflow guard) asked for a change. Put it last
        # so it is the most recent thing the model reads.
        parts.append(f"REVISION REQUESTED — apply this: {item.regen_note}")

    doc = await llm.good(SYSTEM, "\n\n".join(parts), schema=SLIDES_SCHEMA, temperature=0.6)
    slides = normalise_slides(doc.get("slides") or [], images)

    # Critique the deck against what was actually asked, and give the composer
    # one more attempt naming what it left out. A deck can be well-formed and
    # still answer only half the request: item 45 asked for the early signs of
    # burnout and for AI's effect on them, and shipped eight tidy slides about
    # the second half only. Every structural check passed it.
    #
    # One retry, not a loop. If the second attempt still misses, research had
    # already cleared these clauses, so the material exists and a third pass on
    # the same brief is unlikely to find it.
    missing = await unaddressed(llm, item.clauses, _deck_text(slides))
    if missing:
        log.info("item %s deck missed %d clause(s); recomposing", item.id, len(missing))
        retry = parts + [
            "REVISION REQUESTED — the previous attempt did not address these, "
            "and each one needs a slide of its own:\n"
            + "\n".join(f"  - {c}" for c in missing)
        ]
        doc = await llm.good(
            SYSTEM, "\n\n".join(retry), schema=SLIDES_SCHEMA, temperature=0.6
        )
        slides = normalise_slides(doc.get("slides") or [], images)

    # A prompt is a request; this is the check. Presenting our own failure to
    # find something as evidence it does not exist is the one error here that
    # actively misinforms a reader, so it cannot depend on the model obeying.
    problems = check_deck(slides, item.research or [], str(doc.get("caption", "")))
    if problems:
        log.warning("item %s deck failed integrity: %s", item.id, "; ".join(problems))
        fixed = parts + [
            "REJECTED — this deck made a claim it cannot support:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nRebuild it around what the notes actually say. State any "
              "gap in one line at most, and never as a finding."
        ]
        doc = await llm.good(
            SYSTEM, "\n\n".join(fixed), schema=SLIDES_SCHEMA, temperature=0.4
        )
        slides = normalise_slides(doc.get("slides") or [], images)
        problems = check_deck(slides, item.research or [], str(doc.get("caption", "")))
        if problems:
            # Twice is not a slip. Publishing it would put a false claim on a
            # real account, so the operator decides instead.
            raise NeedsInput(
                "I built a deck for this twice and both times it claimed "
                "something the sources do not support:\n"
                + "\n".join(f"  · {p}" for p in problems)
                + "\n\nReply with a source that actually covers it, or /drop.",
                resume_status=Status.RESEARCHED,
            )

    if item.intent == "list":
        slides = _restore_repo_facts(slides, item.research or [])
        slides = ensure_links_slide(slides, item.research or [])
    # Size is judged on what the composer actually produced. Checking after the
    # closing slides were added would let two generated slides pad a deck with
    # one real slide up to the minimum and ship it.
    if len(slides) < MIN_SLIDES:
        raise Retryable(f"composer produced only {len(slides)} usable slides")
    slides = ensure_closing_slide(slides, item.source == "dm", source_urls)

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
            "repo": ("name",),
            "links": ("links",),
            "follow": ("sub",),
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
        for key in ("left_title", "right_title", "quote", "attribution",
                    "owner", "name", "url", "language"):
            if slide.get(key):
                entry[key] = str(slide[key]).strip()
        if isinstance(slide.get("stars"), int):
            entry["stars"] = slide["stars"]
        if slide.get("links"):
            entry["links"] = [
                str(u).strip() for u in slide["links"][:10] if str(u).strip()
            ]

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

    # links, then sources, then follow — the tail of every deck, in that order.
    tails = {t: [s for s in ordered if s["type"] == t] for t in CLOSING_TYPES}
    body = [s for s in ordered if s["type"] not in CLOSING_TYPES]
    ordered = body + [tails[t][0] for t in CLOSING_TYPES if tails[t]]

    return ordered[:MAX_SLIDES]


#: Instagram rejects captions carrying more than this many hashtags.
MAX_HASHTAGS = 5

#: Links the index slide can show before the template runs out of room.
MAX_LINKS = 10

#: How many urls a generated sources slide carries, matching the prompt's cap.
MAX_SOURCE_URLS = 4


def ensure_closing_slide(
    slides: list[dict], is_dm: bool, source_urls: list[str],
) -> list[dict]:
    """Guarantee the deck ends on a follow slide, with attribution before it.

    Two separate guarantees, and conflating them was a bug. Attribution says
    where the facts came from; the follow slide asks for the follow. An earlier
    version required only that *some* closing slide existed, so a channel deck
    that already carried "sources" satisfied the check and shipped with no call
    to action at all — every channel post lacked one.

    The prompt asks for both and the model mostly complies, but 5 of the 30
    decks in the compose eval ended on a body slide, so neither is left to it.
    """
    types = [slide.get("type") for slide in slides]
    additions: list[dict] = []

    # A direct request has no channel to credit, and an item whose research
    # produced no urls has nothing truthful to put on a sources slide — an
    # empty one would be dropped as bodyless anyway.
    if not is_dm and source_urls and not any(
        t in ("sources", "links") for t in types
    ):
        additions.append({
            "type": "sources",
            "headline": "Sources",
            "urls": source_urls[:MAX_SOURCE_URLS],
        })

    # Every deck closes on the call to action, whatever its source.
    if "follow" not in types:
        additions.append({
            "type": "follow",
            "headline": "Follow for more",
            "sub": "Daily tech, explained.",
        })

    # Trim the BODY, never the tail. Slicing the whole deck took the cut off
    # the end, which is where the links index had just been placed — the two
    # guarantees fought and the index was created and then silently discarded,
    # so an enumeration still shipped without one.
    body = [slide for slide in slides if slide.get("type") not in CLOSING_TYPES]
    closing = [slide for slide in slides if slide.get("type") in CLOSING_TYPES]
    closing = sorted(closing + additions,
                     key=lambda s: CLOSING_TYPES.index(s["type"]))

    room = MAX_SLIDES - len(closing)
    if len(body) > room:
        body = body[:room]
    # Ordered unconditionally, so this function's output is well defined
    # whatever order its input arrived in — it orders the tail when it adds to
    # it, and skipping that when it adds nothing would be an odd exception.
    return body + closing


def _restore_repo_facts(slides: list[dict], notes: list[dict]) -> list[dict]:
    """Put the researched facts back on each repo slide.

    Star counts, URLs and logos are data, not prose. Asking a model to copy
    them verbatim mostly works, and "mostly" is not good enough for a link the
    reader is meant to type in — so they are restored from the note by name.
    """
    by_name = {}
    for note in notes:
        if note.get("name"):
            by_name[note["name"].lower()] = note

    for slide in slides:
        if slide.get("type") != "repo":
            continue
        note = by_name.get(str(slide.get("name", "")).lower())
        if not note:
            continue
        for field in ("owner", "name", "url", "stars", "language", "logo"):
            if note.get(field):
                slide[field] = note[field]
        # The prompt asks for a one-line `sub` on every repo slide, and the
        # model sometimes just does not write one — which renders as a name, a
        # star count and a URL floating on an otherwise empty card. The note's
        # own `detail` is a researched description of exactly this thing, so
        # the slide falls back to it rather than to blank space. Not truncated:
        # the renderer shrinks a slide that overflows, which keeps the whole
        # sentence instead of cutting it mid-word.
        if not str(slide.get("sub", "")).strip() and note.get("detail"):
            slide["sub"] = str(note["detail"]).strip()
    return slides


def _deck_text(slides: list[dict]) -> list[str]:
    """What a slide actually says, for the coverage check.

    Headline plus the first body field: a headline alone reads as a topic
    label, and judging coverage from labels alone marks anything vaguely
    on-topic as addressed.
    """
    out = []
    for slide in slides:
        body = ""
        for field in ("sub", "quote", "code", "caption"):
            if slide.get(field):
                body = str(slide[field])
                break
        if not body and slide.get("bullets"):
            body = "; ".join(str(b) for b in slide["bullets"])
        if not body and slide.get("rows"):
            body = "; ".join(f"{r[0]}: {r[1]}" for r in slide["rows"] if len(r) > 1)
        out.append(f"{slide.get('headline','')} — {body}".strip(" —"))
    return out


def ensure_links_slide(slides: list[dict], notes: list[dict]) -> list[dict]:
    """Give an enumeration the index a reader can screenshot.

    Measured, not assumed: across both eval arms that showed the composer a
    worked example, only one enumeration in four produced a links slide. The
    prompt asks for it and the example demonstrates it, and three times in four
    the reader still got a deck of items they could not go and find.

    The urls come from the notes rather than the slides, so what is listed is
    what was researched.
    """
    types = [slide.get("type") for slide in slides]
    if "links" in types:
        return slides

    urls: list[str] = []
    for note in notes:
        url = str(note.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    if len(urls) < 2:
        # One link is a sentence, not an index.
        return slides

    index = {
        "type": "links",
        "headline": "All the links",
        "sub": "Screenshot this slide.",
        "links": urls[:MAX_LINKS],
    }
    # The index belongs with the closing slides, and those are ordered
    # links -> sources -> follow, so it goes before whatever tail exists.
    body = [s for s in slides if s.get("type") not in CLOSING_TYPES]
    tail = [s for s in slides if s.get("type") in CLOSING_TYPES]
    if len(body) + len(tail) >= MAX_SLIDES:
        body = body[: MAX_SLIDES - len(tail) - 1]
    return body + [index] + tail


def build_caption(
    caption: str, hashtags: list[str], credit: str = "",
    limit: int = MAX_HASHTAGS,
) -> str:
    """Assemble the caption, capped at the platform's hashtag limit.

    The cap counts every hashtag in the finished caption, not only the ones
    appended here. Instagram counts what it sees, and the composer writes tags
    into the prose as well — one eval caption ended with five appended tags and
    two more mid-sentence, seven in total against a limit of five. Tags found
    in the body are therefore lifted out and folded into the same capped set
    rather than left to slip past it.

    The cap is enforced here rather than trusted to the prompt: a model that
    returns six tags would otherwise produce a caption Instagram refuses, and
    the failure would surface at publish time as an opaque API error.
    """
    caption = caption.strip()

    # Lift any tags the composer wrote into the prose. They count against the
    # platform limit exactly like the appended ones, so they have to join the
    # same pool instead of being counted separately — and they go first,
    # because the model chose to put those inline for emphasis.
    inline = [m.lower() for m in _INLINE_HASHTAG.findall(caption)]
    caption = _INLINE_HASHTAG.sub("", caption)
    # Removing tags mid-sentence leaves doubled spaces and space-before-period.
    caption = re.sub(r"[ \t]{2,}", " ", caption)
    caption = re.sub(r"\s+([.,!?])", r"\1", caption).strip()

    if credit and credit.lower() not in caption.lower():
        caption = f"{caption}\n\nSource: {credit}"

    seen: list[str] = []
    for tag in [*inline, *hashtags]:
        cleaned = str(tag).lstrip("#").strip().lower().replace(" ", "")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) >= limit:
            break

    tags = " ".join("#" + t for t in seen)
    return f"{caption}\n\n{tags}".strip() if tags else caption
