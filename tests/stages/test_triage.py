
import pytest

from pipeline.models import Item, Status
from pipeline.stages.triage import triage

REAL_NEWS = (
    "OpenAI announced it will block Cursor users from accessing OpenAI models "
    "within the next three months. Cursor says the models account for about "
    "five percent of its user traffic."
)


def _item(db_id=1, source="channel", text=REAL_NEWS, media=None, extracted=None):
    return Item(
        id=db_id, source=source, status=Status.EXTRACTED, raw_text=text,
        raw_media_paths=media or [], extracted=extracted or {},
    )


def _with_image(description, text=""):
    """A post whose story lives entirely in an attached screenshot."""
    return _item(text=text, media=["/tmp/a.png"],
                 extracted={"image_descriptions": [{"path": "/tmp/a.png",
                                                    "description": description}]})


async def test_dm_bypasses_triage_without_calling_the_model(db, fake_llm, settings):
    """Operator submissions skip the gate — and must not spend a model call."""
    out = await triage(_item(source="dm", text="short"), fake_llm, db, settings, threshold=6)

    assert out["triage_score"] == 10
    assert out["_next"] == Status.TRIAGED
    assert fake_llm.calls == []


async def test_high_score_advances(db, fake_llm, settings):
    fake_llm.queue({"score": 8, "reason": "concrete claim", "topic": "ai"})
    out = await triage(_item(), fake_llm, db, settings, threshold=6)
    assert out["_next"] == Status.TRIAGED and out["triage_score"] == 8


async def test_score_exactly_at_threshold_advances(db, fake_llm, settings):
    fake_llm.queue({"score": 6, "reason": "borderline", "topic": "ai"})
    out = await triage(_item(), fake_llm, db, settings, threshold=6)
    assert out["_next"] == Status.TRIAGED


async def test_low_score_drops_with_reason(db, fake_llm, settings):
    fake_llm.queue({"score": 2, "reason": "no verifiable claim", "topic": "chatter"})
    out = await triage(_item(), fake_llm, db, settings, threshold=6)

    assert out["_next"] == Status.DROPPED
    assert out["triage_reason"] == "no verifiable claim"


async def test_very_short_text_drops_without_a_model_call(db, fake_llm, settings):
    out = await triage(_item(text="gm"), fake_llm, db, settings)

    assert out["_next"] == Status.DROPPED
    assert fake_llm.calls == [], "not worth a model call"


async def test_caption_less_image_post_is_judged_on_its_image(db, fake_llm, settings):
    """Regression: the target channel posts screenshots with no caption at all.

    Triage used to run before extraction, so it saw an empty string, scored it
    1, and dropped a real story — surfacing in the digest as a quiet channel
    rather than as a blind pipeline.
    """
    fake_llm.queue({"score": 8, "reason": "concrete launch claim", "topic": "ai"})
    item = _with_image(
        "Headline reads 'OpenAI ships GPT-5.5 to all Plus subscribers, "
        "40% faster, 400k context'. Screenshot of a tweet by @sama."
    )

    out = await triage(item, fake_llm, db, settings)

    assert out["_next"] == Status.TRIAGED
    assert "GPT-5.5" in fake_llm.calls[0].user, "image text must reach the model"


async def test_image_post_with_nothing_legible_still_drops(db, fake_llm, settings):
    """Extraction running first must not turn triage into a rubber stamp."""
    out = await triage(_with_image("A blurry photo of a keyboard."), fake_llm, db, settings)
    assert out["_next"] == Status.DROPPED
    assert fake_llm.calls == []


async def test_extracted_link_text_reaches_triage(db, fake_llm, settings):
    fake_llm.queue({"score": 7, "reason": "real article", "topic": "ai"})
    item = _item(text="see this", extracted={"url_texts": [
        {"url": "https://example.com/a",
         "text": "Anthropic released a new model with a 1M token context window."},
    ]})

    await triage(item, fake_llm, db, settings)

    assert "1M token context" in fake_llm.calls[0].user


