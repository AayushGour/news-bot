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
import sys

from ..budget import Source, budget_chars_for_role, pack
from ..conversation import NeedsInput
from ..coverage import unaddressed
from ..integrity import HEADLINE_COUNT_RE, check_deck, headline_counts
from ..errors import Retryable
from ..models import Item, Status

log = logging.getLogger(__name__)

#: A hashtag written into the caption prose. Requires a letter first, so "#1"
#: and a trailing "C#" are left alone — those are text, not tags.
_INLINE_HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]*)")

MIN_SLIDES = 3
MAX_SLIDES = 10  # Instagram carousel hard maximum, and Telegram album maximum.

#: How many deduplicated source URLs the NEWS prompt's flat "AVAILABLE SOURCE
#: URLS" list carries. Editorial, not a platform limit — a "sources" slide's
#: own MAX_SOURCE_URLS cap is what actually renders. List items never use
#: this: T105/D50 gives every note its own "url" via _notes_block, which
#: does not truncate at a fixed count, so this flat list would only ever be
#: redundant (or, once MAX_ITEMS exceeds 8, silently wrong) for that path —
#: see compose()'s ``item.intent != "list"`` gate below.
MAX_PROMPT_SOURCE_URLS = 8

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

#: JSON Schema fragment for every possible slide field, keyed by name. Shared
#: by every per-type variant in SLIDES_SCHEMA below, so a field's shape (what
#: a "stat" object looks like, that "series" values are numbers) is defined
#: once rather than once per slide type that happens to use it. List-shaped
#: entries here carry NO ``maxItems`` of their own — a shared field ("rows":
#: facts vs compare, "bullets": point vs repo) can bind a different cap per
#: type, so the count comes from MAX_BODY_ITEMS below and is layered on by
#: ``_field_schema`` per discriminated-union arm (SLIDES_SCHEMA) or by
#: ``_flat_field_schema`` as the widest shared value (SLIDES_SCHEMA_FLAT).
_FIELD_SCHEMAS: dict[str, dict] = {
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
    "logo": {"type": "string"},
}

#: Fields legal on every slide type, beyond "type" and "headline". Image
#: placement is cross-cutting per the prompt — "hero"/"inset" may support the
#: text of any slide, "background" is asked for on "hook" and suggested for
#: "takeaway" but not forbidden elsewhere — so it is not gated by type.
_COMMON_FIELDS = ("image", "image_mode")

#: Slide type -> the field(s) whose presence satisfies "this slide has body
#: content" (any ONE is enough — several types accept more than one shape of
#: body, e.g. a "point" slide is content whether it carries bullets, a stat,
#: or a sub).
#:
#: This is the single source of truth for the per-type body contract.
#: SLIDES_SCHEMA below builds a discriminated union straight from it, so a
#: bodyless slide is rejected by the schema itself instead of being generated
#: and then silently discarded; normalise_slides reads the exact same mapping
#: as its second, code-level guarantee for whatever a weaker or
#: schema-degraded model still gets past the schema. A `compare` slide
#: carrying `series` but no `rows` failed exactly this way before — two
#: copies of this contract, one in the schema's `required` and one here,
#: would only need to drift once for it to happen again.
REQUIRED_BODY_FIELDS: dict[str, tuple[str, ...]] = {
    "hook": ("sub",),
    "point": ("bullets", "stat", "sub"),
    "facts": ("rows",),
    "kpi": ("tiles",),
    "chart": ("series",),
    "code": ("code",),
    "flow": ("steps",),
    "compare": ("rows",),
    "quote": ("quote",),
    "photo": ("image",),
    "repo": ("name",),
    "links": ("links",),
    "takeaway": ("sub",),
    "sources": ("urls",),
    "follow": ("sub",),
}

#: Slide type -> every type-specific field the model may set on it (always a
#: superset of REQUIRED_BODY_FIELDS[type] — the body field(s) are legal to
#: set, just not the only legal fields). This is what makes the union
#: discriminated rather than just gated: a "compare" slide cannot carry
#: "series" or "stars", because those names never appear here for "compare".
SLIDE_TYPE_FIELDS: dict[str, tuple[str, ...]] = {
    "hook": ("sub",),
    "point": ("bullets", "stat", "sub"),
    "facts": ("rows", "sub"),
    "kpi": ("tiles",),
    "chart": ("series", "unit", "caption"),
    "code": ("code", "lang", "caption"),
    "flow": ("steps",),
    "compare": ("rows", "left_title", "right_title"),
    "quote": ("quote", "attribution"),
    "photo": ("caption",),
    "repo": ("owner", "name", "url", "stars", "language", "sub", "bullets", "logo"),
    "links": ("links", "sub"),
    "takeaway": ("sub",),
    "sources": ("urls",),
    "follow": ("sub",),
}

for _kind, _body in REQUIRED_BODY_FIELDS.items():
    # An explicit raise, not `assert` — this guards a real invariant the
    # schema builder below depends on, and `assert` is compiled out entirely
    # under `python -O`, which would silently drop the check rather than
    # just weaken it.
    if not set(_body) <= set(SLIDE_TYPE_FIELDS.get(_kind, ())) | {"image"}:
        raise AssertionError(
            f"{_kind}'s required body field(s) {_body} must also be declared in "
            "SLIDE_TYPE_FIELDS (or be the cross-cutting 'image' field), or the "
            "schema cannot make them legal to set"
        )
