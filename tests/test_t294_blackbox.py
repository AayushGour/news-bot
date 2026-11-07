"""T294 blackbox: agentic-revision columns via public Database / requeue APIs."""
import pytest

from pipeline.db import Database
from pipeline.models import Status
from pipeline.requeue import requeue

NEW_COLS = ("revision_request", "revision", "theme_pin")
REQ = {"note": "make it punchier", "scope": ["slide:2"]}
REV = {"v": 1, "runs": [{"id": 1, "ok": True}]}


@pytest.fixture
async def db(tmp_path):
    d = await Database(tmp_path / "t.db").connect()
    yield d
    await d.close()


async def _item(db):
    return await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text="x")


async def _raw(db, item_id, col):
    rows = await db.conn.execute_fetchall(
        f"SELECT {col} IS NULL AS n, {col} AS v FROM items WHERE id=?", (item_id,)
    )
    return rows[0]["n"], rows[0]["v"]


async def test_fresh_db_has_columns(db):
    cols = {r["name"] for r in await db.conn.execute_fetchall("PRAGMA table_info(items)")}
    assert set(NEW_COLS) <= cols


async def test_new_item_defaults_none(db):
    item = await db.get_item(await _item(db))
    assert (item.revision_request, item.revision, item.theme_pin) == (None, None, None)


@pytest.mark.parametrize("col", ["revision_request", "revision"])
async def test_none_is_sql_null_via_update(db, col):
    i = await _item(db)
    await db.update_fields(i, {col: None})
    assert (await _raw(db, i, col))[0] == 1
    assert getattr(await db.get_item(i), col) is None


@pytest.mark.parametrize("col", ["revision_request", "revision"])
async def test_none_is_sql_null_via_transition(db, col):
    i = await _item(db)
    await db.transition(i, Status.TRIAGED, {col: {"a": 1}})
    await db.transition(i, Status.EXTRACTED, {col: None})
    isnull, raw = await _raw(db, i, col)
    assert isnull == 1 and raw is None


async def test_roundtrip(db):
    i = await _item(db)
    await db.update_fields(
        i, {"revision_request": REQ, "revision": REV, "theme_pin": "midnight"}
    )
    item = await db.get_item(i)
    assert item.revision_request == REQ
    assert item.revision == REV
    assert item.theme_pin == "midnight"
    assert (await _raw(db, i, "theme_pin"))[1] == "midnight"  # plain text, not JSON


@pytest.mark.parametrize(
    "target", [Status.INGESTED, Status.EXTRACTED, Status.TRIAGED, Status.RESEARCHED, Status.SYNTHESIZED]
)
async def test_requeue_early_clears_all(db, target):
    i = await _item(db)
    await db.update_fields(i, {"revision_request": REQ, "revision": REV, "theme_pin": "x"})
    await requeue(db, i, target)
    item = await db.get_item(i)
    assert (item.revision_request, item.revision, item.theme_pin) == (None, None, None)
    for col in NEW_COLS:
        assert (await _raw(db, i, col))[0] == 1


@pytest.mark.parametrize("target", [Status.COMPOSED, Status.RENDERED, Status.APPROVED])
async def test_requeue_late_keeps_revision_and_pin(db, target):
    i = await _item(db)
    await db.update_fields(i, {"revision_request": REQ, "revision": REV, "theme_pin": "x"})
    await requeue(db, i, target)
    item = await db.get_item(i)
    assert item.revision_request is None
    assert item.revision == REV
    assert item.theme_pin == "x"