async def test_near_duplicate_of_a_recent_item_drops(db, fake_llm, settings):
    """Channels repost the same story with trivial edits."""
    await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=1, raw_text=REAL_NEWS
    )
    second = await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=2,
        raw_text=REAL_NEWS.upper() + "!!!",
    )

    out = await triage(_item(db_id=second), fake_llm, db, settings)

    assert out["_next"] == Status.DROPPED
    assert "duplicate" in out["triage_reason"]
    assert fake_llm.calls == []


async def test_item_is_not_a_duplicate_of_itself(db, fake_llm, settings):
    """Regression: the item is already stored, so a naive hash check matched
    its own row and dropped every channel item."""
    only = await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=1, raw_text=REAL_NEWS
    )
    fake_llm.queue({"score": 9, "reason": "real", "topic": "ai"})

    out = await triage(_item(db_id=only), fake_llm, db, settings)

    assert out["_next"] == Status.TRIAGED


# ------------------------------------------- scoring criteria regressions

UBER_ROBOTAXI = (
    "🇬🇧 Uber launches London's FIRST AI-powered robotaxis, beating Waymo to the UK.\n\n"
    "Uber is deploying 20 Ford Mustang Mach-Es powered by Wayve's AI Driver, which "
    "learns from real-world driving and adapts to new roads, weather and cities, "
    "making London only the second European city where Uber offers autonomous rides."
)


async def test_prompt_forbids_scoring_down_for_missing_sources(db, fake_llm, settings):
    """Regression: triage dropped a Uber/Wayve robotaxi launch with score 2,
    reasoning it was 'unsubstantiated… no official announcement, source, or data
    point'. That is the research stage's job, not triage's — and every item from
    a news channel arrives unsourced, so this rejected exactly the well-specified
    stories the pipeline exists to process.
    """
    fake_llm.queue({"score": 9, "reason": "names Uber, Wayve, 20 vehicles, London",
                    "topic": "autonomous vehicles"})
    await triage(_item(text=UBER_ROBOTAXI), fake_llm, db, settings)

    system = fake_llm.calls[0].system
    assert "NOT judging whether the claim is true" in system
    assert "Never lower a score because no source" in system
    assert "researchable" in system


async def test_specific_unsourced_story_is_expected_to_pass(db, fake_llm, settings):
    fake_llm.queue({"score": 9, "reason": "specific and researchable", "topic": "av"})
    out = await triage(_item(text=UBER_ROBOTAXI), fake_llm, db, settings, threshold=6)
    assert out["_next"] == Status.TRIAGED


# ------------------------------------------------------- T90/D5: packed input
#
# T61 named THREE uncapped-text-into-`cheap` sites; T70 closed the research
# corpus (research.py's note-writing call). This is the second: triage reads
# `judgeable_text` (raw body + every extracted image description + every
# linked page's FULL text, search.py's MAX_TEXT_CHARS = 0 means no cap at
# extraction) straight into `llm.cheap` at the same fixed `num_ctx_cheap`.
# Ollama silently drops whatever does not fit rather than erroring, so a long
# attached article could lose its back half before triage ever scores it.
# budget.pack() (T70) is reused, not reinvented.