del _kind, _body


#: Per-(slide type, body-item field) capacity: how many items of that field
#: the layout holds before the renderer's overflow guard trips. Measured by
#: ``scripts/measure_slide_capacity.py`` on 2026-09-23 — pre-shrink (shrink
#: is the renderer's own headroom, not a budget compose gets to spend),
#: worst-case documented companion fields present, minimum across all 11
#: files in ``config/themes/`` (not just the 4 in THEMES — ``rotate``/
#: ``random`` in render.py ignore the model's chosen theme and can land on
#: any of the 11). Paste the script's table here again whenever
#: templates/base.html.j2, a slide template, or a theme file changes.
#:
#: Keyed by (type, field) rather than field alone: "bullets" serves both
#: "point" (38px body text, no other companions) and "repo" (30px, sharing
#: its card with a logo, owner, name, language badge, star count and url) —
#: one shared number for both was the original defect (a headline promising
#: 7 over a body showing 4, with no log and no reconciliation).
#:
#: This is a measured LAYOUT limit (coding-standards.md house rule 2), not a
#: platform limit and not an editorial one — it moves only when the layout
#: does.
MAX_BODY_ITEMS: dict[tuple[str, str], int] = {
    ("point", "bullets"): 4,
    ("repo", "bullets"): 4,
    ("facts", "rows"): 5,
    ("compare", "rows"): 6,
    ("sources", "urls"): 10,
    ("flow", "steps"): 4,
    ("kpi", "tiles"): 4,
    ("chart", "series"): 5,
    ("links", "links"): 7,
}

#: Editorial floors under MAX_BODY_ITEMS' measured ceilings — not layout
#: facts but judgement calls about when a slide of that type stops reading
#: as what it claims to be (a "flow" of one step is not a sequence; a "kpi"
#: needs at least two figures to invite a comparison). Named so the SYSTEM
#: prompt's range text has a single source for both ends (house rule 2)
#: instead of a hand-typed floor next to a derived ceiling.
_MIN_FLOW_STEPS = 3
_MIN_KPI_TILES = 2
_MIN_CHART_SERIES = 2
_MIN_REPO_BULLETS = 3


def _field_schema(kind: str, field: str) -> dict:
    """``_FIELD_SCHEMAS[field]``, with a per-(kind, field) ``maxItems``
    layered on top when MAX_BODY_ITEMS caps this exact type+field pair.

    The discriminated union can bind a genuinely different cap per arm — a
    "facts" slide's "rows" and a "compare" slide's "rows" need not agree —
    unlike SLIDES_SCHEMA_FLAT's one shared property definition (see
    ``_flat_field_schema`` below), so this reads the exact number rather
    than the widest-shared-field compromise the flat schema is stuck with.
    A shallow copy: never mutates the shared ``_FIELD_SCHEMAS`` entry the
    next arm reads too.
    """
    schema = _FIELD_SCHEMAS[field]
    cap = MAX_BODY_ITEMS.get((kind, field))
    return {**schema, "maxItems": cap} if cap is not None else schema


def _flat_field_schema(field: str) -> dict:
    """``_FIELD_SCHEMAS[field]`` widened with the WIDEST ``maxItems`` any
    type imposes on this shared field name.

    SLIDES_SCHEMA_FLAT has one property definition per field name across
    every slide type (its own docstring explains why), so a field two types
    share ("bullets": point and repo; "rows": facts and compare) cannot
    carry two different caps here — the wider of the two is used.
    normalise_slides is the real, per-type enforcement (house rule 5 —
    schema is a request, not a guarantee), so the flat schema only needs to
    be tighter than "unbounded", not exact.
    """
    schema = _FIELD_SCHEMAS[field]
    caps = [cap for (_, f), cap in MAX_BODY_ITEMS.items() if f == field]
    return {**schema, "maxItems": max(caps)} if caps else schema


#: A "code" slide's language identifier — "json", "typescript", "dockerfile".
#: Editorial safety net, not measured: every example the prompt names is well
#: under this, and it exists to bound an unexpected value rather than a real
#: layout limit.
MAX_LANG_CHARS = 12

#: A "chart" slide's axis unit — matches the "unit" <= 12" the SYSTEM prompt
#: already states.
MAX_UNIT_CHARS = 12


