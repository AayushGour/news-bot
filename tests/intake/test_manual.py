"""Queueing a request by hand, from the CLI or the dashboard."""

import pytest

from pipeline.intake.manual import MIN_REQUEST_CHARS, TooThin, queue_request
from pipeline.models import Status

REQUEST = "What is the current state of homomorphic encryption in production"


async def test_a_request_becomes_an_ingested_item(db, settings):
    item_id = await queue_request(db, settings, REQUEST)
    item = await db.get_item(item_id)

    assert item.status == Status.INGESTED
    assert item.raw_text == REQUEST
    assert item.source == "dm"


async def test_hand_queued_ids_are_negative(db, settings):
    """Telegram ids are positive and increasing. A hand-queued item taking one
    could collide with a message the listener backfills later, and the UNIQUE
    constraint would silently drop one of the two."""
    item_id = await queue_request(db, settings, REQUEST)
    assert (await db.get_item(item_id)).source_msg_id < 0


async def test_successive_requests_do_not_collide(db, settings):
    ids = [await queue_request(db, settings, f"{REQUEST} number {n}")
           for n in range(3)]
    msg_ids = [(await db.get_item(i)).source_msg_id for i in ids]

    assert len(set(ids)) == 3
    assert len(set(msg_ids)) == 3, "each takes its own id"
    assert all(m < 0 for m in msg_ids)


async def test_a_hand_queued_id_cannot_collide_with_a_telegram_message(db, settings):
    """A real message keeps its positive id; the ranges never meet."""
    await db.insert_item(source="channel", source_chat_id=1, source_msg_id=8128,
                         raw_text="a channel post")
    item_id = await queue_request(db, settings, REQUEST)
    assert (await db.get_item(item_id)).source_msg_id < 0


@pytest.mark.parametrize("text", ["", "   ", "hi", "a" * (MIN_REQUEST_CHARS - 1)])
async def test_a_thin_request_is_refused(db, settings, text):
    """Research always finds something, so a bare prompt yields a confident,
    sourced-looking post about whatever it stumbled across."""
    with pytest.raises(TooThin):
        await queue_request(db, settings, text)


async def test_a_refused_request_creates_nothing(db, settings):
    before = (await db.conn.execute_fetchall("SELECT COUNT(*) c FROM items"))[0]["c"]
    with pytest.raises(TooThin):
        await queue_request(db, settings, "hi")
    after = (await db.conn.execute_fetchall("SELECT COUNT(*) c FROM items"))[0]["c"]
    assert after == before


async def test_the_source_can_be_a_channel_item(db, settings):
    item_id = await queue_request(db, settings, REQUEST, source="channel")
    assert (await db.get_item(item_id)).source == "channel"


async def test_surrounding_whitespace_is_stripped(db, settings):
    item_id = await queue_request(db, settings, f"  {REQUEST}  ")
    assert (await db.get_item(item_id)).raw_text == REQUEST
