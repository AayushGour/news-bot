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


async def test_dm_bypasses_triage_without_calling_the_model(db, fake_llm):
    """Operator submissions skip the gate — and must not spend a model call."""
    out = await triage(_item(source="dm", text="short"), fake_llm, db, threshold=6)

    assert out["triage_score"] == 10
    assert out["_next"] == Status.TRIAGED
    assert fake_llm.calls == []


async def test_high_score_advances(db, fake_llm):
    fake_llm.queue({"score": 8, "reason": "concrete claim", "topic": "ai"})
    out = await triage(_item(), fake_llm, db, threshold=6)
    assert out["_next"] == Status.TRIAGED and out["triage_score"] == 8


async def test_score_exactly_at_threshold_advances(db, fake_llm):
    fake_llm.queue({"score": 6, "reason": "borderline", "topic": "ai"})
    out = await triage(_item(), fake_llm, db, threshold=6)
    assert out["_next"] == Status.TRIAGED


async def test_low_score_drops_with_reason(db, fake_llm):
    fake_llm.queue({"score": 2, "reason": "no verifiable claim", "topic": "chatter"})
    out = await triage(_item(), fake_llm, db, threshold=6)

    assert out["_next"] == Status.DROPPED
    assert out["triage_reason"] == "no verifiable claim"


async def test_very_short_text_drops_without_a_model_call(db, fake_llm):
    out = await triage(_item(text="gm"), fake_llm, db)

    assert out["_next"] == Status.DROPPED
    assert fake_llm.calls == [], "not worth a model call"


async def test_caption_less_image_post_is_judged_on_its_image(db, fake_llm):
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

    out = await triage(item, fake_llm, db)

    assert out["_next"] == Status.TRIAGED
    assert "GPT-5.5" in fake_llm.calls[0].user, "image text must reach the model"


async def test_image_post_with_nothing_legible_still_drops(db, fake_llm):
    """Extraction running first must not turn triage into a rubber stamp."""
    out = await triage(_with_image("A blurry photo of a keyboard."), fake_llm, db)
    assert out["_next"] == Status.DROPPED
    assert fake_llm.calls == []


async def test_extracted_link_text_reaches_triage(db, fake_llm):
    fake_llm.queue({"score": 7, "reason": "real article", "topic": "ai"})
    item = _item(text="see this", extracted={"url_texts": [
        {"url": "https://example.com/a",
         "text": "Anthropic released a new model with a 1M token context window."},
    ]})

    await triage(item, fake_llm, db)

    assert "1M token context" in fake_llm.calls[0].user


async def test_near_duplicate_of_a_recent_item_drops(db, fake_llm):
    """Channels repost the same story with trivial edits."""
    await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=1, raw_text=REAL_NEWS
    )
    second = await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=2,
        raw_text=REAL_NEWS.upper() + "!!!",
    )

    out = await triage(_item(db_id=second), fake_llm, db)

    assert out["_next"] == Status.DROPPED
    assert "duplicate" in out["triage_reason"]
    assert fake_llm.calls == []


async def test_item_is_not_a_duplicate_of_itself(db, fake_llm):
    """Regression: the item is already stored, so a naive hash check matched
    its own row and dropped every channel item."""
    only = await db.insert_item(
        source="channel", source_chat_id=-100, source_msg_id=1, raw_text=REAL_NEWS
    )
    fake_llm.queue({"score": 9, "reason": "real", "topic": "ai"})

    out = await triage(_item(db_id=only), fake_llm, db)

    assert out["_next"] == Status.TRIAGED


# ------------------------------------------- scoring criteria regressions

UBER_ROBOTAXI = (
    "🇬🇧 Uber launches London's FIRST AI-powered robotaxis, beating Waymo to the UK.\n\n"
    "Uber is deploying 20 Ford Mustang Mach-Es powered by Wayve's AI Driver, which "
    "learns from real-world driving and adapts to new roads, weather and cities, "
    "making London only the second European city where Uber offers autonomous rides."
)


async def test_prompt_forbids_scoring_down_for_missing_sources(db, fake_llm):
    """Regression: triage dropped a Uber/Wayve robotaxi launch with score 2,
    reasoning it was 'unsubstantiated… no official announcement, source, or data
    point'. That is the research stage's job, not triage's — and every item from
    a news channel arrives unsourced, so this rejected exactly the well-specified
    stories the pipeline exists to process.
    """
    fake_llm.queue({"score": 9, "reason": "names Uber, Wayve, 20 vehicles, London",
                    "topic": "autonomous vehicles"})
    await triage(_item(text=UBER_ROBOTAXI), fake_llm, db)

    system = fake_llm.calls[0].system
    assert "NOT judging whether the claim is true" in system
    assert "Never lower a score because no source" in system
    assert "researchable" in system


async def test_specific_unsourced_story_is_expected_to_pass(db, fake_llm):
    fake_llm.queue({"score": 9, "reason": "specific and researchable", "topic": "av"})
    out = await triage(_item(text=UBER_ROBOTAXI), fake_llm, db, threshold=6)
    assert out["_next"] == Status.TRIAGED