def _slide_variant(kind: str) -> dict:
    """Build one arm of the slides discriminated union.

    Requires the type's own body field(s) (any one, per REQUIRED_BODY_FIELDS),
    allows every other field that type and only that type uses, and forbids
    the rest via ``additionalProperties: false`` — so a "compare" slide
    carrying "series" but no "rows" fails schema validation instead of
    reaching normalise_slides to be silently discarded.

    Both lookups index directly (``[kind]``, not ``.get(kind, ())``) so a type
    added to SLIDE_TYPES without a matching entry in SLIDE_TYPE_FIELDS or
    REQUIRED_BODY_FIELDS raises KeyError here — at MODULE IMPORT TIME, since
    SLIDES_SCHEMA below calls this for every entry in SLIDE_TYPES as soon as
    the module loads. The alternative (``.get`` with a default of ``()``)
    silently built a permissive bodyless arm requiring only "type" and
    "headline" for the unregistered type: precisely the bug class this task
    exists to close, reintroduced here in the union builder's own defaults.
    Indexing ``SLIDE_TYPE_FIELDS`` alone (not unioned with
    REQUIRED_BODY_FIELDS, as a previous version did) also makes the
    module-level assert above load-bearing: that assert is what actually
    guarantees every required body field is a declared property, the "image"
    exception aside (image ships via _COMMON_FIELDS regardless of `fields`).

    T102: each list-shaped property also carries the measured, per-(kind,
    field) ``maxItems`` from MAX_BODY_ITEMS via ``_field_schema`` — AC7.
    """
    fields = set(SLIDE_TYPE_FIELDS[kind])
    properties = {
        "type": {"type": "string", "enum": [kind]},
        "headline": _FIELD_SCHEMAS["headline"],
        **{f: _FIELD_SCHEMAS[f] for f in _COMMON_FIELDS},
        **{f: _field_schema(kind, f) for f in fields},
    }
    variant: dict = {
        "type": "object",
        "properties": properties,
        "required": ["type", "headline"],
        "additionalProperties": False,
    }
    body = REQUIRED_BODY_FIELDS[kind]
    if len(body) == 1:
        variant["required"] = variant["required"] + [body[0]]
    elif body:
        # "at least one of" — a genuine OR, not expressible via "required".
        variant["anyOf"] = [{"required": [field]} for field in body]
    return variant


SLIDES_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "minItems": MIN_SLIDES,
            "maxItems": MAX_SLIDES,
            "items": {"oneOf": [_slide_variant(kind) for kind in SLIDE_TYPES]},
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "theme": {"type": "string", "enum": THEMES},
    },
    "required": ["slides", "caption", "hashtags", "theme"],
}

#: Every field any slide type uses, flattened into one object instead of
#: SLIDES_SCHEMA's 15-arm discriminated union. Built from the same
#: registries (SLIDE_TYPE_FIELDS, _COMMON_FIELDS, _FIELD_SCHEMAS) so the two
#: schemas cannot drift apart — this sheds only per-type discrimination,
#: which a prompt-embedded schema was never enforcing anyway (see
#: normalise_slides for why neither provider's schema enforcement is trusted
#: on its own). Used in place of SLIDES_SCHEMA only on OpenRouter's degraded
#: json_object path (_schema_prompt in llm.py): the full union serializes to
#: 6735 chars and that function embeds its schema argument verbatim into the
#: system prompt on every degraded call — the exact call where context is
#: already tightest (the local fallback runs at num_ctx_good's fixed
#: budget). SYSTEM already spells out every type's fields and per-field
#: character limits in prose, which is strictly more information than this
#: schema adds, so the degraded path loses nothing by shedding the union.
_ALL_TYPE_FIELDS = sorted({field for fields in SLIDE_TYPE_FIELDS.values() for field in fields})

SLIDES_SCHEMA_FLAT = {
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
                    "headline": _FIELD_SCHEMAS["headline"],
                    **{f: _FIELD_SCHEMAS[f] for f in _COMMON_FIELDS},
                    **{f: _flat_field_schema(f) for f in _ALL_TYPE_FIELDS},
                },
                "required": ["type", "headline"],
                "additionalProperties": False,
            },
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "theme": {"type": "string", "enum": THEMES},
    },
    "required": ["slides", "caption", "hashtags", "theme"],
}

