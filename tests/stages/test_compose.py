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


def test_slide_vocabulary_uses_facts_not_compare():
    """The PoC named this type `compare` and the model correctly ignored the
    comparison semantics, emitting a label/value table. The name was wrong."""
    enum = SLIDES_SCHEMA["properties"]["slides"]["items"]["properties"]["type"]["enum"]
    assert "facts" in enum
    assert "compare" not in enum
    assert enum == SLIDE_TYPES


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
