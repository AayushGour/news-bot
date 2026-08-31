import pytest

from pipeline.db import Database
from pipeline.models import Status


@pytest.fixture
async def db(tmp_path):
    d = await Database(tmp_path / "t.db").connect()
    yield d
    await d.close()


async def test_insert_and_get_roundtrip(db):
    item_id = await db.insert_item(
        source="channel", source_chat_id=-100123, source_msg_id=7, raw_text="hello"
    )
    item = await db.get_item(item_id)
    assert item.status == Status.INGESTED
    assert item.raw_text == "hello"
    assert item.attempts == 0
    assert item.research == [] and item.extracted == {}


async def test_duplicate_source_msg_is_rejected(db):
    """Backfill and restart replay must never create a second copy."""
    first = await db.insert_item(
        source="channel", source_chat_id=-100123, source_msg_id=7, raw_text="a"
    )
    dup = await db.insert_item(
        source="channel", source_chat_id=-100123, source_msg_id=7, raw_text="a"
    )
    assert first is not None
    assert dup is None


async def test_transition_writes_fields_and_event(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text="x")
    await db.transition(i, Status.TRIAGED, {"triage_score": 8, "triage_reason": "ok"})

    item = await db.get_item(i)
    assert item.status == Status.TRIAGED
    assert item.triage_score == 8

    events = await db.events_for(i)
    assert (events[-1]["from_status"], events[-1]["to_status"]) == ("ingested", "triaged")


async def test_transition_roundtrips_json_columns(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=2, raw_text="x")
    notes = [{"claim": "c", "sources": ["https://a.example/1"]}]
    await db.transition(i, Status.RESEARCHED, {"research": notes})
    assert (await db.get_item(i)).research == notes


async def test_transition_rejects_unknown_column(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=3, raw_text="x")
    with pytest.raises(KeyError):
        await db.transition(i, Status.TRIAGED, {"not_a_column": 1})


async def test_claim_items_respects_next_attempt_at(db):
    """A failing stage must back off, not spin."""
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=4, raw_text="x")
    await db.record_failure(i, "boom")
    assert await db.claim_items([Status.INGESTED]) == []

    await db.clear_backoff(i)
    claimed = await db.claim_items([Status.INGESTED])
    assert [c.id for c in claimed] == [i]


async def test_record_failure_fails_item_after_max_attempts(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=5, raw_text="x")
    for _ in range(2):
        await db.record_failure(i, "boom", max_attempts=3)
        assert (await db.get_item(i)).status == Status.INGESTED
    await db.record_failure(i, "boom", max_attempts=3)
    item = await db.get_item(i)
    assert item.status == Status.FAILED
    assert item.last_error == "boom"


async def test_record_failure_terminal_skips_retries(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=6, raw_text="x")
    await db.record_failure(i, "instagram 400", terminal=True)
    item = await db.get_item(i)
    assert item.status == Status.FAILED
    assert item.attempts == 1


async def test_transition_clears_prior_failure_state(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=7, raw_text="x")
    await db.record_failure(i, "transient")
    await db.transition(i, Status.TRIAGED)
    item = await db.get_item(i)
    assert item.attempts == 0 and item.last_error is None and item.next_attempt_at is None


async def test_seen_hash_recently_ignores_trivial_edits(db):
    await db.insert_item(
        source="channel", source_chat_id=-1, source_msg_id=1, raw_text="OpenAI blocks Cursor!"
    )
    assert await db.seen_hash_recently("openai   blocks cursor")
    assert not await db.seen_hash_recently("something entirely different")


async def test_max_source_msg_id_drives_backfill(db):
    for msg_id in (4, 9, 2):
        await db.insert_item(
            source="channel", source_chat_id=-100, source_msg_id=msg_id, raw_text="x"
        )
    assert await db.max_source_msg_id(-100) == 9
    assert await db.max_source_msg_id(-999) == 0


async def test_source_domains_dedupes_for_preview(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=8, raw_text="x")
    await db.transition(i, Status.RESEARCHED, {"research": [
        {"sources": ["https://teslarati.com/a", "https://teslarati.com/b"]},
        {"sources": ["https://livemint.com/c"]},
    ]})
    assert (await db.get_item(i)).source_domains == ["teslarati.com", "livemint.com"]