_SYSTEM_TEMPLATE = """You write Instagram carousel slides for a tech and AI news account.

Turn the brief into slides. Choose how many (3-10) based on how much the
research actually supports. Do not pad to reach a number.

Slide types and their character limits. These are what the template can fit;
going over means text gets cut:

- "hook":     headline <= 60, sub <= 90.  Exactly one, always first.
- "point":    headline <= 45, up to <<POINT_BULLETS>> "bullets" each <= 95.
              Optional "stat": {value <= 12, label <= 45}.
- "facts":    headline <= 45, up to <<FACTS_ROWS>> "rows", each row exactly
              [label <= 30, value <= 34]. A label/value table, not a comparison.
- "code":     headline <= 45, "lang" (e.g. json, python, bash, yaml, text),
              "code" <= 420 chars and at most 12 lines, optional "caption"
              <= 90. Use REAL syntax for the thing being described - an actual
              config snippet, request body, file tree, or command. Never
              pseudocode, never invented API names.
- "flow":     headline <= 45, <<FLOW_STEPS_MIN>>-<<FLOW_STEPS_MAX>> "steps",
              each {label <= 26, detail <= 70}. For a process, pipeline, or
              sequence of events in order.
- "compare":  headline <= 45, "left_title" and "right_title" <= 22 each, up to
              <<COMPARE_ROWS>> "rows" as [left <= 40, right <= 40]. A genuine
              A-vs-B: before and after, us and them, old and new. Not a
              label/value table - that is "facts".
- "quote":    headline <= 40, "quote" <= 180, "attribution" <= 40. Only when
              the brief contains an actual quoted statement. Never fabricate
              or paraphrase one into quotation marks.
- "photo":    headline <= 45, optional "caption" <= 90, and "image" set to the
              index of an available image. Only for images rated "hero".
- "kpi":      headline <= 45, <<KPI_TILES_MIN>>-<<KPI_TILES_MAX>> "tiles",
              each {value <= 10 chars, label <= 28, optional delta <= 10 like
              "+38%" or "-2pts"}. For headline figures that stand on their
              own.
- "chart":    headline <= 45, <<CHART_SERIES_MIN>>-<<CHART_SERIES_MAX>>
              "series" entries, each {label <= 20, value: a NUMBER, optional
              display like "$65B"}, optional "unit" <= 12 and "caption" <=
              80. Renders as a horizontal bar chart. Values must be
              COMPARABLE — the same measure on the same scale, since they
              share one axis. Never mix a count with a percentage.
- "takeaway": headline <= 55, sub <= 110. Exactly one, near the end.
- "sources":  headline <= 45, up to <<SOURCES_URLS>> "urls". Exactly one,
              always last.
- "links":    headline <= 45, up to <<LINKS_LINKS>> "links". The pipeline
              builds this automatically for a list deck's index — write one
              yourself only if you need it elsewhere, and keep it under the
              cap above.

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

#: Token -> value for _SYSTEM_TEMPLATE. Plain string tokens rather than
#: str.format()/an f-string: the template's prose already uses literal curly
#: braces ("{label <= 26, detail <= 70}") to describe a dict shape to the
#: model, and escaping every one of those would be far easier to get wrong
#: than a straight .replace() pass. Generated from MAX_BODY_ITEMS — never
#: hand-edit a number in _SYSTEM_TEMPLATE directly.
_SYSTEM_TOKENS = {
    "<<POINT_BULLETS>>": MAX_BODY_ITEMS[("point", "bullets")],
    "<<FACTS_ROWS>>": MAX_BODY_ITEMS[("facts", "rows")],
    "<<COMPARE_ROWS>>": MAX_BODY_ITEMS[("compare", "rows")],
    "<<FLOW_STEPS_MIN>>": _MIN_FLOW_STEPS,
    "<<FLOW_STEPS_MAX>>": MAX_BODY_ITEMS[("flow", "steps")],
    "<<KPI_TILES_MIN>>": _MIN_KPI_TILES,
    "<<KPI_TILES_MAX>>": MAX_BODY_ITEMS[("kpi", "tiles")],
    "<<CHART_SERIES_MIN>>": _MIN_CHART_SERIES,
    "<<CHART_SERIES_MAX>>": MAX_BODY_ITEMS[("chart", "series")],
    "<<SOURCES_URLS>>": MAX_BODY_ITEMS[("sources", "urls")],
    "<<LINKS_LINKS>>": MAX_BODY_ITEMS[("links", "links")],
}


def _render_system(template: str) -> str:
    for token, value in _SYSTEM_TOKENS.items():
        template = template.replace(token, str(value))
    return template


SYSTEM = _render_system(_SYSTEM_TEMPLATE)

# N4: a token added to _SYSTEM_TEMPLATE without a matching _SYSTEM_TOKENS
# entry (or a typo in one) would otherwise ship "<<LIKE_THIS>>" straight into
# the model's SYSTEM prompt, silently — the existing test only checked 7 of
# the 11 registered tokens, so this class of mistake had no guard at all.
# Module-level so it fails at import time, before any request goes out.
if "<<" in SYSTEM:
    raise AssertionError(
        "compose.SYSTEM: an unreplaced <<TOKEN>> reached the rendered prompt"
    )


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
    if source_urls and item.intent != "list":
        # A list item's notes carry their own "url" field, restated in full
        # by _notes_block below — this flat, truncated-at-8 list would only
        # ever duplicate or (past MAX_ITEMS=8) contradict that (D50/T105).
        parts.append(
            "AVAILABLE SOURCE URLS:\n"
            + "\n".join(source_urls[:MAX_PROMPT_SOURCE_URLS])
        )

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
        # D50/T105: item.research used to reach this function only at the
        # harvest above and in the post-hoc repairs below (check_deck,
        # _restore_repo_facts, ensure_links_slide) — never the prompt itself.
        # The DECK SHAPE instruction told the model to copy owner/name/url/
        # stars/language from the note VERBATIM, from a note it was never
        # shown, so it reverse-engineered owner/name from the URLs above and
        # invented the rest. This is the fix: the notes themselves, in rank
        # order, budgeted the same way research.py budgets a corpus.
        budget_chars = (
            budget_chars_for_role(settings, role="good") if settings else None
        )
        notes_block = _notes_block(item.research or [], budget_chars)
        if notes_block:
            parts.append(notes_block)

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
            f"     saying what it is and who it is for, plus "
            f"{_MIN_REPO_BULLETS}-{MAX_BODY_ITEMS[('repo', 'bullets')]} short\n"
            "     \"bullets\" (<= 60 chars each) covering what it does, what\n"
            "     stands out, and who should reach for it. A card with only a\n"
            "     name and a star count wastes most of the slide. If a note\n"
            "     does not carry one of those fields — a real repo can come\n"
            "     back with no star count or no detected language — LEAVE\n"
            "     THAT FIELD OUT of the slide entirely. Do not invent a\n"
            "     plausible number or language; an absent fact is not yours\n"
            "     to guess.\n"
            f"  3. a \"links\" slide listing up to {MAX_LINKS} urls, in rank\n"
            "     order, so the reader can find them from one screenshot\n"
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

    doc = await llm.good(
        SYSTEM, "\n\n".join(parts), schema=SLIDES_SCHEMA,
        degraded_schema=SLIDES_SCHEMA_FLAT, temperature=0.6,
    )
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
            SYSTEM, "\n\n".join(retry), schema=SLIDES_SCHEMA,
            degraded_schema=SLIDES_SCHEMA_FLAT, temperature=0.6,
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
            SYSTEM, "\n\n".join(fixed), schema=SLIDES_SCHEMA,
            degraded_schema=SLIDES_SCHEMA_FLAT, temperature=0.4,
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
    slides = drop_duplicate_ctas(slides)
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


#: Note fields the DECK SHAPE instruction names verbatim ("Copy owner, name,
#: url, stars and language from the note"), in the order it names them,
#: besides "name" itself — _notes_block always puts that first in the
#: header, ahead of these, rather than where DECK SHAPE's prose has it.
#: enumerate_items.py's ``_note`` is the one producer of these keys — kept
#: here rather than re-typed so a renamed note field fails this function
#: loudly (KeyError-free ``.get``) instead of silently going missing from
#: the prompt.
_NOTE_FACT_FIELDS = ("owner", "url", "stars", "language")


def _notes_block(notes: list[dict], budget_chars: int | None = None) -> str:
    """Render the enumeration's research notes into the compose prompt.

    D50/T105: before this existed, ``item.research`` reached compose.py only
    for post-hoc work — harvesting source_urls, ``check_deck``,
    ``_restore_repo_facts``, ``ensure_links_slide`` — never the prompt the
    model actually reads. The DECK SHAPE instruction nonetheless told the
    model to "copy owner, name, url, stars and language from the note
    VERBATIM" — from a note it had never been shown. It reverse-engineered
    owner/name from the URLs in AVAILABLE SOURCE URLS and invented the rest;
    ``_restore_repo_facts`` rescued only the cases where the guessed name
    happened to match a real one.

    One paragraph per note, in rank order: index, name (falling back to
    ``claim`` for a catalogue with no repo-shaped fields), detail, and
    whichever of owner/url/stars/language the note actually carries — a
    "web"-kind note from a non-repo catalogue has none of those, and nothing
    here manufactures them.

    Reuses ``budget.pack()`` (T70) rather than re-implementing trimming: each
    note becomes one ``Source``, fair-shared across ``budget_chars`` so a
    15-note list (D53) cannot silently blow ``num_ctx_good`` on the local
    fallback (mirrors ``research.py``'s corpus packing). Nothing is dropped
    without a trace (D5): pack()'s own "[... N chars omitted ...]" marker
    survives into the prompt, and ``kept_chars + omitted_chars ==
    original_chars`` holds per note regardless.

    ``pack()`` always runs, even with ``budget_chars=None`` (no ``settings``,
    e.g. a direct unit test) — it is what applies ``_sanitize`` (marker-
    forgery stripping, see budget.py's module docstring), and that guarantee
    belongs to this function, not to whichever caller happened to pass a
    real budget. ``None`` packs against ``sys.maxsize`` instead of skipping
    packing outright, so every note still fits inside its share and nothing
    is actually trimmed (the returned text is unpacked-note-identical) —
    only sanitization is structural here, not truncation.
    """
    if not notes:
        return ""

    paragraphs = []
    for i, note in enumerate(notes, start=1):
        name = str(note.get("name") or note.get("claim") or "").strip()
        header_fields = [f"name={name}"] if name else []
        for field in _NOTE_FACT_FIELDS:
            value = note.get(field)
            if value not in (None, ""):
                header_fields.append(f"{field}={value}")
        header = (f"[{i}] " + " ".join(header_fields)) if header_fields else f"[{i}]"
        detail = str(note.get("detail") or "").strip()
        paragraphs.append(f"{header}\n{detail}" if detail else header)

    packed = pack(
        [Source(id=str(i), text=p) for i, p in enumerate(paragraphs, start=1)],
        budget_chars if budget_chars is not None else sys.maxsize,
    )
    if any(p.omitted_chars for p in packed):
        # %-style, not an eager f-string: note text/detail is untrusted
        # (fetched web content, same as research.py's corpus) and %-style
        # logging defers str()/repr() of the manifest until formatting,
        # which escapes embedded newlines rather than emitting them as
        # real log lines — see budget.py / research.py for the full case.
        log.warning(
            "list notes block packed into %d/%d chars budget: %s",
            sum(len(p.text) for p in packed), budget_chars,
            [{"note": p.id, "kept": p.kept_chars, "omitted": p.omitted_chars}
             for p in packed],
        )
    text = "\n\n".join(p.text for p in packed)

    return "RESEARCHED ITEMS:\n" + text


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


def _cap_items(kind: str, field: str, raw: list, cap: int, clean) -> list:
    """Clean and cap one body-item list, logging what did not survive.

    ``clean`` maps one raw item to its normalised form, or ``None`` to
    discard it (an empty string, a malformed row). ``received`` counts every
    raw item the model sent for this field, so ``kept + dropped == received``
    always, by construction — bytes may be dropped, never without a trace
    (coding-standards.md house rule 7).
    """
    kept = [v for v in (clean(item) for item in raw[:cap]) if v is not None]
    received = len(raw)
    dropped = received - len(kept)
    if dropped:
        log.info("trim %s.%s: kept %d dropped %d received %d",
                  kind, field, len(kept), dropped, received)
    return kept


#: The (type, field) MAX_BODY_ITEMS falls back to per field name, when a
#: slide names a field it does not carry in SLIDE_TYPE_FIELDS for its own
#: type — reachable on the unenforced/degraded schema path (D18/D27), where
#: a model can set "bullets" on a "facts" slide despite the contract. One
#: default per field name, not a hand-picked one at each of _cap_for's
#: (formerly seven) call sites.
_DEFAULT_CAP_KEY: dict[str, tuple[str, str]] = {
    "bullets": ("point", "bullets"),
    "rows": ("facts", "rows"),
    "urls": ("sources", "urls"),
    "steps": ("flow", "steps"),
    "tiles": ("kpi", "tiles"),
    "series": ("chart", "series"),
    "links": ("links", "links"),
}


def _cap_for(kind: str, field: str) -> int:
    """MAX_BODY_ITEMS[(kind, field)], or ``field``'s default cap when this
    exact (kind, field) pair has none (N2: replaces seven identical
    ``MAX_BODY_ITEMS.get((kind, F), MAX_BODY_ITEMS[(<default>, F)])`` call
    sites in normalise_slides, each with its own hand-picked default and
    only one of which — "bullets" — had a test)."""
    return MAX_BODY_ITEMS.get((kind, field), MAX_BODY_ITEMS[_DEFAULT_CAP_KEY[field]])


def _clean_text_item(value) -> str | None:
    """A bare string body item — a bullet, a url, a link."""
    text = str(value).strip()
    return text or None


def _clean_row(row) -> list | None:
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        return None
    return [str(row[0]).strip(), str(row[1]).strip()]


def _clean_step(step) -> dict | None:
    if not isinstance(step, dict):
        return None
    label = str(step.get("label", "")).strip()
    if not label:
        return None
    return {"label": label, "detail": str(step.get("detail", "")).strip()}


def _clean_tile(tile) -> dict | None:
    if not isinstance(tile, dict):
        return None
    value = str(tile.get("value", "")).strip()
    if not value:
        return None
    return {"value": value, "label": str(tile.get("label", "")).strip(),
            "delta": str(tile.get("delta", "")).strip()}


def _clean_series_point(point) -> dict | None:
    if not isinstance(point, dict):
        return None
    try:
        value = float(point["value"])
    except (KeyError, TypeError, ValueError):
        return None
    label = str(point.get("label", "")).strip()
    if not label:
        return None
    return {
        "label": label, "value": value,
        "display": str(point.get("display", "")).strip() or _fmt(value),
    }


#: Which body-item field, if any, a slide type's headline might be naming a
#: count of — the same field MAX_BODY_ITEMS caps for that type. Used only to
#: decide whether a headline needs reconciling after the trim (see
#: reconcile_headline_counts, beside drop_duplicate_ctas below).
#:
#: DERIVED from MAX_BODY_ITEMS (house rule 3 — one registry, not a hand-typed
#: restatement of it). This used to be its own literal dict, byte-identical
#: to `{t: f for (t, f) in MAX_BODY_ITEMS}` but unpinned to it — deleting an
#: entry from MAX_BODY_ITEMS left this one untouched, so a "facts" slide
#: could trim 8 bullets to 5 with the reconciler silently unable to see it
#: (the whole suite stayed green). The guard below is what makes the
#: derivation well-defined: a type with two capped fields would collide on
#: one dict key, and this must never happen silently.
_HEADLINE_ITEM_FIELD: dict[str, str] = {kind: field for (kind, field) in MAX_BODY_ITEMS}
if len(_HEADLINE_ITEM_FIELD) != len(MAX_BODY_ITEMS):
    raise AssertionError(
        "MAX_BODY_ITEMS caps two different fields on the same slide type — "
        "_HEADLINE_ITEM_FIELD's type -> field derivation is only well-defined "
        "while every type has exactly one capped field"
    )

#: Internal bookkeeping key normalise_slides stashes the model's original
#: item count under (before _cap_items trims it); reconcile_headline_counts
#: reads and strips it. Never reaches a saved deck. Named once (house rule
#: 1 — was re-typed 8 times across this module).
_RECEIVED_COUNT_KEY = "_received_count"


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
        # by more than one field. SLIDES_SCHEMA asks the model for this per
        # REQUIRED_BODY_FIELDS, via a discriminated union — but "sent as a
        # schema" is not "enforced", on EITHER provider. Ollama's `format`
        # does not bind the top-level `oneOf` at all: confirmed live,
        # SLIDES_SCHEMA sent whole to qwen3:4b-instruct accepted a bodyless
        # `compare`, a `compare` carrying `series` with no `rows` (the exact
        # bug this task closes), a bodyless `point`, and a `point` carrying
        # `tiles`+`stars`. A single arm sent alone DOES bind there
        # (`additionalProperties: false` is enforced), so the hole is
        # specifically the `oneOf`. OpenRouter's `strict: true` json_schema is
        # NOT reliable either — confirmed live against whatever model
        # OPENROUTER_MODEL_GOOD was pinned to at the time. (The slug itself
        # has already been repinned once, by T69; naming it here would only
        # go stale — this is a finding about the PROVIDER's enforcement, not
        # about any one model.) Despite `strict: true`, its 200 response used
        # "subheadline" and "background" on a hook, "valueA"/"valueB" objects
        # in place of `rows`' 2-tuples on a compare, and "items" instead of
        # `urls` on sources — none of that is in the schema at all. Whether a given
        # slug actually honours `strict` is per-provider and currently
        # unverified; T82 tracks probing it properly, and MODEL_GOOD_FALLBACK
        # set to a local GGUF means compose also runs Ollama's unenforced path
        # whenever OpenRouter fails. Do not trust either provider's schema as
        # load-bearing on its own. The check below is therefore a
        # LIVE BACKSTOP on the PRIMARY path as well as the fallback,
        # not belt-and-braces — do not remove it on the theory the schema
        # already covers it. It reads the exact same REQUIRED_BODY_FIELDS
        # mapping the schema is built from, so the two cannot drift apart. Fixing either
        # provider's enforcement is out of scope here (T82).
        required_any = REQUIRED_BODY_FIELDS.get(kind, ())
        if required_any and not any(
            slide.get(field) not in (None, "", [], {}) for field in required_any
        ):
            continue
        entry = {"type": kind, "headline": str(slide["headline"]).strip()}
        if slide.get("sub"):
            entry["sub"] = str(slide["sub"]).strip()
        if slide.get("bullets"):
            cap = _cap_for(kind, "bullets")
            entry["bullets"] = _cap_items(kind, "bullets", slide["bullets"], cap, _clean_text_item)
            if _HEADLINE_ITEM_FIELD.get(kind) == "bullets":
                entry[_RECEIVED_COUNT_KEY] = len(slide["bullets"])
        if isinstance(slide.get("stat"), dict) and slide["stat"].get("value"):
            entry["stat"] = {
                "value": str(slide["stat"].get("value", "")),
                "label": str(slide["stat"].get("label", "")),
            }
        if slide.get("rows"):
            cap = _cap_for(kind, "rows")
            rows = _cap_items(kind, "rows", slide["rows"], cap, _clean_row)
            if rows:
                entry["rows"] = rows
            if _HEADLINE_ITEM_FIELD.get(kind) == "rows":
                entry[_RECEIVED_COUNT_KEY] = len(slide["rows"])
        if slide.get("urls"):
            cap = _cap_for(kind, "urls")
            entry["urls"] = _cap_items(kind, "urls", slide["urls"], cap, _clean_text_item)
            if _HEADLINE_ITEM_FIELD.get(kind) == "urls":
                entry[_RECEIVED_COUNT_KEY] = len(slide["urls"])
        if slide.get("code"):
            # Keep newlines and indentation; they are the content here.
            entry["code"] = str(slide["code"]).rstrip()[:900]
            entry["lang"] = str(slide.get("lang", "")).strip().lower()[:MAX_LANG_CHARS]
        if slide.get("caption"):
            entry["caption"] = str(slide["caption"]).strip()
        if slide.get("steps"):
            cap = _cap_for(kind, "steps")
            steps = _cap_items(kind, "steps", slide["steps"], cap, _clean_step)
            if steps:
                entry["steps"] = steps
            if _HEADLINE_ITEM_FIELD.get(kind) == "steps":
                entry[_RECEIVED_COUNT_KEY] = len(slide["steps"])
        if slide.get("tiles"):
            cap = _cap_for(kind, "tiles")
            tiles = _cap_items(kind, "tiles", slide["tiles"], cap, _clean_tile)
            if tiles:
                entry["tiles"] = tiles
            if _HEADLINE_ITEM_FIELD.get(kind) == "tiles":
                entry[_RECEIVED_COUNT_KEY] = len(slide["tiles"])
        if slide.get("series"):
            cap = _cap_for(kind, "series")
            entry_series = _cap_items(kind, "series", slide["series"], cap, _clean_series_point)
            if entry_series:
                # Bar length is computed here so the template stays declarative
                # and cannot divide by zero on a flat series.
                widest = max(abs(p["value"]) for p in entry_series) or 1.0
                for point in entry_series:
                    point["pct"] = round(abs(point["value"]) / widest * 100, 1)
                entry["series"] = entry_series
                if slide.get("unit"):
                    entry["unit"] = str(slide["unit"]).strip()[:MAX_UNIT_CHARS]
            if _HEADLINE_ITEM_FIELD.get(kind) == "series":
                entry[_RECEIVED_COUNT_KEY] = len(slide["series"])
        for key in ("left_title", "right_title", "quote", "attribution",
                    "owner", "name", "url", "language"):
            if slide.get(key):
                entry[key] = str(slide[key]).strip()
        if isinstance(slide.get("stars"), int):
            entry["stars"] = slide["stars"]
        if slide.get("links"):
            cap = _cap_for(kind, "links")
            entry["links"] = _cap_items(kind, "links", slide["links"], cap, _clean_text_item)
            if _HEADLINE_ITEM_FIELD.get(kind) == "links":
                entry[_RECEIVED_COUNT_KEY] = len(slide["links"])

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

    return reconcile_headline_counts(ordered[:MAX_SLIDES])


#: Instagram rejects captions carrying more than this many hashtags.
MAX_HASHTAGS = 5

#: Links the index slide can show before the template runs out of room.
#: Derived from MAX_BODY_ITEMS — do not re-type this number (house rule 1;
#: it duplicated the registry once already and drifted).
MAX_LINKS = MAX_BODY_ITEMS[("links", "links")]

#: How many urls a generated sources slide carries, matching the prompt's cap.
#: Derived from MAX_BODY_ITEMS — do not re-type this number (house rule 1).
MAX_SOURCE_URLS = MAX_BODY_ITEMS[("sources", "urls")]


#: A body slide that is really a second call to action. The composer sometimes
#: writes its own "Follow for more" as a `point`, and ensure_closing_slide then
#: appends the real one — item 116 shipped nine slides ending on two CTAs, the
#: last of them the generic "Daily tech, explained." under a psychology deck.
_CTA = re.compile(r"\bfollow\s+(for|us|me|along)\b|\bsubscribe\b", re.IGNORECASE)


def reconcile_headline_counts(slides: list[dict]) -> list[dict]:
    """Rewrite a headline's promised count to match what actually shipped.

    normalise_slides trims each body-item field to MAX_BODY_ITEMS and, on the
    one field a type's headline might be counting, records what the model
    originally sent as ``_received_count``. A headline written before that
    trim can still promise the original number — "7 Best Practices" over 4
    surviving bullets, the exact complaint that opened this task. This checks
    whether the headline names EXACTLY that received count and, only then,
    rewrites it to the number that actually shipped.

    Deterministic, zero model calls, one right answer — the same pattern
    `drop_duplicate_ctas` and `ensure_closing_slide` already use for a
    structural mistake (coding-standards.md house rule 8). It never fires on
    a version, year, multiplier, ratio, money figure, or a hyphenated range
    (`integrity.headline_counts` excludes those by construction), and it only
    ever rewrites a number that equals what THIS slide's OWN field received —
    not a guess about what the model meant.

    That is NOT the same as "never on a coincidental match" — a false claim
    this docstring used to make. A proper noun with a trailing version number
    ("Windows 7 finally dies") can equal the received count purely by
    coincidence and gets rewritten exactly like a genuine one ("Top 7
    picks") would, because the two are textually identical: same shape, same
    position, same digit. Nothing at this level can tell them apart without
    knowing "Windows" names a product — see `integrity.HEADLINE_COUNT_RE`'s
    own docstring. Known, undecided residual (T102 bounce 1), not silently
    claimed away.

    A headline that promised a count the model never delivered in the first
    place — it wrote "7 Best Practices" over only 4 bullets, so nothing here
    was ever trimmed — is a different, unrelated defect. There is no single
    correct number to substitute without asking the model, which D39 rules
    out for a deterministic repair, so it is left alone.
    """
    for slide in slides:
        received = slide.pop(_RECEIVED_COUNT_KEY, None)
        field = _HEADLINE_ITEM_FIELD.get(slide.get("type"))
        if received is None or not field:
            continue
        kept = len(slide.get(field) or [])
        if kept == received:
            continue  # nothing was trimmed; the headline is still accurate
        headline = slide.get("headline", "")
        if headline_counts(headline).count(received) != 1:
            continue  # ambiguous or no match — never guess
        target = str(received)
        replacement = str(kept)
        slide["headline"] = HEADLINE_COUNT_RE.sub(
            lambda m: replacement if m.group(1) == target else m.group(0),
            headline,
        )
    return slides


def drop_duplicate_ctas(slides: list[dict]) -> list[dict]:
    """Keep the deck's own closing slide and strip CTAs written as body.

    Deterministic rather than a rebuild: a second call to action is a
    structural mistake with one right answer, and asking the model again costs
    a call to fix something we can simply remove.
    """
    kept: list[dict] = []
    for slide in slides:
        if slide.get("type") != "follow" and _CTA.search(
            f"{slide.get('headline', '')} {slide.get('sub', '')}"
        ):
            continue
        kept.append(slide)
    return kept


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

    T111: this used to be `if note.get(field): slide[field] = note[field]` —
    truthy, not presence-aware. That can only ever SKIP a field, never STRIP
    one, so a model-fabricated `stars=4200` survived untouched whenever the
    note had no stars at all (a "web"-kind catalogue note has none, and even
    a repo note can come back with `stars=None` when the candidate carried no
    popularity signal, or `language=""` when nothing matched `_LANG`). Worse,
    a genuine `stars: 0` — a real, newly-published repo — is falsy too, so it
    could never overwrite a fabricated non-zero either. Presence in the note
    is now the question, not truthiness of the note's value: absent (key
    missing, or explicitly `None`/`""` — enumerate_items.py's own sentinels
    for "nothing here", see `_notes_block`) strips whatever the model wrote,
    since an unverifiable guess must not survive; present — including a real
    `0` — writes it, because `0 not in (None, "")`.
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
            value = note.get(field)
            if value not in (None, ""):
                slide[field] = value
            else:
                slide.pop(field, None)
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

    B4: this is the pipeline's OWN trim, generated after normalise_slides has
    already run — a model-written links slide's urls get capped by
    _cap_items (traced, logged) inside normalise_slides, but this one used to
    slice with a bare ``urls[:MAX_LINKS]`` that traced nothing. A list can
    carry up to MAX_ITEMS (D53) researched notes, so once that exceeds
    MAX_LINKS this WAS a silent drop through the pipeline's own index — the
    identical defect this task exists to close, reachable through the one
    code path D34/D5 had not yet covered. Routed through _cap_items so the
    two paths are traced the same way.
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
        "links": _cap_items("links", "links", urls, MAX_LINKS, _clean_text_item),
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
