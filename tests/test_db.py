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


async def test_migration_adds_theme_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS skips existing tables, so a column added
    later never appears on a live database without an explicit migration.

    Builds the real prior schema — everything except `theme` — rather than a
    toy table, so this exercises the actual upgrade path.
    """
    import re

    import aiosqlite

    from pipeline.db import SCHEMA

    prior = re.sub(r"^\s*theme\s+TEXT,\n", "", SCHEMA, flags=re.MULTILINE)
    assert "theme" not in prior, "prior schema should not declare theme"

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(prior)
        await conn.execute(
            "INSERT INTO items (source, source_chat_id, source_msg_id, created_at,"
            " status, status_updated_at) VALUES ('channel', -1, 1, '2026-01-01', 'ingested', '2026-01-01')"
        )
        await conn.commit()

    db = await Database(path).connect()
    rows = await db.conn.execute_fetchall("PRAGMA table_info(items)")
    assert "theme" in {r["name"] for r in rows}

    # And the pre-existing row survives, readable through the new mapping.
    item = await db.get_item(1)
    assert item is not None and item.theme is None
    await db.close()


# --- one conversation per item, whichever surface it arrived on --------------

async def test_messages_come_back_in_order(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.add_message(i, "pipeline", "Which framework?", "telegram")
    await db.add_message(i, "operator", "pytorch", "dashboard")

    thread = await db.messages_for(i)
    assert [(m["role"], m["text"]) for m in thread] == [
        ("pipeline", "Which framework?"), ("operator", "pytorch")]


def _other(db):
    return db.insert_item(source="dm", source_chat_id=1, source_msg_id=2,
                          raw_text="other")


async def test_threads_do_not_leak_between_items(db):
    first = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                                 raw_text="request")
    second = await _other(db)
    await db.add_message(first, "operator", "for the first", "dashboard")

    assert len(await db.messages_for(first)) == 1
    assert await db.messages_for(second) == []


async def test_a_telegram_question_and_a_dashboard_answer_share_one_thread(db):
    """The same exchange, so splitting by surface would show each side half."""
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.add_message(i, "pipeline", "Which timeframe?", "telegram")
    await db.add_message(i, "operator", "weekly", "dashboard")

    surfaces = {m["surface"] for m in await db.messages_for(i)}
    assert surfaces == {"telegram", "dashboard"}
    assert len(await db.messages_for(i)) == 2


async def test_status_counts_groups_the_queue(db):
    for n, status in enumerate((Status.AWAITING_APPROVAL, Status.AWAITING_APPROVAL,
                                Status.FAILED), start=1):
        i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=n,
                                 raw_text="x")
        await db.transition(i, status)

    counts = await db.status_counts()
    assert counts[Status.AWAITING_APPROVAL.value] == 2
    assert counts[Status.FAILED.value] == 1


async def test_status_updated_at_is_readable_from_the_item(db):
    """A real column that was missing from the dataclass, so anything reading
    it off an Item rather than straight out of SQL raised AttributeError."""
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    assert (await db.get_item(i)).status_updated_at

    await db.transition(i, Status.TRIAGED)
    assert (await db.get_item(i)).status_updated_at


# --- priority ---------------------------------------------------------------
#
# The queue was strictly ORDER BY id, so an urgent request sat behind every
# older one. Priority defaults to 0 so an untouched queue is unchanged.

async def _queued(db, n):
    ids = []
    for k in range(n):
        i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=k,
                                 raw_text=f"request {k}")
        ids.append(i)
    return ids


CLAIMABLE = (Status.INGESTED,)


async def test_an_untouched_queue_is_still_oldest_first(db):
    ids = await _queued(db, 4)
    assert [i.id for i in await db.claim_items(CLAIMABLE, limit=10)] == ids


async def test_a_bumped_item_jumps_the_whole_line(db):
    ids = await _queued(db, 4)
    await db.set_priority(ids[-1], 1)

    order = [i.id for i in await db.claim_items(CLAIMABLE, limit=10)]
    assert order[0] == ids[-1]
    assert order[1:] == ids[:-1], "everything else keeps its order"


async def test_a_lowered_item_goes_to_the_back(db):
    ids = await _queued(db, 4)
    await db.set_priority(ids[0], -1)

    order = [i.id for i in await db.claim_items(CLAIMABLE, limit=10)]
    assert order[-1] == ids[0]


async def test_higher_priority_wins_and_ties_break_by_age(db):
    ids = await _queued(db, 5)
    await db.set_priority(ids[3], 5)
    await db.set_priority(ids[4], 2)
    await db.set_priority(ids[1], 2)

    order = [i.id for i in await db.claim_items(CLAIMABLE, limit=10)]
    assert order[0] == ids[3]
    assert order[1:3] == [ids[1], ids[4]], "same priority, older first"


async def test_priority_survives_a_reload(db):
    ids = await _queued(db, 2)
    await db.set_priority(ids[0], 3)
    assert (await db.get_item(ids[0])).priority == 3


async def test_priority_does_not_override_backoff(db):
    """A bumped item that is failing must not be retried in a tight loop."""
    ids = await _queued(db, 2)
    await db.set_priority(ids[0], 9)
    await db.defer(ids[0], 3600, "provider down")

    assert [i.id for i in await db.claim_items(CLAIMABLE, limit=10)] == [ids[1]]


async def test_priority_does_not_resurrect_a_halted_item(db):
    """Bumping something awaiting approval must not push it past the human."""
    ids = await _queued(db, 2)
    await db.transition(ids[0], Status.AWAITING_APPROVAL)
    await db.set_priority(ids[0], 9)

    assert [i.id for i in await db.claim_items(CLAIMABLE, limit=10)] == [ids[1]]
