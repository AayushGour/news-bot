"""A requeue must never erase the fact that something is already on Instagram.

Item 84 published, was requeued from the dashboard, and its row then claimed it
had never been posted — while the carousel sat on a real public account. The
next approval would have been a duplicate with nothing on screen to warn about
it. `ig_post_id` cannot carry that record: publish_carousel returns early when
it is set, so a requeue has to clear it or a genuine redo silently republishes
nothing. `publish_log` is the record that survives.
"""


from pipeline.db import _ITEM_FIELDS, JSON_COLUMNS, Database
from pipeline.models import Status
from pipeline.requeue import STAGE_OUTPUTS, fields_to_clear, requeue


def test_publish_log_is_not_cleared_by_any_requeue_target():
    """The guarantee, stated over every stage rather than the one that bit."""
    for target, _ in STAGE_OUTPUTS:
        assert "publish_log" not in fields_to_clear(target), target


def test_no_stage_declares_publish_log_as_its_output():
    """Adding it to a stage's outputs would silently re-open the hole."""
    for _, outputs in STAGE_OUTPUTS:
        assert "publish_log" not in outputs


def test_requeue_still_clears_ig_post_id_so_a_redo_can_publish():
    """The counterpart: keeping ig_post_id would make publish_carousel return
    the OLD post id and never upload the new deck."""
    assert fields_to_clear(Status.PUBLISHING)["ig_post_id"] is None


def test_publish_log_round_trips_as_json():
    assert "publish_log" in JSON_COLUMNS
    assert "publish_log" in _ITEM_FIELDS


async def test_history_survives_a_real_requeue_end_to_end(tmp_path):
    db = await Database(tmp_path / "t.db").connect()
    item_id = await db.insert_item(source="dm", source_chat_id=1,
                                   source_msg_id=1, raw_text="x")
    await db.update_fields(item_id, {
        "ig_post_id": "18092835962538597",
        "publish_log": [{"ig_post_id": "18092835962538597", "at": "2026-09-13T10:30:49+00:00"}],
    })
    await db.transition(item_id, Status.PUBLISHED, {})

    await requeue(db, item_id, Status.INGESTED, detail="dashboard requeue")

    item = await db.get_item(item_id)
    assert item.status == Status.INGESTED.value
    assert item.ig_post_id is None, "guard must clear so a redo can publish"
    assert item.publish_log == [
        {"ig_post_id": "18092835962538597", "at": "2026-09-13T10:30:49+00:00"}
    ], "the record that it is live must survive"
    await db.close()


async def test_a_second_publication_appends_rather_than_replaces(tmp_path):
    from pipeline.publish.instagram import _record_publication

    db = await Database(tmp_path / "t.db").connect()
    item_id = await db.insert_item(source="dm", source_chat_id=1,
                                   source_msg_id=2, raw_text="x")
    item = await db.get_item(item_id)
    await _record_publication(db, item, "first")
    item = await db.get_item(item_id)
    await _record_publication(db, item, "second")

    item = await db.get_item(item_id)
    assert [e["ig_post_id"] for e in item.publish_log] == ["first", "second"]
    assert item.ig_post_id == "second"
    await db.close()
