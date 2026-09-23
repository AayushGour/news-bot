import logging
from dataclasses import replace

import jsonschema
import pytest

from pipeline.budget import _MARKER_SENTINEL, budget_chars_for_role, marker_present
from pipeline.errors import Retryable
from pipeline.models import Item, Status
from pipeline.stages.compose import (
    MAX_HASHTAGS,
    MAX_SLIDES,
    REQUIRED_BODY_FIELDS,
    SLIDE_TYPES,
    SLIDES_SCHEMA,
    MAX_SOURCE_URLS,
    _NOTE_FACT_FIELDS,
    _notes_block,
    build_caption,
    ensure_closing_slide,
    ensure_links_slide,
    compose,
    drop_duplicate_ctas,
    normalise_slides,
)
from pipeline.stages.synthesize import synthesize


def _item(**kw):
    base = dict(id=1, source="channel", status=Status.SYNTHESIZED,
                raw_text="news", brief="THE BRIEF",
                research=[{"claim": "c", "sources": ["https://a.example/1"]}])
    base.update(kw)
    return Item(**base)


def _doc(slides=None, caption="A caption.", hashtags=None):
    return {
        "slides": slides or [
            {"type": "hook", "headline": "Big news", "sub": "Why it matters."},
            {"type": "point", "headline": "Detail", "bullets": ["a", "b"]},
            {"type": "takeaway", "headline": "So what", "sub": "The consequence."},
        ],
        "caption": caption,
        "hashtags": hashtags or ["ai", "tech"],
    }


# ----------------------------------------------------------------- synthesize


async def test_synthesize_returns_brief_and_demands_attribution(fake_llm):
    fake_llm.queue("OpenAI blocked Cursor (source: https://a.example/1).")
    out = await synthesize(_item(status=Status.RESEARCHED), fake_llm)

    assert "OpenAI blocked Cursor" in out["brief"]
    assert "Attribute every fact to its source" in fake_llm.calls[0].system
    assert "contradict" in fake_llm.calls[0].system


async def test_synthesize_without_notes_is_retryable(fake_llm):
    with pytest.raises(Retryable):
        await synthesize(_item(research=[]), fake_llm)


async def test_synthesize_rejects_an_empty_brief(fake_llm):
    fake_llm.queue("   ")
    with pytest.raises(Retryable, match="empty brief"):
        await synthesize(_item(), fake_llm)


# -------------------------------------------------------------------- schema


def _variant(kind: str) -> dict:
    """The discriminated-union arm for one slide type."""
    variants = SLIDES_SCHEMA["properties"]["slides"]["items"]["oneOf"]
    for variant in variants:
        if variant["properties"]["type"]["enum"] == [kind]:
            return variant
    raise AssertionError(f"no schema variant for slide type {kind!r}")


def test_facts_and_compare_are_distinct_types():
    """The PoC had only `compare`, and the model used it as a label/value
    table. Both now exist with separate jobs: `facts` is label/value, `compare`
    is a genuine two-column A-vs-B with column titles."""
    variants = SLIDES_SCHEMA["properties"]["slides"]["items"]["oneOf"]
    enum = [v["properties"]["type"]["enum"][0] for v in variants]
    assert "facts" in enum and "compare" in enum
    assert enum == SLIDE_TYPES

    props = _variant("compare")["properties"]
    assert "left_title" in props and "right_title" in props, \
        "compare needs column titles to be distinguishable from facts"
    assert "left_title" not in _variant("facts")["properties"], \
        "facts is a label/value table, not a two-column comparison"


def test_schema_clamps_slide_count_to_instagram_maximum():
    slides = SLIDES_SCHEMA["properties"]["slides"]
    assert slides["minItems"] == 3
    assert slides["maxItems"] == MAX_SLIDES == 10


#: Sample value for each body field, used only to prove *presence* satisfies
#: the schema — the field's own value type is already covered by the
#: (unchanged) per-field JSON Schema fragments, not retested here.
_SAMPLE_BODY_VALUE = {
    "sub": "s", "bullets": ["b"], "stat": {"value": "5", "label": "l"},
    "rows": [["a", "b"]], "tiles": [{"value": "5", "label": "l"}],
    "series": [{"label": "l", "value": 1}], "code": "x = 1",
    "steps": [{"label": "l"}], "quote": "q", "image": 0, "name": "n",
    "links": ["https://a"], "urls": ["https://a"],
}


def _schema_allows(slide: dict) -> bool:
    """Minimal reimplementation of what SLIDES_SCHEMA's discriminated union
    checks for one slide: every ``required`` key present, at least one
    ``anyOf`` alternative satisfied (when the variant has one), and no key
    outside what ``additionalProperties: false`` declares legal.

    Exercises the exact constructs ``_slide_variant`` builds without a
    dependency on the optional ``jsonschema`` package (not installed here).
    """
    variant = _variant(slide["type"])
    if not all(key in slide for key in variant["required"]):
        return False
    if variant.get("anyOf") and not any(
        all(key in slide for key in alt["required"]) for alt in variant["anyOf"]
    ):
        return False
    if variant["additionalProperties"] is False and not set(slide) <= set(variant["properties"]):
        return False
    return True


def test_compare_with_series_but_no_rows_is_rejected_by_the_schema():
    """The exact failure this task fixes: golden-set items 3 and 33 shipped a
    `compare` slide carrying `series`/`tiles`/`links` but never `rows`, and
    the old flat property bag called that schema-valid (`normalise_slides`
    then silently discarded it via `required_any`). The union must reject it
    outright, and `series` must not even be a legal field on `compare`."""
    slide = {"type": "compare", "headline": "H",
             "series": [{"label": "l", "value": 1}]}
    assert not _schema_allows(slide)
    assert "series" not in _variant("compare")["properties"]


def test_schema_forbids_other_types_fields_on_a_variant():
    variant = _variant("compare")
    assert variant["additionalProperties"] is False
    assert "stars" not in variant["properties"]
    assert "tiles" not in variant["properties"]


def test_every_variants_body_requirement_comes_from_required_body_fields():
    """Prove the schema is BUILT from REQUIRED_BODY_FIELDS, the same mapping
    normalise_slides reads — not a second, independently-written copy of it
    that could silently drift out of sync."""
    for kind in SLIDE_TYPES:
        variant = _variant(kind)
        body = REQUIRED_BODY_FIELDS[kind]
        if len(body) == 1:
            assert body[0] in variant["required"]
            assert variant.get("anyOf") is None
        else:
            alternatives = {tuple(alt["required"]) for alt in variant["anyOf"]}
            assert alternatives == {(field,) for field in body}


@pytest.mark.parametrize("kind", SLIDE_TYPES)
def test_minimal_body_passes_and_bare_headline_fails_the_schema(kind):
    """Table-driven over SLIDE_TYPES / REQUIRED_BODY_FIELDS: a new slide type
    gets this coverage automatically, it cannot be added without a test."""
    body_field = REQUIRED_BODY_FIELDS[kind][0]
    minimal = {"type": kind, "headline": "H", body_field: _SAMPLE_BODY_VALUE[body_field]}
    assert _schema_allows(minimal), f"{kind}'s minimal body form should satisfy the schema"

    bare = {"type": kind, "headline": "H"}
    assert not _schema_allows(bare), f"{kind} with no body should be rejected by the schema"


# ------------------------------------------------- real jsonschema (T78 fix)
#
# T78's reviewer installed real `jsonschema` and validated the actual schema
# object rather than trusting the hand-rolled `_schema_allows` above.
# `_schema_allows` is a legitimate reimplementation of the same constructs,
# but it is structurally blind to value TYPES, to oneOf exclusivity, and — the
# thing that actually mattered — to shapes our own code produces outside the
# model's completion. That blind spot is exactly why it could not find B1 by
# construction: `_schema_allows` never runs against anything ensure_links_slide,
# ensure_closing_slide, drop_duplicate_ctas or _restore_repo_facts emit.


def _validate_real(slide: dict) -> None:
    """Validate one slide against the actual SLIDES_SCHEMA union, with the
    real `jsonschema` library. Raises jsonschema.exceptions.ValidationError
    on any mismatch — type, missing required field, or a forbidden
    (additionalProperties) key."""
    item_schema = SLIDES_SCHEMA["properties"]["slides"]["items"]
    jsonschema.validate(slide, item_schema, cls=jsonschema.Draft7Validator)


