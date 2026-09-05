from dataclasses import replace

import pytest

from pipeline.errors import Retryable
from pipeline.models import Item, Status
from pipeline.stages.compose import (
    MAX_SLIDES,
    SLIDE_TYPES,
    SLIDES_SCHEMA,
    build_caption,
    compose,
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
            {"type": "hook", "headline": "Big news"},
            {"type": "point", "headline": "Detail", "bullets": ["a", "b"]},
            {"type": "takeaway", "headline": "So what"},
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


def test_facts_and_compare_are_distinct_types():
    """The PoC had only `compare`, and the model used it as a label/value
    table. Both now exist with separate jobs: `facts` is label/value, `compare`
    is a genuine two-column A-vs-B with column titles."""
    enum = SLIDES_SCHEMA["properties"]["slides"]["items"]["properties"]["type"]["enum"]
    assert "facts" in enum and "compare" in enum
    assert enum == SLIDE_TYPES

    props = SLIDES_SCHEMA["properties"]["slides"]["items"]["properties"]
    assert "left_title" in props and "right_title" in props, \
        "compare needs column titles to be distinguishable from facts"


def test_schema_clamps_slide_count_to_instagram_maximum():
    slides = SLIDES_SCHEMA["properties"]["slides"]
    assert slides["minItems"] == 3
    assert slides["maxItems"] == MAX_SLIDES == 10


# --------------------------------------------------------------- normalising


def test_normalise_puts_hook_first_and_sources_last():
    out = normalise_slides([
        {"type": "sources", "headline": "S", "urls": ["https://a"]},
        {"type": "point", "headline": "P"},
        {"type": "hook", "headline": "H"},
    ])
    assert [s["type"] for s in out] == ["hook", "point", "sources"]


def test_normalise_demotes_surplus_hooks_rather_than_dropping_them():
    out = normalise_slides([
        {"type": "hook", "headline": "First"},
        {"type": "hook", "headline": "Second"},
        {"type": "point", "headline": "P"},
    ])
    assert [s["type"] for s in out] == ["hook", "point", "point"]
    assert out[1]["headline"] == "Second", "researched content must not be lost"


def test_normalise_keeps_one_sources_slide():
    out = normalise_slides([
        {"type": "hook", "headline": "H"},
        {"type": "sources", "headline": "S1", "urls": ["https://a"]},
        {"type": "sources", "headline": "S2", "urls": ["https://b"]},
    ])
    assert [s["type"] for s in out].count("sources") == 1


def test_normalise_drops_unknown_types_and_empty_headlines():
    out = normalise_slides([
        {"type": "hook", "headline": "H"},
        {"type": "compare", "headline": "old name"},
        {"type": "point", "headline": "   "},
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
    many = [{"type": "hook", "headline": "H"}] + [
        {"type": "point", "headline": f"P{i}"} for i in range(15)
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


def test_build_caption_caps_hashtags_at_twelve():
    out = build_caption("x", [f"t{i}" for i in range(20)])
    assert out.count("#") == 12


# ------------------------------------------------------------------- compose


async def test_compose_returns_slides_and_caption(fake_llm, settings):
    fake_llm.queue(_doc())
    out = await compose(_item(), fake_llm, replace(settings, source_credit="@aipost"))

    assert [s["type"] for s in out["slides"]] == ["hook", "point", "takeaway"]
    assert "Source: @aipost" in out["caption"]


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
        {"type": "hook", "headline": "H"},
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
        {"type": "hook", "headline": "Fine"},
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
        [{"type": "point", "headline": "P", "image": 7, "image_mode": "inset"}],
        _images("hero"),
    )
    assert "image" not in out[0]


def test_image_cannot_be_promoted_above_its_rating():
    """Vision said background-only; the composer must not make it a hero."""
    out = normalise_slides(
        [{"type": "point", "headline": "P", "image": 0, "image_mode": "hero"}],
        _images("background"),
    )
    assert "image" not in out[0]


def test_image_may_be_used_more_modestly_than_rated():
    out = normalise_slides(
        [{"type": "point", "headline": "P", "image": 0, "image_mode": "background"}],
        _images("hero"),
    )
    assert out[0]["image_mode"] == "background"


def test_photo_slide_without_an_image_is_dropped():
    out = normalise_slides(
        [{"type": "hook", "headline": "H"}, {"type": "photo", "headline": "No image"}],
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
