import pytest

from pipeline.db import Database
from pipeline.models import Item, Status
from pipeline.stages.compose import compose


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


async def test_verification_json_roundtrip(db):
    """Verification is a JSON column, NULL default, holds dict | None."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=20, raw_text="x"
    )

    # Start with None (default)
    item = await db.get_item(i)
    assert item.verification is None

    # Write a dict
    verification_data = {
        "status": "partial",
        "coverage": {
            "chars_seen": 10,
            "chars_total": 20,
            "passes": 2,
            "passes_failed": 1,
        },
        "findings": [],
        "reason": "bad_output",
    }
    await db.transition(i, Status.SYNTHESIZED, {"verification": verification_data})

    # Read it back - should match
    item = await db.get_item(i)
    assert item.verification == verification_data


async def test_compose_output_verification_record_reaches_the_db_intact(
    db,
    fake_llm,
    settings,
):
    """T224: compose()'s return dict carries "verification" (built by
    verify.py) straight through db.transition into the real column -- the
    real nested shape (a "coverage" dict, a "findings" list), not just the
    flat fixture test_verification_json_roundtrip hand-builds above."""
    i = await db.insert_item(
        source="channel", source_chat_id=1, source_msg_id=99, raw_text="x"
    )

    fake_llm.queue(
        {
            "slides": [
                {"type": "hook", "headline": "Big news", "sub": "Why it matters."},
                {"type": "point", "headline": "Detail", "bullets": ["a", "b"]},
                {"type": "takeaway", "headline": "So what", "sub": "The consequence."},
            ],
            "caption": "A caption.",
            "hashtags": ["ai"],
        }
    )
    # No queue_verify() -- the verifier gets FakeLLM's own empty-queue
    # AssertionError and must come back as status="unavailable", not crash
    # compose() or the transition.
    item = Item(
        id=i,
        source="channel",
        status=Status.SYNTHESIZED,
        brief="THE BRIEF",
        research=[{"claim": "c", "sources": ["https://a.example/1"]}],
    )
    fields = await compose(item, fake_llm, settings)
    assert fields["verification"]["status"] == "unavailable"

    await db.transition(i, Status.COMPOSED, fields)

    stored = await db.get_item(i)
    assert stored.verification["status"] == "unavailable"
    assert stored.verification["coverage"]["chars_seen"] == 0, (
        "the failed pass saw nothing"
    )
    assert stored.verification["coverage"]["passes"] == 1
    assert stored.verification["findings"], (
        "the deck's claims still get a 'not_checked' record"
    )
    assert all(f["verdict"] == "not_checked" for f in stored.verification["findings"])
    assert stored.slides and stored.caption == fields["caption"]


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


async def test_recent_copy_ignores_trivial_edits(db):
    i = await db.insert_item(
        source="channel", source_chat_id=-1, source_msg_id=1,
        raw_text="OpenAI blocks Cursor from its models, citing terms!",
    )
    assert (await db.recent_copy("openai   blocks cursor from its models citing terms")).id == i
    assert await db.recent_copy("something entirely different, and long enough to count") is None


async def test_recent_copy_matches_a_channel_link_against_the_same_link_forwarded(db):
    """#790/#791: Telethon renders the channel's link as markdown, the forward is plain."""
    url = "https://thehackernews.com/2026/11/10-year-old-nginx-http2-flaw-lets.html"
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=1,
                             raw_text=f"A 10-year-old flaw in #NGINX.\n\nFull details ↓ [{url}]({url})")
    assert (await db.recent_copy(f"A 10-year-old flaw in #NGINX.\n\nFull details ↓ {url}")).id == i
    assert (await db.recent_copy("A 10-year-old flaw in #NGINX.\n\nFull details ↓ read more")) is None
    await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=2,
                         raw_text="A 10-year-old flaw in #NGINX.\n\nFull details ↓ [read more](https://x.example/a)")
    assert await db.recent_copy("A 10-year-old flaw in #NGINX.\n\nFull details ↓ read more")


