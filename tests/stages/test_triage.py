import pytest

from pipeline.models import Item, Status
from pipeline.stages.triage import triage

REAL_NEWS = (
    "OpenAI announced it will block Cursor users from accessing OpenAI models "
    "within the next three months. Cursor says the models account for about "
    "five percent of its user traffic."
)


def _item(db_id=1, source="channel", text=REAL_NEWS, media=None):
    return Item(
        id=db_id, source=source, status=Status.INGESTED, raw_text=text,
        raw_media_paths=media or [],
    )


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


async def test_short_text_with_media_still_reaches_the_model(db, fake_llm):
    """A screenshot with a two-word caption can still be a real story."""
    fake_llm.queue({"score": 7, "reason": "screenshot of announcement", "topic": "ai"})
    out = await triage(_item(text="big news", media=["/tmp/a.png"]), fake_llm, db)

    assert out["_next"] == Status.TRIAGED
    assert len(fake_llm.calls) == 1


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
