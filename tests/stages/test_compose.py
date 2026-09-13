from dataclasses import replace

import pytest

from pipeline.errors import Retryable
from pipeline.models import Item, Status
from pipeline.stages.compose import (
    MAX_HASHTAGS,
    MAX_SLIDES,
    SLIDE_TYPES,
    SLIDES_SCHEMA,
    MAX_SOURCE_URLS,
    build_caption,
    ensure_closing_slide,
    ensure_links_slide,
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