async def test_recent_copy_skips_ignored_statuses_short_text_and_itself(db):
    text = "A long enough story about something that happened today"
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text=text)
    assert await db.recent_copy(text, exclude_id=i) is None
    await db.transition(i, Status.DROPPED)
    assert await db.recent_copy(text, ignore=(Status.DROPPED,)) is None
    assert (await db.recent_copy(text)).id == i
    await db.insert_item(source="dm", source_chat_id=1, source_msg_id=2, raw_text="short one")
    assert await db.recent_copy("short one") is None


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
    assert re.search(r"^\s*theme\s+TEXT,", prior, flags=re.MULTILINE) is None, (
        "prior schema should not declare theme column"
    )

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(prior)
        await conn.execute(
            "INSERT INTO items (source, source_chat_id, source_msg_id, created_at,"
            " status, status_updated_at) VALUES ('channel', -1, 1, '2026-01-01', 'ingested', '2026-01-01')"
        )
        await conn.commit()

    db = await Database(path).connect()
    try:
        rows = await db.conn.execute_fetchall("PRAGMA table_info(items)")
        assert "theme" in {r["name"] for r in rows}

        # And the pre-existing row survives, readable through the new mapping.
        item = await db.get_item(1)
        assert item is not None and item.theme is None
    finally:
        await db.close()


async def test_migration_adds_verification_to_an_existing_database(tmp_path):
    """The verification column is added by _migrate on first connect."""
    import re

    import aiosqlite

    from pipeline.db import SCHEMA

    prior = re.sub(r"^\s*verification\s+TEXT,\n", "", SCHEMA, flags=re.MULTILINE)
    assert "verification" not in prior, "prior schema should not declare verification"

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(prior)
        await conn.execute(
            "INSERT INTO items (source, source_chat_id, source_msg_id, created_at,"
            " status, status_updated_at) VALUES ('channel', -1, 1, '2026-01-01', 'ingested', '2026-01-01')"
        )
        await conn.commit()

    db = await Database(path).connect()
    try:
        rows = await db.conn.execute_fetchall("PRAGMA table_info(items)")
        col_names = {r["name"] for r in rows}
        assert "verification" in col_names

        # Pre-existing row has verification=None (NULL)
        item = await db.get_item(1)
        assert item is not None and item.verification is None

        # Run migration again - idempotent
        await db._migrate()
        item = await db.get_item(1)
        assert item is not None and item.verification is None
    finally:
        await db.close()


async def test_integrity_json_roundtrip(db):
    """Integrity is a JSON column, NULL default, holds dict | None."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=21, raw_text="x"
    )

    # Start with None (default)
    item = await db.get_item(i)
    assert item.integrity is None

    # Write a dict
    integrity_data = {
        "dropped": [{"slide_type": "point", "reason": "invalid_bullets"}],
        "urls_dropped": 2,
        "values_dropped": 1,
        "fail_open_notes": 0,
        "forced_post": False,
    }
    await db.transition(i, Status.COMPOSED, {"integrity": integrity_data})

    # Read it back - should match
    item = await db.get_item(i)
    assert item.integrity == integrity_data


async def test_integrity_none_stores_as_null_not_string(db):
    """Writing None to integrity must store SQL NULL, not the string "null"."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=22, raw_text="x"
    )

    # Explicitly write None
    await db.transition(i, Status.COMPOSED, {"integrity": None})

    # Check via raw SQL that it is NULL, not the string "null"
    rows = await db.conn.execute_fetchall(
        "SELECT integrity FROM items WHERE id=?", (i,)
    )
    assert rows[0]["integrity"] is None, "integrity should be SQL NULL, not a string"

    # Verify via the Item mapping
    item = await db.get_item(i)
    assert item.integrity is None


async def test_migration_adds_integrity_to_an_existing_database(tmp_path):
    """The integrity column is added by _migrate on first connect."""
    import re

    import aiosqlite

    from pipeline.db import SCHEMA

    prior = re.sub(r"^\s*integrity\s+TEXT,\n", "", SCHEMA, flags=re.MULTILINE)
    assert "integrity" not in prior, "prior schema should not declare integrity"

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(prior)
        await conn.execute(
            "INSERT INTO items (source, source_chat_id, source_msg_id, created_at,"
            " status, status_updated_at) VALUES ('channel', -1, 1, '2026-01-01', 'ingested', '2026-01-01')"
        )
        await conn.commit()

    db = await Database(path).connect()
    try:
        rows = await db.conn.execute_fetchall("PRAGMA table_info(items)")
        col_names = {r["name"] for r in rows}
        assert "integrity" in col_names

        # Pre-existing row has integrity=None (NULL)
        item = await db.get_item(1)
        assert item is not None and item.integrity is None

        # Run migration again - idempotent
        await db._migrate()
        item = await db.get_item(1)
        assert item is not None and item.integrity is None
    finally:
        await db.close()