def test_the_slides_schema_itself_is_well_formed_json_schema():
    """Sanity check the fixture above rests on: SLIDES_SCHEMA must compile as
    a schema before it can be used to validate anything."""
    jsonschema.Draft7Validator.check_schema(SLIDES_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(SLIDES_SCHEMA)


def test_the_flat_schema_is_well_formed_and_meaningfully_smaller():
    """SLIDES_SCHEMA_FLAT is what the degraded json_object path embeds in the
    prompt in place of the full union (llm.py's _schema_prompt / compose.py's
    degraded_schema argument). It must still be valid JSON Schema, and it
    must actually be smaller — that is the entire reason it exists."""
    from pipeline.stages.compose import SLIDES_SCHEMA_FLAT

    jsonschema.Draft7Validator.check_schema(SLIDES_SCHEMA_FLAT)
    jsonschema.Draft202012Validator.check_schema(SLIDES_SCHEMA_FLAT)

    import json as _json
    full_len = len(_json.dumps(SLIDES_SCHEMA))
    flat_len = len(_json.dumps(SLIDES_SCHEMA_FLAT))
    assert flat_len < full_len * 0.5, (
        f"flat schema ({flat_len} chars) should be well under half the "
        f"union's size ({full_len} chars) to be worth the split"
    )


def test_the_flat_schema_still_accepts_every_type_real_code_produces():
    """The flat schema drops per-type discrimination, but it must not become
    so loose it rejects shapes our own code legitimately emits — it still
    gates on the union of every type's legal fields plus additionalProperties:
    false, so a slide from an unrelated type's vocabulary is still caught."""
    from pipeline.stages.compose import SLIDES_SCHEMA_FLAT

    item_schema = SLIDES_SCHEMA_FLAT["properties"]["slides"]["items"]
    for kind in SLIDE_TYPES:
        body_field = REQUIRED_BODY_FIELDS[kind][0]
        slide = {"type": kind, "headline": "H", body_field: _SAMPLE_BODY_VALUE[body_field]}
        jsonschema.validate(slide, item_schema, cls=jsonschema.Draft7Validator)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(
            {"type": "hook", "headline": "H", "not_a_real_field": 1},
            item_schema, cls=jsonschema.Draft7Validator,
        )


@pytest.mark.parametrize("kind", ["facts", "links"])
def test_b1_sub_is_declared_where_the_renderer_and_our_own_code_use_it(kind):
    """B1: facts.html.j2 and links.html.j2 both render `s.sub`, and our own
    ensure_links_slide emits "sub" on every generated links slide — but
    SLIDE_TYPE_FIELDS omitted "sub" from both types, so the schema forbade a
    field the renderer reads and our own code produces. Before the fix this
    raises jsonschema.exceptions.ValidationError (additionalProperties);
    after, it validates clean."""
    body_field = REQUIRED_BODY_FIELDS[kind][0]
    slide = {"type": kind, "headline": "H", "sub": "a sub line",
             body_field: _SAMPLE_BODY_VALUE[body_field]}
    _validate_real(slide)  # must not raise


def test_ensure_links_slide_output_is_real_schema_valid():
    """The exact slide ensure_links_slide hardcodes in production — this is
    the concrete instance of the B1 links.sub bug: before the fix, this
    slide (as emitted by our own code on every "list"-intent deck) failed
    real jsonschema validation."""
    slides = [{"type": "hook", "headline": "h", "sub": "s"}]
    out = ensure_links_slide(slides, _repo_notes())
    links_slide = next(s for s in out if s["type"] == "links")
    assert links_slide["sub"] == "Screenshot this slide."
    _validate_real(links_slide)


def test_ensure_closing_slide_output_is_real_schema_valid():
    out = ensure_closing_slide(_body(), is_dm=False, source_urls=["https://a.example"])
    validated = [s for s in out if s["type"] in ("sources", "follow")]
    assert len(validated) == 2, "both generated closing slides must be checked"
    for slide in validated:
        _validate_real(slide)


def test_drop_duplicate_ctas_survivors_are_real_schema_valid():
    slides = [
        {"type": "hook", "headline": "Narcissism", "sub": "A spectrum."},
        {"type": "point", "headline": "Follow for more psychology insights",
         "bullets": ["Evidence-based breakdowns weekly"]},
        {"type": "follow", "headline": "More like this", "sub": "Weekly."},
    ]
    survivors = drop_duplicate_ctas(slides)
    assert len(survivors) == 2, "the duplicate CTA must actually be dropped"
    for slide in survivors:
        _validate_real(slide)


def test_restore_repo_facts_output_is_real_schema_valid():
    """Audit finding beyond the reviewer's two: enumerate_items.py routinely
    sets note["logo"] (a downloaded org/repo avatar), and _restore_repo_facts
    copies it verbatim onto the repo slide — repo.html.j2 renders it as
    `s.logo_uri`. "logo" was not declared in SLIDE_TYPE_FIELDS["repo"]: the
    same bug class as B1, one call site over. Before the fix this raises
    ValidationError on the "logo" key; after, it validates clean."""
    from pipeline.stages.compose import _restore_repo_facts

    slides = [{"type": "repo", "headline": "r0", "name": "r0"}]
    notes = [{"name": "r0", "owner": "o0", "url": "https://github.com/o0/r0",
              "stars": 10, "language": "Python", "logo": "/tmp/o0.png",
              "detail": "A thing worth reading."}]
    out = _restore_repo_facts(slides, notes)
    assert out[0]["logo"] == "/tmp/o0.png"
    _validate_real(out[0])


def test_an_unregistered_slide_type_fails_loudly_not_permissively():
    """B2: _slide_variant used `.get(kind, ())` for both SLIDE_TYPE_FIELDS and
    REQUIRED_BODY_FIELDS, so a type added to SLIDE_TYPES without registering
    its body contract silently built a PERMISSIVE bodyless arm (only "type"
    and "headline" required) — exactly the bug class this task exists to
    close, reintroduced in the union builder's own defaults.

    SLIDES_SCHEMA is built by calling _slide_variant(kind) for every kind in
    SLIDE_TYPES at MODULE IMPORT TIME (see the list comprehension building
    SLIDES_SCHEMA["properties"]["slides"]["items"]["oneOf"]), so this KeyError
    is not just a test assertion — it is what production actually does the
    moment such a type is added, before compose.py finishes importing."""
    from pipeline.stages.compose import _slide_variant

    with pytest.raises(KeyError):
        _slide_variant("newtype")


def test_both_registries_must_independently_fail_loudly():
    """A subtler cousin of the mutation above: a type missing from BOTH dicts
    is caught by the very first lookup (`SLIDE_TYPE_FIELDS[kind]`) regardless
    of whether the SECOND lookup (`REQUIRED_BODY_FIELDS[kind]`) still indexes
    or has quietly reverted to `.get(kind, ())` — the first KeyError fires
    before the second lookup ever runs, so
    test_an_unregistered_slide_type_fails_loudly_not_permissively alone
    cannot prove the second lookup is still load-bearing. This test isolates
    it: register a type in SLIDE_TYPE_FIELDS ONLY (so the first lookup
    succeeds) and prove the second lookup still raises KeyError on its own,
    rather than silently degrading to a bodyless-permitted arm via a
    forgotten `.get` default."""
    from pipeline.stages.compose import (
        REQUIRED_BODY_FIELDS,
        SLIDE_TYPE_FIELDS,
        _slide_variant,
    )

    assert "newtype" not in REQUIRED_BODY_FIELDS
    SLIDE_TYPE_FIELDS["newtype"] = ("sub",)
    try:
        with pytest.raises(KeyError):
            _slide_variant("newtype")
    finally:
        del SLIDE_TYPE_FIELDS["newtype"]


# --------------------------------- template field audit (generalising B1)


def test_every_field_a_template_reads_is_declared_for_its_type():
    """Generalises B1 rather than re-testing its two known instances: parses
    the ACTUAL .html.j2 source (not a hand-copied list, which is exactly how
    "sub" on facts/links went undetected in the first place) and asserts
    every `s.<field>` a slide type's template reads is a schema-legal field
    for that type. A template that starts reading a field SLIDE_TYPE_FIELDS
    forbids cannot ship silently again — this test goes red the moment it
    does, for any of the 15 types, not just the two found this round."""
    import re
    from pathlib import Path

    from pipeline.stages.compose import _COMMON_FIELDS, SLIDE_TYPE_FIELDS

    # image_uri/logo_uri are computed at render time (with_image_uris) from
    # the raw "image"/"logo" keys — the template never sees the raw key, so
    # the audit has to know the derived name maps back to the real field.
    derived_from = {"image_uri": "image", "logo_uri": "logo"}
    field_access = re.compile(r"(?<![\w.])s\.([A-Za-z_][A-Za-z0-9_]*)")

    root = Path(__file__).resolve().parents[2] / "templates" / "slides"
    for kind in SLIDE_TYPES:
        source = (root / f"{kind}.html.j2").read_text()
        used = {derived_from.get(f, f) for f in field_access.findall(source)}
        legal = set(SLIDE_TYPE_FIELDS[kind]) | set(_COMMON_FIELDS) | {"headline", "type"}
        undeclared = used - legal
        assert not undeclared, (
            f"{kind}.html.j2 reads {undeclared}, not declared in "
            f"SLIDE_TYPE_FIELDS[{kind!r}] (or _COMMON_FIELDS)"
        )


def test_the_shared_wrapper_template_only_reads_common_fields():
    """The per-type audit above only walks templates/slides/*.html.j2 — it
    structurally cannot see templates/base.html.j2, the shared wrapper every
    slide renders inside (it lives one directory up and is not named after a
    slide type). That template is generic over every type by construction
    (it `{% include %}`s the per-type one), so the only `s.<field>` accesses
    it should ever have are the cross-cutting ones. Closing that gap here
    rather than leaving it as a documented blind spot: today it only reads
    `type`/`image_mode`/`image_uri` (all already common/legal), which is why
    this was latent rather than live — but the per-type audit's whole point
    is that "currently fine" does not mean "cannot regress silently"."""
    import re
    from pathlib import Path

    from pipeline.stages.compose import _COMMON_FIELDS

    derived_from = {"image_uri": "image", "logo_uri": "logo"}
    field_access = re.compile(r"(?<![\w.])s\.([A-Za-z_][A-Za-z0-9_]*)")

    path = Path(__file__).resolve().parents[2] / "templates" / "base.html.j2"
    source = path.read_text()
    used = {derived_from.get(f, f) for f in field_access.findall(source)}
    legal = set(_COMMON_FIELDS) | {"headline", "type"}
    undeclared = used - legal
    assert not undeclared, (
        f"base.html.j2 reads {undeclared} unconditionally, but it renders "
        "every slide type generically and can only rely on fields common to "
        "all of them"
    )


def test_b3_normalise_slides_comment_names_the_real_runtime_risk():
    """B3: the comment above the body-content check in normalise_slides
    previously claimed the top-level `oneOf` "forbids a bodyless slide of a
    given type from validating at all" — flatly false for Ollama, which does
    not bind the top-level `oneOf` (confirmed live: it accepted a bodyless
    `compare` and a `compare` carrying `series` with no `rows`, the exact bug
    this task closes). Re-running the live check against OpenRouter with the
    actual configured OPENROUTER_MODEL_GOOD (during this same fix) found the
    same class of problem there too — despite `strict: true`, the model
    ignored the schema's field names entirely. The corrected comment must say
    the check is a live backstop on the PRIMARY path, not just the local
    fallback, and not decoration — checked here the same way this codebase
    already checks prompt copy (grep the actual source), so the comment
    cannot silently regress back to a false sense of safety on either
    provider.

    Only the negative assertion is pinned here, not a set of positive phrases
    lifted from the comment's current wording: repo precedent (the SYSTEM
    prompt-copy checks elsewhere in this file) asserts on strings that
    actually reach the model, where exact wording is load-bearing. A code
    comment's prose is not — pinning five of its phrases would just make the
    comment impossible to reword without breaking a test for no functional
    reason. What must never come back is the specific false claim."""
    import inspect

    source = inspect.getsource(normalise_slides)
    collapsed = " ".join(source.split())

    assert (
        "forbids a bodyless slide of a given type from validating at all"
        not in collapsed
    ), "the old, incorrect claim must not still be present"


# --------------------------------------------------------------- normalising


def test_normalise_puts_hook_first_and_sources_last():
    out = normalise_slides([
        {"type": "sources", "headline": "S", "urls": ["https://a"]},
        {"type": "point", "headline": "P", "bullets": ["b"]},
        {"type": "hook", "headline": "H", "sub": "s"},
    ])
    assert [s["type"] for s in out] == ["hook", "point", "sources"]


def test_normalise_demotes_surplus_hooks_rather_than_dropping_them():
    out = normalise_slides([
        {"type": "hook", "headline": "First", "sub": "one"},
        {"type": "hook", "headline": "Second", "sub": "two"},
        {"type": "point", "headline": "P", "bullets": ["b"]},
    ])
    assert [s["type"] for s in out] == ["hook", "point", "point"]
    assert out[1]["headline"] == "Second", "researched content must not be lost"


def test_normalise_keeps_one_sources_slide():
    out = normalise_slides([
        {"type": "hook", "headline": "H", "sub": "s"},
        {"type": "sources", "headline": "S1", "urls": ["https://a"]},
        {"type": "sources", "headline": "S2", "urls": ["https://b"]},
    ])
    assert [s["type"] for s in out].count("sources") == 1


def test_normalise_drops_unknown_types_and_empty_headlines():
    out = normalise_slides([
        {"type": "hook", "headline": "H", "sub": "s"},
        {"type": "nonsense", "headline": "unknown type"},
        {"type": "point", "headline": "   ", "bullets": ["b"]},
    ])
    assert [s["type"] for s in out] == ["hook"]


def test_normalise_caps_bullets_rows_and_urls_at_four():
    out = normalise_slides([{
        "type": "point", "headline": "H",
        "bullets": ["a", "b", "c", "d", "e"],
    }, {
        "type": "facts", "headline": "F",
        "rows": [["a", "1"], ["b", "2"], ["c", "3"], ["d", "4"], ["e", "5"]],
    }])
    assert len(out[0]["bullets"]) == 4
    assert len(out[1]["rows"]) == 4


def test_normalise_enforces_the_carousel_maximum():
    many = [{"type": "hook", "headline": "H", "sub": "s"}] + [
        {"type": "point", "headline": f"P{i}", "bullets": ["b"]} for i in range(15)
    ]
    assert len(normalise_slides(many)) == MAX_SLIDES


def test_normalise_drops_malformed_rows():
    out = normalise_slides([
        {"type": "facts", "headline": "F", "rows": [["only-one"], ["a", "b"]]},
    ])
    assert out[0]["rows"] == [["a", "b"]]


# ---------------------------------------------------------------- captioning


def test_build_caption_appends_credit_and_hashtags():
    out = build_caption("The story.", ["AI", "#Tech"], credit="@aipost")
    assert "Source: @aipost" in out
    assert "#ai #tech" in out


def test_build_caption_does_not_duplicate_an_existing_credit():
    out = build_caption("The story. via @aipost", ["ai"], credit="@aipost")
    assert out.count("@aipost") == 1


def test_build_caption_caps_hashtags_at_five():
    """Instagram refuses a caption with more than five hashtags, and the
    failure would surface at publish time as an opaque API error. Enforced in
    code rather than trusted to the prompt."""
    out = build_caption("x", [f"t{i}" for i in range(20)])
    assert out.count("#") == 5


def test_build_caption_dedupes_hashtags():
    """Duplicates would waste slots against a five-tag budget."""
    out = build_caption("x", ["ai", "AI", "#ai", "openai", "openai", "cursor"])
    assert out.count("#") == 3
    assert "#ai" in out and "#openai" in out and "#cursor" in out


def test_prompt_asks_for_at_most_five_hashtags():
    from pipeline.stages.compose import SYSTEM

    collapsed = " ".join(SYSTEM.split())
    assert "3-5 lowercase tags" in collapsed
    assert "Five is the hard maximum" in collapsed


# ------------------------------------------------------------------- compose


async def test_compose_returns_slides_and_caption(fake_llm, settings):
    fake_llm.queue(_doc())
    out = await compose(_item(), fake_llm, replace(settings, source_credit="@aipost"))

    # "sources" is appended by the closing-slide guarantee: _doc() omits one,
    # and a deck that ends on a body slide would publish with no attribution.
    assert [s["type"] for s in out["slides"]] == [
        "hook", "point", "takeaway", "sources", "follow"]
    assert "Source: @aipost" in out["caption"]


async def test_compose_wires_the_flat_schema_as_the_degraded_prompt_fallback(fake_llm, settings):
    """compose() must offer llm.good() a smaller `degraded_schema` for
    OpenRouter's json_object fallback, not just the full discriminated
    union — SLIDES_SCHEMA_FLAT is built from the same registries but is a
    fraction of the size once serialized (2160 vs 6635 chars measured on this
    schema), and _schema_prompt in llm.py embeds whichever one it is given
    verbatim into the system prompt on every degraded call."""
    from pipeline.stages.compose import SLIDES_SCHEMA_FLAT

    fake_llm.queue(_doc())
    await compose(_item(), fake_llm, settings)

    assert fake_llm.calls[-1].schema == SLIDES_SCHEMA
    assert fake_llm.calls[-1].degraded_schema == SLIDES_SCHEMA_FLAT
    assert len(str(SLIDES_SCHEMA_FLAT)) < len(str(SLIDES_SCHEMA)), (
        "the whole point is that the degraded copy is smaller"
    )


async def test_compose_without_a_brief_is_retryable(fake_llm, settings):
    with pytest.raises(Retryable, match="without a brief"):
        await compose(_item(brief=""), fake_llm, settings)


async def test_regen_note_reaches_the_prompt_and_is_cleared(fake_llm, settings):
    """Stale revision instructions must not silently reapply later."""
    fake_llm.queue(_doc())
    out = await compose(_item(regen_note="make it punchier"), fake_llm, settings)

    assert "make it punchier" in fake_llm.calls[-1].user
    assert out["regen_note"] is None


async def test_compose_passes_source_urls_for_the_sources_slide(fake_llm, settings):
    fake_llm.queue(_doc())
    await compose(_item(), fake_llm, settings)
    assert "https://a.example/1" in fake_llm.calls[-1].user


async def test_compose_rejects_a_deck_that_normalises_too_small(fake_llm, settings):
    fake_llm.queue(_doc(slides=[
        {"type": "hook", "headline": "H", "sub": "s"},
        {"type": "nonsense", "headline": "X"},
    ]))
    with pytest.raises(Retryable, match="only 1 usable slides"):
        await compose(_item(), fake_llm, settings)


# --------------------------------------------- content-driven theme choice


async def test_composer_picks_a_theme_and_it_is_persisted(fake_llm, settings):
    fake_llm.queue({**_doc(), "theme": "blockprint"})
    out = await compose(_item(), fake_llm, settings)
    assert out["theme"] == "blockprint"


async def test_theme_prompt_describes_when_each_look_applies(fake_llm, settings):
    """The model must match look to substance, not alternate blindly."""
    fake_llm.queue({**_doc(), "theme": "signal"})
    await compose(_item(), fake_llm, settings)

    system = fake_llm.calls[-1].system
    for theme in ("blockprint", "newsprint", "aurora", "signal"):
        assert theme in system, f"{theme} not described in the prompt"
    assert "suits THIS story" in system


async def test_invalid_theme_falls_back_rather_than_breaking_render(fake_llm, settings):
    """An unknown theme name would resolve to no file and lose the design."""
    fake_llm.queue({**_doc(), "theme": "vaporwave"})
    out = await compose(_item(), fake_llm, settings)
    assert out["theme"] == "signal"


async def test_missing_theme_falls_back(fake_llm, settings):
    doc = _doc()
    doc.pop("theme", None)
    fake_llm.queue(doc)
    out = await compose(_item(), fake_llm, settings)
    assert out["theme"] == "signal"


def test_every_composer_theme_has_a_file_on_disk():
    """A theme the model can choose but that does not exist renders as default."""
    from pipeline.stages.compose import THEMES
    from pipeline.stages.render import available_themes

    assert set(THEMES) <= set(available_themes()), "composer can pick a missing theme"


# ------------------------------------------------ richer slide types


def test_new_slide_types_are_available():
    from pipeline.stages.compose import SLIDE_TYPES
    for t in ("code", "flow", "compare", "quote"):
        assert t in SLIDE_TYPES


def test_prompt_pushes_toward_non_bullet_slides():
    """A deck of nothing but headline-and-bullets was the complaint."""
    from pipeline.stages.compose import SYSTEM
    collapsed = " ".join(SYSTEM.split())
    assert "headline-and-bullets is the failure mode" in collapsed
    assert "show the real thing" in collapsed
    assert "Never pseudocode" in collapsed
    assert "at least one non-bullet slide" in collapsed


def test_code_slide_keeps_newlines_and_indentation():
    out = normalise_slides([{
        "type": "code", "headline": "Layout", "lang": "JSON",
        "code": '{\n  "a": 1,\n    "b": 2\n}\n\n',
    }])
    assert out[0]["code"] == '{\n  "a": 1,\n    "b": 2\n}'
    assert out[0]["lang"] == "json"


def test_flow_steps_are_capped_and_require_a_label():
    out = normalise_slides([{
        "type": "flow", "headline": "How",
        "steps": [{"label": f"s{i}", "detail": "d"} for i in range(9)] + [{"detail": "no label"}],
    }])
    assert len(out[0]["steps"]) == 5


def test_compare_keeps_column_titles():
    out = normalise_slides([{
        "type": "compare", "headline": "A vs B", "left_title": "Old",
        "right_title": "New", "rows": [["x", "y"]],
    }])
    assert out[0]["left_title"] == "Old" and out[0]["right_title"] == "New"


def test_slide_without_its_payload_is_dropped():
    """A typed slide with no content renders as a bare headline on an empty
    slide, which looks broken."""
    out = normalise_slides([
        {"type": "hook", "headline": "Fine", "sub": "s"},
        {"type": "code", "headline": "No code here"},
        {"type": "flow", "headline": "No steps"},
        {"type": "quote", "headline": "No quote"},
        {"type": "compare", "headline": "No rows"},
    ])
    assert [s["type"] for s in out] == ["hook"]


def test_every_slide_type_has_a_template():
    from pathlib import Path

    from pipeline.stages.compose import SLIDE_TYPES

    root = Path(__file__).resolve().parents[2] / "templates" / "slides"
    for t in SLIDE_TYPES:
        assert (root / f"{t}.html.j2").exists(), f"no template for {t}"


# ------------------------------------------------- reusing attached images


def _images(*ratings):
    return [{"path": f"/tmp/img{i}.png", "usable": r, "description": f"image {i}"}
            for i, r in enumerate(ratings)]


def test_usable_images_skips_the_ones_vision_rejected():
    from pipeline.stages.compose import usable_images

    item = _item(extracted={"image_descriptions": [
        {"path": "/tmp/a.png", "usable": "hero", "description": "d"},
        {"path": "/tmp/b.png", "usable": "none", "description": "watermark"},
        {"path": "/tmp/c.png", "usable": "inset", "description": "d"},
        {"path": "/tmp/d.png", "error": "vision failed"},
    ]})
    assert [i["path"] for i in usable_images(item)] == ["/tmp/a.png", "/tmp/c.png"]


def test_image_index_resolves_to_a_path():
    out = normalise_slides(
        [{"type": "photo", "headline": "The car", "image": 0}], _images("hero")
    )
    assert out[0]["image"] == "/tmp/img0.png"
    assert out[0]["image_mode"] == "hero"


def test_hallucinated_image_index_is_dropped():
    """A model naming an image that does not exist would render a broken img."""
    out = normalise_slides(
        [{"type": "point", "headline": "P", "bullets": ["b"], "image": 7, "image_mode": "inset"}],
        _images("hero"),
    )
    assert "image" not in out[0]


def test_image_cannot_be_promoted_above_its_rating():
    """Vision said background-only; the composer must not make it a hero."""
    out = normalise_slides(
        [{"type": "point", "headline": "P", "bullets": ["b"], "image": 0, "image_mode": "hero"}],
        _images("background"),
    )
    assert "image" not in out[0]


def test_image_may_be_used_more_modestly_than_rated():
    out = normalise_slides(
        [{"type": "point", "headline": "P", "bullets": ["b"], "image": 0, "image_mode": "background"}],
        _images("hero"),
    )
    assert out[0]["image_mode"] == "background"


def test_photo_slide_without_an_image_is_dropped():
    out = normalise_slides(
        [{"type": "hook", "headline": "H", "sub": "s"},
         {"type": "photo", "headline": "No image"}],
        _images("hero"),
    )
    assert [s["type"] for s in out] == ["hook"]


async def test_available_images_are_offered_to_the_model(fake_llm, settings):
    fake_llm.queue(_doc())
    item = _item(extracted={"image_descriptions": [
        {"path": "/tmp/a.png", "usable": "hero", "description": "a photo of a car"},
    ]})
    await compose(item, fake_llm, settings)

    user = fake_llm.calls[-1].user
    assert "AVAILABLE IMAGES" in user
    assert "rating=hero" in user
    assert "a photo of a car" in user


async def test_no_image_section_when_nothing_is_usable(fake_llm, settings):
    fake_llm.queue(_doc())
    await compose(_item(), fake_llm, settings)
    assert "AVAILABLE IMAGES" not in fake_llm.calls[-1].user


def test_research_and_synthesis_preserve_verbatim_snippets():
    """Regression: an OKF explainer produced no code slide because the brief
    described the format in prose. Research summarised snippets away and
    synthesis flattened what was left, so compose had nothing real to show and
    correctly refused to invent syntax."""
    from pipeline.stages.research import RESEARCH_SYSTEM
    from pipeline.stages.synthesize import SYSTEM as SYNTH_SYSTEM
    from pipeline.stages.compose import SYSTEM as COMPOSE_SYSTEM

    assert "VERBATIM" in RESEARCH_SYSTEM
    assert "Do not paraphrase such material" in RESEARCH_SYSTEM

    assert "VERBATIM" in SYNTH_SYSTEM
    assert "not a substitute for the format itself" in " ".join(SYNTH_SYSTEM.split())

    collapsed = " ".join(COMPOSE_SYSTEM.split())
    assert "that is verbatim source material" in collapsed
    assert "do NOT invent one" in collapsed


# ------------------------------------------- no slide may be a bare headline


def test_every_type_requires_body_content():
    """Regression: 20% of live slides were a headline on an empty 1080x1350
    field. required_payload covered only the newer types, so point, hook,
    takeaway and facts sailed through with nothing in them."""
    bare = [
        {"type": "hook", "headline": "Just a headline"},
        {"type": "point", "headline": "Just a headline"},
        {"type": "facts", "headline": "Just a headline"},
        {"type": "takeaway", "headline": "Just a headline"},
        {"type": "sources", "headline": "Just a headline"},
        {"type": "code", "headline": "Just a headline"},
        {"type": "flow", "headline": "Just a headline"},
        {"type": "compare", "headline": "Just a headline"},
        {"type": "quote", "headline": "Just a headline"},
    ]
    assert normalise_slides(bare) == []


def test_point_is_satisfied_by_bullets_or_a_stat():
    """A stat block is real content even without bullets."""
    out = normalise_slides([
        {"type": "point", "headline": "A", "bullets": ["x"]},
        {"type": "point", "headline": "B", "stat": {"value": "5%", "label": "share"}},
        {"type": "point", "headline": "C", "sub": "a supporting sentence"},
    ])
    assert [s["headline"] for s in out] == ["A", "B", "C"]


def test_prompt_forbids_putting_the_substance_in_the_headline():
    from pipeline.stages.compose import SYSTEM

    collapsed = " ".join(SYSTEM.split())
    assert "EVERY SLIDE MUST HAVE BODY CONTENT" in collapsed
    assert "is a sentence, not a headline" in collapsed
    assert "write fewer, fuller slides" in collapsed


# ------------------------------------- closing slide depends on the source


async def test_dm_items_are_told_to_close_on_follow(fake_llm, settings):
    """A DM was requested directly, so there is no channel to credit and no
    reason to spend the last slide listing sources the requester already has."""
    fake_llm.queue(_doc())
    await compose(_item(source="dm"), fake_llm, settings)

    user = fake_llm.calls[-1].user
    assert 'End with a "follow" slide' in user
    assert "Credit this source channel" not in user


async def test_channel_items_are_told_to_close_on_sources(fake_llm, settings):
    from dataclasses import replace as _replace

    fake_llm.queue(_doc())
    await compose(_item(source="channel"), fake_llm,
                  _replace(settings, source_credit="@aipost"))

    user = fake_llm.calls[-1].user
    assert 'end with a "sources" slide' in user.lower()
    assert "Credit this source channel" in user


def test_follow_slide_goes_after_sources_at_the_very_end():
    out = normalise_slides([
        {"type": "follow", "headline": "More like this", "sub": "daily"},
        {"type": "hook", "headline": "H", "sub": "s"},
        {"type": "sources", "headline": "S", "urls": ["https://a"]},
        {"type": "point", "headline": "P", "bullets": ["b"]},
    ])
    assert [s["type"] for s in out] == ["hook", "point", "sources", "follow"]


def test_follow_slide_needs_body_content():
    assert normalise_slides([{"type": "follow", "headline": "Follow"}]) == []


def test_follow_template_exists():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "templates" / "slides"
    assert (root / "follow.html.j2").exists()


# --- closing slides are guaranteed, not requested --------------------------
#
# Two separate guarantees. Attribution says where the facts came from; the
# follow slide asks for the follow. Requiring only that *some* closing slide
# existed meant a channel deck carrying "sources" passed the check and shipped
# with no call to action — every channel post lacked one.

def _body(n=3):
    return [{"type": "hook", "headline": "h", "sub": "s"}] + [
        {"type": "point", "headline": f"p{i}", "sub": "s"} for i in range(n - 1)
    ]


def _types(slides):
    return [s["type"] for s in slides]


def test_channel_deck_gets_both_sources_and_follow():
    out = ensure_closing_slide(_body(), is_dm=False, source_urls=["https://a.example"])
    assert _types(out)[-2:] == ["sources", "follow"]
    assert out[-2]["urls"] == ["https://a.example"]


def test_channel_deck_that_already_cites_still_gets_a_follow():
    """The bug: sources satisfied the old check, so no call to action shipped."""
    slides = _body() + [{"type": "sources", "headline": "Sources", "urls": ["u"]}]
    out = ensure_closing_slide(slides, is_dm=False, source_urls=["https://a.example"])
    assert _types(out)[-2:] == ["sources", "follow"]
    assert _types(out).count("sources") == 1, "must not add a second sources slide"


def test_a_links_slide_also_counts_as_attribution():
    slides = _body() + [{"type": "links", "headline": "All the links", "links": ["u"]}]
    out = ensure_closing_slide(slides, is_dm=False, source_urls=["https://a.example"])
    assert _types(out)[-2:] == ["links", "follow"]
    assert "sources" not in _types(out)


def test_dm_deck_closes_on_follow_with_no_sources():
    """A direct request has no channel to credit."""
    out = ensure_closing_slide(_body(), is_dm=True, source_urls=["https://a.example"])
    assert _types(out)[-1] == "follow"
    assert "sources" not in _types(out)


def test_no_research_urls_means_no_sources_slide():
    """An empty sources slide would be dropped as bodyless anyway."""
    out = ensure_closing_slide(_body(), is_dm=False, source_urls=[])
    assert _types(out)[-1] == "follow"
    assert "sources" not in _types(out)


def test_a_deck_already_complete_is_left_alone():
    slides = _body() + [
        {"type": "sources", "headline": "Sources", "urls": ["u"]},
        {"type": "follow", "headline": "Follow for more", "sub": "s"},
    ]
    assert ensure_closing_slide(slides, False, ["https://a.example"]) == slides


def test_full_deck_makes_room_for_every_addition():
    slides = _body(MAX_SLIDES)
    out = ensure_closing_slide(slides, False, ["https://a.example"])
    assert len(out) == MAX_SLIDES
    assert _types(out)[-2:] == ["sources", "follow"]
    assert out[:-2] == slides[: MAX_SLIDES - 2]


def test_sources_slide_caps_url_count():
    urls = [f"https://e{i}.example" for i in range(9)]
    out = ensure_closing_slide(_body(), False, urls)
    assert out[-2]["urls"] == urls[:MAX_SOURCE_URLS]


# --- hashtag cap counts the whole caption ----------------------------------
#
# The cap governed only the appended tags. The composer also writes tags into
# the prose, and Instagram counts those too: one eval caption carried five
# appended plus two inline, seven against a limit of five.

def test_inline_hashtags_count_against_the_limit():
    out = build_caption(
        "Plain Markdown, no proprietary tools. #AI #OpenKnowledge",
        ["openknowledge", "aigent", "markdown", "yaml", "aistandards"],
    )
    assert out.count("#") == MAX_HASHTAGS


def test_inline_hashtags_are_kept_and_come_first():
    """The model put those inline deliberately; they are its strongest tags."""
    out = build_caption("Body text #mcp #ai", ["devtools", "apps"])
    tags = out.rsplit("\n\n", 1)[-1].split()
    assert tags[:2] == ["#mcp", "#ai"]


def test_inline_hashtags_are_removed_from_the_prose():
    out = build_caption("Markdown files, no tools needed. #AI", ["x"])
    body = out.rsplit("\n\n", 1)[0]
    assert "#AI" not in body
    assert body.endswith("needed.")


def test_a_duplicate_inline_tag_is_not_counted_twice():
    out = build_caption("Body #mcp", ["mcp", "ai", "devtools"])
    tags = out.rsplit("\n\n", 1)[-1].split()
    assert tags.count("#mcp") == 1
    assert len(tags) == 3


def test_sharp_notation_is_not_treated_as_a_hashtag():
    """C# is a language; #1 is a rank. Neither is a tag."""
    out = build_caption("Built in C# and ranked #1 overall.", ["dotnet"])
    body = out.rsplit("\n\n", 1)[0]
    assert "C#" in body and "#1" in body


def test_a_caption_of_only_hashtags_still_yields_a_capped_set():
    out = build_caption("#a #b #c #d #e #f #g", [])
    assert out.count("#") == MAX_HASHTAGS


# --- few-shot scope reaches the prompt --------------------------------------
#
# few_shot_enabled was unit-tested while compose's use of it was not, so
# replacing the scope check with a bare "mode != off" passed the whole suite.
# These assert on the prompt compose actually sends.

async def _prompt_for(fake_llm, settings, mode, intent):
    from dataclasses import replace as _replace
    fake_llm.queue(_doc())
    await compose(_item(intent=intent), fake_llm,
                  _replace(settings, few_shot_examples=mode))
    return fake_llm.calls[-1].user


async def test_list_mode_shows_examples_to_an_enumeration(fake_llm, settings):
    assert "EXAMPLE OF A GOOD" in await _prompt_for(fake_llm, settings, "list", "list")


async def test_list_mode_withholds_examples_from_news(fake_llm, settings):
    assert "EXAMPLE OF A GOOD" not in await _prompt_for(fake_llm, settings, "list", "news")


async def test_all_mode_shows_examples_to_news_too(fake_llm, settings):
    assert "EXAMPLE OF A GOOD" in await _prompt_for(fake_llm, settings, "all", "news")


async def test_off_mode_shows_examples_to_nobody(fake_llm, settings):
    for intent in ("list", "news"):
        assert "EXAMPLE OF A GOOD" not in await _prompt_for(
            fake_llm, settings, "off", intent)


# --- the deck is critiqued against the request ------------------------------
#
# A deck can be well-formed and answer half the request. Item 45 asked for the
# early signs of burnout AND for AI's effect on them, and shipped eight tidy
# slides about the second half. Every structural check passed it.

async def test_a_deck_that_misses_a_clause_is_recomposed_once(fake_llm, settings):
    fake_llm.queue(_doc())                      # first attempt
    fake_llm.queue({"uncovered": ["the early signs of burnout"]})   # critique
    fake_llm.queue(_doc(slides=[
        {"type": "hook", "headline": "Signs", "sub": "s"},
        {"type": "point", "headline": "Early signs", "bullets": ["exhaustion"]},
        {"type": "takeaway", "headline": "So what", "sub": "x"},
    ]))                                         # second attempt
    out = await compose(
        _item(clauses=["the early signs of burnout"]), fake_llm, settings)

    assert any("Early signs" in s.get("headline", "") for s in out["slides"])
    retry_prompt = fake_llm.calls[-1].user
    assert "REVISION REQUESTED" in retry_prompt
    assert "the early signs of burnout" in retry_prompt


async def test_a_covered_deck_is_not_recomposed(fake_llm, settings):
    fake_llm.queue(_doc())
    fake_llm.queue({"uncovered": []})
    out = await compose(_item(clauses=["anything"]), fake_llm, settings)

    assert len(fake_llm.calls) == 2, "one compose, one critique, no retry"
    assert out["slides"]


async def test_an_item_with_no_clauses_skips_the_critique(fake_llm, settings):
    """Channel items mostly ask one thing; there is nothing to check and no
    reason to spend a call on every post."""
    fake_llm.queue(_doc())
    await compose(_item(clauses=[]), fake_llm, settings)
    assert len(fake_llm.calls) == 1


async def test_the_retry_is_not_repeated(fake_llm, settings):
    """One retry, never a loop — research already cleared these clauses, so a
    third pass on the same brief will not find what the second could not."""
    fake_llm.queue(_doc())
    fake_llm.queue({"uncovered": ["still missing"]})
    fake_llm.queue(_doc())
    out = await compose(_item(clauses=["still missing"]), fake_llm, settings)

    assert len(fake_llm.calls) == 3, "compose, critique, one recompose — then stop"
    assert out["slides"]


def test_deck_text_carries_body_not_just_headlines():
    """Judging coverage from headlines alone marks anything vaguely on-topic
    as addressed."""
    from pipeline.stages.compose import _deck_text

    text = _deck_text([
        {"type": "point", "headline": "Signs", "bullets": ["exhaustion", "cynicism"]},
        {"type": "facts", "headline": "Split", "rows": [["Burnout", "work-specific"]]},
        {"type": "hook", "headline": "Lead", "sub": "the standfirst"},
    ])
    assert "exhaustion" in text[0] and "cynicism" in text[0]
    assert "work-specific" in text[1]
    assert "the standfirst" in text[2]


# --- an enumeration gets its links index ------------------------------------
#
# Measured, not assumed: across both eval arms that showed the composer a
# worked example, only 1 enumeration in 4 produced a links slide. The prompt
# asks for it and the example demonstrates it; three times in four the reader
# still could not go and find the items.

def _repo_notes(n=3):
    return [{"claim": f"o{i}/r{i}", "url": f"https://github.com/o{i}/r{i}",
             "name": f"r{i}", "owner": f"o{i}"} for i in range(n)]


def _list_notes(n=3):
    """Full-shaped repo-catalogue notes, the ones enumerate_items.py's
    ``_note`` (repos=True) actually produces — every D50/T105 field
    populated, for tests that assert the composer's prompt carries them."""
    return [{
        "claim": f"owner{i}/repo{i}", "name": f"repo{i}", "owner": f"owner{i}",
        "url": f"https://github.com/owner{i}/repo{i}", "stars": 100 + i,
        "language": "Python", "sources": [f"https://github.com/owner{i}/repo{i}"],
        "detail": f"Repo {i} does something a developer would reach for.",
    } for i in range(n)]


def test_a_links_slide_is_added_from_the_researched_urls():
    slides = [{"type": "hook", "headline": "h", "sub": "s"},
              {"type": "repo", "headline": "r0", "name": "r0"},
              {"type": "follow", "headline": "Follow for more", "sub": "s"}]
    out = ensure_links_slide(slides, _repo_notes())

    assert [s["type"] for s in out] == ["hook", "repo", "links", "follow"]
    assert out[2]["links"] == [n["url"] for n in _repo_notes()]


def test_an_existing_links_slide_is_left_alone():
    slides = [{"type": "hook", "headline": "h", "sub": "s"},
              {"type": "links", "headline": "All the links", "links": ["u"]}]
    assert ensure_links_slide(slides, _repo_notes()) == slides


def test_b4_a_links_slide_carrying_only_sub_is_dropped_so_the_real_index_still_gets_added():
    """B4: REQUIRED_BODY_FIELDS["links"] used to list "sub" as body content
    alongside "links" itself, so a model-emitted links slide with a `sub`
    line but no actual `links` array survived normalise_slides on `sub`
    alone. Because a "links"-typed slide was then present, ensure_links_slide's
    `"links" in types` guard treated the index as already satisfied and never
    added the real one — the deck shipped `links.html.j2`'s `<ol
    class="linklist">` with zero `<li>`s under a "Screenshot this slide." sub.
    `sub` must not count as body content for "links"; only the `links` field
    itself does, so the empty slide is dropped and the real index is added
    behind it."""
    slides = [
        {"type": "hook", "headline": "h", "sub": "s"},
        {"type": "links", "headline": "All the links", "sub": "Screenshot this slide."},
    ]
    survivors = normalise_slides(slides)
    assert [s["type"] for s in survivors] == ["hook"], (
        "a links slide with no links content must be dropped, not kept on sub alone"
    )

    out = ensure_links_slide(survivors, _repo_notes())
    links_slide = next((s for s in out if s["type"] == "links"), None)
    assert links_slide is not None, "the real index must be added once the fake one is dropped"
    assert links_slide["links"] == [n["url"] for n in _repo_notes()]


def test_b4_a_facts_slide_carrying_only_sub_is_dropped():
    """Weaker instance of the same bug: a "facts" slide with a `sub` but no
    `rows` used to survive as a mislabeled point slide under a FACTS kicker.
    `sub` must not count as body content for "facts" either."""
    out = normalise_slides([
        {"type": "facts", "headline": "The split", "sub": "a lone sentence, no rows"},
    ])
    assert out == []


def test_one_url_is_not_an_index():
    slides = [{"type": "hook", "headline": "h", "sub": "s"},
              {"type": "repo", "headline": "r0"}]
    assert ensure_links_slide(slides, _repo_notes(1)) == slides


def test_the_index_sits_before_the_closing_slides():
    """Tail order is links, then sources, then follow."""
    slides = [{"type": "hook", "headline": "h", "sub": "s"},
              {"type": "repo", "headline": "r"},
              {"type": "sources", "headline": "Sources", "urls": ["u"]},
              {"type": "follow", "headline": "Follow for more", "sub": "s"}]
    out = [s["type"] for s in ensure_links_slide(slides, _repo_notes())]
    assert out == ["hook", "repo", "links", "sources", "follow"]


def test_a_full_deck_makes_room_for_the_index():
    slides = ([{"type": "hook", "headline": "h", "sub": "s"}]
              + [{"type": "repo", "headline": f"r{i}"} for i in range(8)]
              + [{"type": "follow", "headline": "Follow for more", "sub": "s"}])
    out = ensure_links_slide(slides, _repo_notes())
    assert len(out) == MAX_SLIDES
    assert [s["type"] for s in out][-2:] == ["links", "follow"]


def test_duplicate_urls_appear_once():
    notes = _repo_notes(2) + _repo_notes(2)
    out = ensure_links_slide(
        [{"type": "hook", "headline": "h", "sub": "s"},
         {"type": "repo", "headline": "r"}], notes)
    assert len(out[-1]["links"]) == 2


# --- the two guarantees must compose ----------------------------------------
#
# Each was correct alone and wrong together: ensure_closing_slide trimmed the
# deck from the end to make room, and the end was exactly where the links index
# had just been placed. The index was created and silently discarded, so an
# enumeration still shipped without one and every test still passed.

def test_the_links_index_survives_the_closing_slide():
    deck = ([{"type": "hook", "headline": "h", "sub": "s"}]
            + [{"type": "repo", "headline": f"r{i}", "name": f"r{i}"}
               for i in range(9)])
    notes = [{"url": f"https://github.com/o{i}/r{i}"} for i in range(9)]

    out = ensure_closing_slide(
        ensure_links_slide(deck, notes), is_dm=True, source_urls=[])
    types = [s["type"] for s in out]

    assert "links" in types, "the index must not be trimmed away to fit a follow slide"
    assert types[-2:] == ["links", "follow"]
    assert len(out) == MAX_SLIDES


def test_trimming_comes_out_of_the_body_not_the_tail():
    deck = ([{"type": "hook", "headline": "h", "sub": "s"}]
            + [{"type": "point", "headline": f"p{i}", "sub": "s"} for i in range(8)]
            + [{"type": "links", "headline": "All the links", "links": ["u", "v"]}])

    out = ensure_closing_slide(deck, is_dm=False, source_urls=["https://a.example"])
    types = [s["type"] for s in out]

    assert len(out) == MAX_SLIDES
    # A links index already attributes the deck, so no sources slide is added.
    assert types[-2:] == ["links", "follow"]
    assert types.count("point") == 7, "body slides give up the room"


def test_closing_slides_end_up_in_order_however_they_arrived():
    deck = [{"type": "hook", "headline": "h", "sub": "s"},
            {"type": "follow", "headline": "Follow for more", "sub": "s"},
            {"type": "links", "headline": "All the links", "links": ["u", "v"]}]
    out = ensure_closing_slide(deck, is_dm=False, source_urls=["https://a.example"])
    assert [s["type"] for s in out] == ["hook", "links", "follow"]


# ------------------------------------------------- integrity, at the stage


async def test_compose_rejects_an_unsupported_absence_claim(fake_llm, settings):
    """Unit-testing integrity.check_deck proves the rule; this proves compose
    applies it. Item 116 shipped because nothing between the rule and the
    renderer connected them."""
    bad = _doc(
        slides=[
            {"type": "hook", "headline": "What the research doesn't show",
             "sub": "The missing longitudinal data"},
            {"type": "point", "headline": "Detail", "bullets": ["a", "b"]},
            {"type": "takeaway", "headline": "The gap is the finding",
             "sub": "Nothing covers it."},
        ],
        caption="That research simply hasn't been done yet.")
    good = _doc()
    fake_llm.queue(bad)      # first attempt
    fake_llm.queue(good)     # forced rebuild

    fields = await compose(_item(), fake_llm, settings)

    text = " ".join(str(s) for s in fields["slides"]) + fields["caption"]
    assert "gap is the finding" not in text
    assert "hasn't been done" not in text


async def test_compose_parks_the_item_when_the_rebuild_still_claims_it(
        fake_llm, settings):
    """Twice is not a slip, and publishing it would put a false claim on a
    real account."""
    from pipeline.conversation import NeedsInput

    bad = _doc(
        slides=[
            {"type": "hook", "headline": "Fine", "sub": "Fine."},
            {"type": "point", "headline": "Detail", "bullets": ["a", "b"]},
            {"type": "takeaway", "headline": "So what", "sub": "The consequence."},
        ],
        caption="No longitudinal studies exist on this.")
    fake_llm.queue(bad)
    fake_llm.queue(bad)

    with pytest.raises(NeedsInput) as exc:
        await compose(_item(), fake_llm, settings)
    assert "do not support" in exc.value.question


async def test_a_deck_may_repeat_an_absence_its_SOURCES_report(fake_llm, settings):
    """The rule is about who is making the claim. A source saying the research
    does not exist is a finding the deck is entitled to carry."""
    sourced = _item(research=[{
        "claim": "Review finds no longitudinal data exists",
        "detail": "The authors note no longitudinal studies exist on this pathway.",
        "sources": ["https://a.example/1"]}])
    fake_llm.queue(_doc(caption="No longitudinal studies exist on this."))

    fields = await compose(sourced, fake_llm, settings)
    assert "No longitudinal studies exist" in fields["caption"]


async def test_known_gaps_are_put_in_front_of_the_composer(fake_llm, settings):
    """/post builds knowing part of the request is unanswered. Unsaid, the
    model finds the hole itself and builds a thesis around it."""
    fake_llm.queue(_doc())
    await compose(_item(gaps=["how it affects children in later life"]), fake_llm, settings)

    prompt = fake_llm.calls[-1].user
    assert "PARTIAL MATERIAL" in prompt
    assert "how it affects children in later life" in prompt
    assert "must NOT claim" in prompt


def test_a_cta_written_as_a_body_slide_is_dropped():
    """Item 116 shipped nine slides ending on two calls to action: the
    composer's own "Follow for more psychology insights" as a point, then the
    real follow slide."""
    from pipeline.stages.compose import drop_duplicate_ctas
    slides = [
        {"type": "hook", "headline": "Narcissism", "sub": "A spectrum."},
        {"type": "point", "headline": "Follow for more psychology insights",
         "bullets": ["Evidence-based breakdowns weekly"]},
        {"type": "follow", "headline": "More like this", "sub": "Weekly."},
    ]
    kept = drop_duplicate_ctas(slides)
    assert [s["type"] for s in kept] == ["hook", "follow"]


def test_the_real_follow_slide_is_never_dropped():
    from pipeline.stages.compose import drop_duplicate_ctas
    slides = [{"type": "follow", "headline": "Follow for more", "sub": "Weekly."}]
    assert drop_duplicate_ctas(slides) == slides


def test_ordinary_slides_mentioning_following_are_kept():
    """"Followed 200 patients" is not a call to action."""
    from pipeline.stages.compose import drop_duplicate_ctas
    slides = [{"type": "point", "headline": "The study followed 200 patients",
               "bullets": ["Over ten years"]}]
    assert drop_duplicate_ctas(slides) == slides


# ---------------------------------------------------------- D50/T105: notes
#
# item.research never reached the compose prompt at all -- only post-hoc, at
# check_deck / _restore_repo_facts / ensure_links_slide, after the model had
# already guessed. The DECK SHAPE instruction told it to "copy owner, name,
# url, stars and language from the note VERBATIM" from a note it never saw.


def test_notes_block_of_no_notes_is_empty():
    assert _notes_block([]) == ""


def test_notes_block_carries_index_name_and_every_present_fact():
    notes = [
        {"name": "repo0", "owner": "o0", "url": "https://github.com/o0/repo0",
         "stars": 120, "language": "Python", "detail": "A thing worth reading."},
        # A non-repo ("web") catalogue note: no owner/stars/language at all.
        {"claim": "web-thing", "detail": "No repo fields here.",
         "url": "https://example.com/x"},
    ]
    block = _notes_block(notes)

    assert block.startswith("RESEARCHED ITEMS:\n")
    assert ("[1] name=repo0 owner=o0 url=https://github.com/o0/repo0 "
            "stars=120 language=Python") in block
    assert "A thing worth reading." in block
    assert "[2] name=web-thing url=https://example.com/x" in block
    assert "No repo fields here." in block
    # The web-kind note has none of these -- nothing here may invent them.
    web_paragraph = block.split("[2]", 1)[1]
    assert "owner=" not in web_paragraph
    assert "stars=" not in web_paragraph
    assert "language=" not in web_paragraph


def test_notes_block_keeps_a_falsy_but_real_star_count():
    """0 stars is a real, valid fact (a brand-new repo) -- distinct from a
    missing one. A naive `if value:` truthiness check would silently drop
    it, the same class of bug as treating an empty string and a real zero
    as the same "nothing here"."""
    notes = [{"name": "repo0", "owner": "o0", "url": "https://x/repo0",
              "stars": 0, "language": "Python", "detail": "d"}]
    block = _notes_block(notes)
    assert "stars=0" in block


def test_notes_block_with_no_name_or_claim_still_carries_the_detail():
    """A malformed note (neither name nor claim survived) must not crash
    the prompt build -- it degrades to a bare index, not an exception."""
    notes = [{"detail": "orphaned detail with no identifying name."}]
    block = _notes_block(notes)
    assert "orphaned detail with no identifying name." in block
    assert "[1]" in block
    assert "name=" not in block


def test_notes_block_sanitizes_a_forged_marker_even_with_no_budget():
    """A bounce-1 review flagged that the sanitize guarantee (budget.py's
    marker-forgery stripping -- see its module docstring) used to be
    coupled to whether a caller passed a real budget: budget_chars=None
    skipped pack() entirely, so a forged sentinel-wrapped marker in a
    note's detail would have passed straight into the prompt unsanitized
    on that path. Not production-reachable today (compose()'s one real
    call site always resolves a real settings-derived budget), but the
    guarantee belongs to this function, not to its caller's behaviour --
    so pin it directly against the None path itself."""
    forged = _MARKER_SENTINEL + "[... 999999 chars omitted ...]" + _MARKER_SENTINEL
    notes = [{"name": "n", "detail": f"legit text {forged} more legit text"}]

    block = _notes_block(notes, budget_chars=None)

    assert not marker_present(block), (
        "a forged marker in note detail must be stripped even with no "
        "explicit budget"
    )
    assert _MARKER_SENTINEL not in block


async def test_deck_shape_field_names_match_notes_block_field_names(fake_llm, settings):
    """D21's house rule, applied here: the DECK SHAPE prose says 'copy
    owner, name, url, stars and language from the note VERBATIM'. This
    parses that sentence out of a REAL composed prompt -- not a third
    hardcoded literal set living inside the test, which is exactly the kind
    of copy this test exists to catch drifting, and which a bounce-1 review
    caught this test doing (it pinned _NOTE_FACT_FIELDS against a literal
    the test itself wrote, so it stayed green through mutations that dropped
    or added a field in the DECK SHAPE prose). Pinned against
    _NOTE_FACT_FIELDS (plus the header's own "name="), so a rename on either
    side -- or a field named in the prose but never emitted, or emitted but
    never named -- fails loudly instead of drifting the way the
    three-places contract already has once.
    """
    import re

    fake_llm.queue(_doc())
    await compose(_item(intent="list", research=_list_notes(3), clauses=[]),
                  fake_llm, settings)

    prompt = fake_llm.calls[0].user
    match = re.search(r"Copy\s+(.*?)\s+from the\s+note VERBATIM", prompt, re.DOTALL)
    assert match, f"DECK SHAPE prose's 'Copy ... from the note VERBATIM' sentence not found in:\n{prompt}"
    named_fields = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert named_fields == set(_NOTE_FACT_FIELDS) | {"name"}


async def test_compose_prompt_for_a_list_carries_every_notes_name_and_url(
        fake_llm, settings):
    """AC1: a composed prompt for a 12-note list contains all 12 names and
    all 12 URLs."""
    notes = _list_notes(12)
    fake_llm.queue(_doc())
    await compose(_item(intent="list", research=notes, clauses=[]),
                  fake_llm, settings)

    prompt = fake_llm.calls[0].user
    assert "RESEARCHED ITEMS" in prompt
    for note in notes:
        assert note["name"] in prompt
        assert note["url"] in prompt


async def test_list_prompt_drops_the_separate_truncated_source_url_list(
        fake_llm, settings):
    """AC4: source_urls[:8] is now redundant for the list path -- every
    note's own url already travels in RESEARCHED ITEMS, uncapped."""
    fake_llm.queue(_doc())
    await compose(_item(intent="list", research=_list_notes(3), clauses=[]),
                  fake_llm, settings)

    prompt = fake_llm.calls[-1].user
    assert "AVAILABLE SOURCE URLS" not in prompt
    assert "RESEARCHED ITEMS" in prompt


async def test_news_prompt_does_not_gain_a_redundant_notes_block(fake_llm, settings):
    """What was concluded about the non-list path: synthesize.SYSTEM already
    demands "attribute every fact" and "prefer specifics" for news, unlike
    LIST_SYSTEM which explicitly withholds item-by-item detail -- so a news
    BRIEF is already a synthesis of the notes and a notes block would
    duplicate it rather than fill a gap. AVAILABLE SOURCE URLS is untouched
    on this path -- unchanged behaviour, still asserted by
    test_compose_passes_source_urls_for_the_sources_slide above."""
    fake_llm.queue(_doc())
    await compose(_item(intent="news"), fake_llm, settings)

    assert "RESEARCHED ITEMS" not in fake_llm.calls[-1].user


async def test_notes_block_adds_zero_model_calls(fake_llm, settings):
    """AC4: zero added model calls. A list item with no clauses (/post's
    normal shape) must still make exactly the one compose call --
    test_an_item_with_no_clauses_skips_the_critique proves this invariant
    for the general case; this proves the notes block did not add a second
    one for the path it actually touches."""
    fake_llm.queue(_doc())
    await compose(_item(intent="list", research=_list_notes(12), clauses=[]),
                  fake_llm, settings)
    assert len(fake_llm.calls) == 1


async def test_restore_repo_facts_actually_matches_the_researched_note(
        fake_llm, settings):
    """AC2 regression: before the notes reached the prompt, the model could
    only guess a repo's real name from the URL, so _restore_repo_facts' by-
    name lookup silently no-opped on every guess that missed -- it ran, but
    matched nothing. Prove a real match now happens: the fake model returns
    FABRICATED facts under the note's real name, and the note's real facts
    -- not the model's invented ones -- must survive into the output."""
    note = {
        "name": "repo0", "claim": "owner0/repo0", "owner": "owner0",
        "url": "https://github.com/owner0/repo0", "stars": 4242,
        "language": "Rust", "detail": "A real fact from research.",
    }
    fake_llm.queue(_doc(slides=[
        {"type": "hook", "headline": "Best Rust tools", "sub": "s"},
        {"type": "repo", "headline": "repo0", "name": "repo0",
         # Fabricated -- not what the note says, and must not survive.
         "owner": "made-up-owner", "url": "https://example.invalid/wrong",
         "stars": 1, "language": "Cobol", "sub": "invented summary"},
        {"type": "follow", "headline": "Follow for more", "sub": "s"},
    ]))

    out = await compose(_item(intent="list", research=[note], clauses=[]),
                         fake_llm, settings)

    repo_slide = next(s for s in out["slides"] if s["type"] == "repo")
    assert repo_slide["owner"] == "owner0"
    assert repo_slide["url"] == "https://github.com/owner0/repo0"
    assert repo_slide["stars"] == 4242
    assert repo_slide["language"] == "Rust"


async def test_compose_strips_facts_the_note_never_researched(fake_llm, settings):
    """T111 end-to-end, live-reproduced 2/2 against the real hosted model:
    when a note genuinely lacks an optional fact, the model invents a
    plausible value for it anyway -- its only evidence for the field is the
    GitHub URL in RESEARCHED ITEMS, not the field itself. Reproduces both
    observed cases in one deck: a fabricated "language" on a note that never
    had one, and a fabricated non-zero "stars" on a note that genuinely said
    zero (a brand-new repo)."""
    note = {
        "name": "repo0", "claim": "owner0/repo0", "owner": "owner0",
        "url": "https://github.com/owner0/repo0",
        "stars": 0,  # a real, newly-published repo
        "language": "",  # enumerate_items.py's "nothing detected" sentinel
        "detail": "A brand-new repo with no stars yet.",
    }
    fake_llm.queue(_doc(slides=[
        {"type": "hook", "headline": "New Rust tools", "sub": "s"},
        {"type": "repo", "headline": "repo0", "name": "repo0", "owner": "owner0",
         "url": "https://github.com/owner0/repo0",
         "stars": 4200,        # fabricated -- the note said 0
         "language": "Cobol",  # fabricated -- the note has no language
         "sub": "invented summary"},
        {"type": "follow", "headline": "Follow for more", "sub": "s"},
    ]))

    out = await compose(_item(intent="list", research=[note], clauses=[]),
                         fake_llm, settings)

    repo_slide = next(s for s in out["slides"] if s["type"] == "repo")
    assert repo_slide["stars"] == 0, "a genuine zero must overwrite the fabricated 4200"
    assert "language" not in repo_slide, "an absent fact must not survive as an invention"


def test_notes_block_at_realistic_sizes_fits_the_local_fallback_budget(settings):
    """AC3: a 15-note list at realistic note sizes produces a prompt that
    fits num_ctx_good -- and at realistic sizes nothing should need to be
    dropped at all."""
    notes = _list_notes(15)
    budget_chars = budget_chars_for_role(settings, role="good")

    block = _notes_block(notes, budget_chars)

    assert not marker_present(block), "realistic notes must not need trimming"
    for note in notes:
        assert note["name"] in block
        assert note["url"] in block
    assert len(block) < budget_chars


async def test_compose_budgets_the_notes_block_against_the_good_role_not_cheap(
        fake_llm, settings):
    """compose() calls llm.good() for the list path, so the notes block must
    be sized against num_ctx_good -- not num_ctx_cheap, and not left
    unbudgeted. Sized so the corpus overflows the cheap budget but not the
    good one, so a role mix-up shows up as a dropped note rather than
    passing by coincidence (the earlier realistic-size test is too small to
    tell the two budgets apart at all)."""
    notes = [{
        "name": f"repo{i}", "owner": f"owner{i}",
        "url": f"https://github.com/owner{i}/repo{i}", "stars": i,
        "language": "Python", "detail": "x" * 1200,
    } for i in range(15)]

    cheap_budget = budget_chars_for_role(settings, role="cheap")
    good_budget = budget_chars_for_role(settings, role="good")
    received = len(_notes_block(notes, budget_chars=None))
    assert cheap_budget < received < good_budget, (
        "fixture must actually straddle the two budgets"
    )

    fake_llm.queue(_doc())
    await compose(_item(intent="list", research=notes, clauses=[]),
                  fake_llm, settings)

    prompt = fake_llm.calls[-1].user
    assert not marker_present(prompt), (
        "the good-role budget must not have needed to trim this"
    )
    assert "x" * 1200 in prompt, "every note's detail must survive intact"


def test_notes_block_logs_only_when_something_was_actually_dropped(
        settings, caplog):
    """Fair-share packing (T70) lets a short note pass through untouched
    while a long one next to it still gets trimmed -- the log must fire on
    that PARTIAL case (any note omitted), not only when every note was cut
    (all notes omitted)."""
    notes = [
        {"name": "small", "detail": "short and complete."},
        {"name": "big", "detail": "y" * 5000},
    ]
    budget_chars = 2000

    with caplog.at_level(logging.WARNING, logger="pipeline.stages.compose"):
        block = _notes_block(notes, budget_chars)

    assert "short and complete." in block, "the note that fit must be untouched"
    assert marker_present(block), "the oversized note must still be trimmed"
    assert any("packed into" in r.getMessage() for r in caplog.records)


async def test_notes_block_stays_within_budget_and_logs_what_it_drops(
        settings, caplog):
    """AC3: whatever IS dropped (an adversarial/oversized corpus) is logged
    with kept + dropped == received (D5), reusing budget.pack() (T70)
    rather than a second trimming implementation."""
    notes = [
        {"name": f"repo{i}", "owner": f"owner{i}",
         "url": f"https://github.com/owner{i}/repo{i}", "stars": i,
         "language": "Python", "detail": "x" * 50_000}
        for i in range(15)
    ]
    budget_chars = budget_chars_for_role(settings, role="good")

    # The un-budgeted rendering is the oracle for "what should be kept if
    # nothing were dropped" -- same formatting, pack() simply not invoked.
    full = _notes_block(notes, budget_chars=None)
    header_len = len("RESEARCHED ITEMS:\n")
    separators = 2 * (len(notes) - 1)  # "\n\n".join overhead between notes
    received_total = len(full) - header_len - separators

    with caplog.at_level(logging.WARNING, logger="pipeline.stages.compose"):
        block = _notes_block(notes, budget_chars)

    assert marker_present(block), "50k-char details must actually get trimmed"
    body = block[header_len:]
    assert len(body) <= budget_chars, "the packed body must respect the budget"

    manifest = next(
        r.args[2] for r in caplog.records if "packed into" in r.getMessage()
    )
    kept_total = sum(d["kept"] for d in manifest)
    omitted_total = sum(d["omitted"] for d in manifest)
    assert kept_total + omitted_total == received_total