async def test_adversarial_long_attached_article_is_packed_with_a_trace(
    db, fake_llm, settings, caplog
):
    """A 64k-char attached page — comfortably larger than the default
    num_ctx_cheap budget (settings fixture: 8192 tokens -> (8192-2048)*2 =
    12,288 chars) — must reach the model CUT, with a marker proving it, not
    silently truncated by whatever the transport happens to drop."""
    import logging

    from pipeline.budget import budget_chars_for_role, marker_present

    fake_llm.queue({"score": 8, "reason": "long article", "topic": "ai"})
    huge_article = "Fact about the launch. " * 3000  # ~72,000 chars
    assert len(huge_article) > budget_chars_for_role(settings, role="cheap"), (
        "sanity: this body must actually exceed the budget or this test "
        "proves nothing"
    )
    item = _item(text="see this huge writeup", extracted={"url_texts": [
        {"url": "https://example.com/huge", "text": huge_article},
    ]})

    with caplog.at_level(logging.INFO):
        await triage(item, fake_llm, db, settings)

    user = fake_llm.calls[0].user
    budget_chars = budget_chars_for_role(settings, role="cheap")
    # A little slack for the "ITEM:\n" prefix and the marker text itself —
    # the point is the prompt is bounded by the budget, not exactly equal.
    assert len(user) <= budget_chars + 200, (
        "the packed prompt must be bounded by the cheap-role budget, not "
        "grow with the source article"
    )
    assert marker_present(user), (
        "a cut this large must leave a trace (D5), never vanish silently"
    )
    assert "packed" in caplog.text and "omitted" in caplog.text, (
        "the omission must be logged — kept + omitted == original, but "
        "traceably, not just internally consistent"
    )


async def test_short_article_reaches_the_model_unmarked(db, fake_llm, settings):
    """The common case: nothing to trim, so no marker and no omission — the
    packer must be a no-op on ordinary input, not a source of new noise."""
    from pipeline.budget import marker_present

    fake_llm.queue({"score": 8, "reason": "concrete claim", "topic": "ai"})
    await triage(_item(), fake_llm, db, settings)

    user = fake_llm.calls[0].user
    assert not marker_present(user)
    assert REAL_NEWS in user


# ---- T275: BadCompletion on malformed verdict

async def test_non_numeric_score_raises_badcompletion(db, fake_llm, settings):
    """T275: score that is not numeric (e.g., "high") should raise BadCompletion."""
    from pipeline.errors import BadCompletion

    fake_llm.queue({"score": "high", "reason": "concrete claim", "topic": "ai"})
    with pytest.raises(BadCompletion):
        await triage(_item(), fake_llm, db, settings)


async def test_none_score_raises_badcompletion(db, fake_llm, settings):
    """T275: score that is None should raise BadCompletion."""
    from pipeline.errors import BadCompletion

    fake_llm.queue({"score": None, "reason": "concrete claim", "topic": "ai"})
    with pytest.raises(BadCompletion):
        await triage(_item(), fake_llm, db, settings)


async def test_verdict_as_list_raises_badcompletion(db, fake_llm, settings):
    """T275: verdict that is a list instead of dict should raise BadCompletion."""
    from pipeline.errors import BadCompletion

    fake_llm.queue([8, "concrete claim", "ai"])
    with pytest.raises(BadCompletion):
        await triage(_item(), fake_llm, db, settings)


async def test_verdict_as_string_raises_badcompletion(db, fake_llm, settings):
    """T275: verdict that is a string instead of dict should raise BadCompletion."""
    from pipeline.errors import BadCompletion

    fake_llm.queue("score: 8")
    with pytest.raises(BadCompletion):
        await triage(_item(), fake_llm, db, settings)


async def test_infinite_score_raises_badcompletion(db, fake_llm, settings):
    """T275: json.loads accepts Infinity; int(inf) raises OverflowError, which
    must surface as BadCompletion, not an unclassified failure."""
    from pipeline.errors import BadCompletion

    fake_llm.queue({"score": float("inf"), "reason": "x", "topic": "ai"})
    with pytest.raises(BadCompletion):
        await triage(_item(), fake_llm, db, settings)


async def test_valid_verdict_passes(db, fake_llm, settings):
    """T275: valid verdict dict with numeric score still works."""
    fake_llm.queue({"score": 8, "reason": "concrete claim", "topic": "ai"})
    out = await triage(_item(), fake_llm, db, settings)
    assert out["triage_score"] == 8