async def test_integrity_survives_a_later_transition(db):
    """Integrity should not be clobbered by a later transition (not in the clear list)."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=23, raw_text="x"
    )

    integrity_data = {
        "dropped": [{"slide_type": "compare", "reason": "missing_rows"}],
        "urls_dropped": 0,
        "values_dropped": 3,
        "fail_open_notes": 1,
        "forced_post": True,
    }

    # First transition - write integrity
    await db.transition(i, Status.COMPOSED, {"integrity": integrity_data})
    item = await db.get_item(i)
    assert item.integrity == integrity_data

    # Second transition - doesn't touch integrity, should survive
    await db.transition(i, Status.RENDERED, {"rendered_paths": ["path1", "path2"]})
    item = await db.get_item(i)
    assert item.integrity == integrity_data, "integrity should survive transition"
    assert item.rendered_paths == ["path1", "path2"]


async def test_verification_none_via_transition_stores_sql_null(db):
    """Verification=None via transition stores SQL NULL, not "null" string."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=24, raw_text="x"
    )

    await db.transition(i, Status.SYNTHESIZED, {"verification": None})

    # Check via raw SQL that it is NULL
    rows = await db.conn.execute_fetchall(
        "SELECT verification FROM items WHERE id=?", (i,)
    )
    assert rows[0]["verification"] is None, (
        "verification should be SQL NULL, not 'null' string"
    )


async def test_integrity_none_via_transition_if_stores_sql_null(db):
    """Integrity=None via transition_if stores SQL NULL, not "null" string."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=25, raw_text="x"
    )

    await db.transition(i, Status.COMPOSED, {"integrity": {"test": "data"}})
    item = await db.get_item(i)

    # transition_if: clear integrity to None
    success = await db.transition_if(
        i, Status.COMPOSED, item.status_updated_at, Status.RENDERED, {"integrity": None}
    )
    assert success

    # Check via raw SQL that it is NULL
    rows = await db.conn.execute_fetchall(
        "SELECT integrity FROM items WHERE id=?", (i,)
    )
    assert rows[0]["integrity"] is None, (
        "integrity should be SQL NULL, not 'null' string"
    )


async def test_verification_none_via_update_fields_stores_sql_null(db):
    """Verification=None via update_fields stores SQL NULL, not "null" string."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=26, raw_text="x"
    )

    # First set it to something
    await db.transition(i, Status.SYNTHESIZED, {"verification": {"status": "ok"}})

    # Then update_fields to clear it
    await db.update_fields(i, {"verification": None})

    # Check via raw SQL that it is NULL
    rows = await db.conn.execute_fetchall(
        "SELECT verification FROM items WHERE id=?", (i,)
    )
    assert rows[0]["verification"] is None, (
        "verification should be SQL NULL, not 'null' string"
    )


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


async def test_lost_transition_if_does_not_discard_another_writers_uncommitted_update(
    db,
):
    """The worker and retention share one connection; losing the race must not roll back."""
    a = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=70, raw_text="a"
    )
    b = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=71, raw_text="b"
    )
    await db.conn.execute(
        "UPDATE items SET raw_text='mid-flight' WHERE id=?", (a,)
    )  # no commit
    won = await db.transition_if(
        b, Status.TRIAGED, None, Status.REJECTED
    )  # b is ingested: loses
    assert won is False
    await db.conn.commit()
    assert (await db.get_item(a)).raw_text == "mid-flight"
    assert (await db.get_item(b)).status is Status.INGESTED


@pytest.mark.parametrize(
    "column", ["integrity", "verification", "revision_request", "revision"]
)
async def test_corrupt_json_in_a_nullable_column_reads_back_as_none(db, column):
    """T264: a decode failure on a nullable JSON column is None (unknown), never [] and never a raise."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=99, raw_text="x"
    )
    await db.conn.execute(
        f"UPDATE items SET {column}=? WHERE id=?", ("{not valid json", i)
    )
    await db.conn.commit()
    item = await db.get_item(i)
    assert getattr(item, column) is None


# --- T294: agentic revision columns -----------------------------------------------

# column -> a representative non-NULL value (JSON columns hold dicts, theme_pin a str)
_T294_VALUES = {
    "revision_request": {"note": "try another angle", "requested_by": "operator"},
    "revision": {"v": 1, "runs": [{"status": "success"}, {"status": "failure"}]},
    "theme_pin": "dark_mode_theme",
}
_T294_COLUMNS = list(_T294_VALUES)


@pytest.mark.parametrize("column", _T294_COLUMNS)
async def test_t294_column_roundtrips_and_defaults_to_none(db, column):
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=27, raw_text="x"
    )
    assert getattr(await db.get_item(i), column) is None
    await db.transition(i, Status.COMPOSED, {column: _T294_VALUES[column]})
    assert getattr(await db.get_item(i), column) == _T294_VALUES[column]


@pytest.mark.parametrize("column", ["revision_request", "revision"])
async def test_t294_json_column_none_is_sql_null_not_the_string_null(db, column):
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=30, raw_text="x"
    )
    # Set a real value first so writing None has to overwrite something.
    await db.transition(i, Status.COMPOSED, {column: _T294_VALUES[column]})
    await db.transition(i, Status.RENDERED, {column: None})
    rows = await db.conn.execute_fetchall(
        f"SELECT {column} FROM items WHERE id=?", (i,)
    )
    assert rows[0][column] is None
    assert getattr(await db.get_item(i), column) is None


@pytest.mark.parametrize("column", _T294_COLUMNS)
async def test_t294_migration_adds_column_to_an_existing_database(tmp_path, column):
    import re

    import aiosqlite

    from pipeline.db import SCHEMA

    prior = re.sub(rf"^\s*{column}\s+TEXT,\n", "", SCHEMA, flags=re.MULTILINE)
    assert not re.search(rf"^\s*{column}\s", prior, flags=re.MULTILINE), (
        "prior schema must not declare the column"
    )

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(prior)
        await conn.execute(
            "INSERT INTO items (source, source_chat_id, source_msg_id, created_at,"
            " status, status_updated_at) VALUES ('channel', -1, 1, '2026-01-01', 'ingested', '2026-01-01')"
        )
        await conn.commit()

    db = await Database(path).connect()
    try:
        rows = await db.conn.execute_fetchall("PRAGMA table_info(items)")
        assert column in {r["name"] for r in rows}
        assert getattr(await db.get_item(1), column) is None

        await db._migrate()  # idempotent
        assert getattr(await db.get_item(1), column) is None
    finally:
        await db.close()


async def test_requeue_clears_revision_request_always(db):
    """revision_request is cleared on every requeue (in _ALWAYS_CLEARED)."""
    from pipeline.requeue import requeue

    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=33, raw_text="x"
    )
    value = _T294_VALUES["revision_request"]
    await db.transition(i, Status.COMPOSED, {"revision_request": value})
    assert (await db.get_item(i)).revision_request == value

    await requeue(db, i, Status.SYNTHESIZED, detail="test requeue")
    assert (await db.get_item(i)).revision_request is None


async def test_requeue_clears_revision_and_theme_pin_from_synthesized(db):
    """Requeuing from SYNTHESIZED clears revision and theme_pin (in STAGE_OUTPUTS)."""
    from pipeline.requeue import requeue

    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=34, raw_text="x"
    )
    await db.transition(
        i,
        Status.COMPOSED,
        {"revision": _T294_VALUES["revision"], "theme_pin": "dark"},
    )
    item = await db.get_item(i)
    assert item.revision == _T294_VALUES["revision"]
    assert item.theme_pin == "dark"

    await requeue(db, i, Status.SYNTHESIZED, detail="test requeue")
    item = await db.get_item(i)
    assert item.revision is None
    assert item.theme_pin is None


async def test_revision_and_theme_pin_survive_requeue_to_composed(db):
    """Requeues that don't target SYNTHESIZED must not clear revision/theme_pin."""
    from pipeline.requeue import requeue

    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=35, raw_text="x"
    )
    await db.transition(
        i,
        Status.COMPOSED,
        {"revision": _T294_VALUES["revision"], "theme_pin": "light"},
    )

    await requeue(db, i, Status.COMPOSED, detail="test requeue")
    item = await db.get_item(i)
    assert item.revision == _T294_VALUES["revision"]
    assert item.theme_pin == "light"


async def test_recent_copy_names_the_newest_copy(db):
    text = "A long enough story about something that happened today"
    await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text=text)
    newest = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=2, raw_text=text)
    assert (await db.recent_copy(text)).id == newest
