import pytest

from pipeline.errors import Recompose, Retryable, Retryforever, Terminal
from pipeline.models import Status
from pipeline.worker import Worker


async def _seed(db, status=Status.INGESTED, msg_id=1, **fields):
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=msg_id, raw_text="x"
    )
    if status != Status.INGESTED or fields:
        await db.transition(i, status, fields or None)
    return i


async def test_worker_advances_through_registered_stage(db):
    i = await _seed(db)
    stages = {Status.INGESTED: (lambda item: {"triage_score": 9}, Status.TRIAGED)}
    assert await Worker(db, stages).tick() == 1

    item = await db.get_item(i)
    assert item.status == Status.TRIAGED and item.triage_score == 9


async def test_worker_supports_async_handlers(db):
    i = await _seed(db)

    async def handler(item):
        return {"triage_score": 7}

    await Worker(db, {Status.INGESTED: (handler, Status.TRIAGED)}).tick()
    assert (await db.get_item(i)).triage_score == 7


async def test_handler_can_override_next_status(db):
    """Triage uses this to route low scores to DROPPED instead of TRIAGED."""
    i = await _seed(db)
    stages = {Status.INGESTED: (
        lambda item: {"triage_score": 2, "_next": Status.DROPPED}, Status.TRIAGED
    )}
    await Worker(db, stages).tick()
    assert (await db.get_item(i)).status == Status.DROPPED


async def test_worker_never_advances_awaiting_approval(db):
    """The human gate is enforced by the state machine, not by a check.

    Even handed a registry that wrongly contains AWAITING_APPROVAL, the worker
    must refuse to act on it.
    """
    i = await _seed(db, Status.AWAITING_APPROVAL, msg_id=2)
    sabotage = {Status.AWAITING_APPROVAL: (lambda item: {}, Status.PUBLISHED)}

    assert await Worker(db, sabotage).tick() == 0
    assert (await db.get_item(i)).status == Status.AWAITING_APPROVAL


async def test_terminal_error_fails_immediately_without_retrying(db):
    i = await _seed(db, msg_id=3)

    def boom(item):
        raise Terminal("instagram 400")

    await Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)}).tick()
    item = await db.get_item(i)
    assert item.status == Status.FAILED
    assert item.attempts == 1, "terminal errors must not consume three attempts"


async def test_retryable_error_backs_off_then_fails_after_max_attempts(db):
    i = await _seed(db, msg_id=4)

    def boom(item):
        raise Retryable("ollama gibberish")

    worker = Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)}, max_attempts=3)
    for _ in range(3):
        await db.clear_backoff(i)
        await worker.tick()

    assert (await db.get_item(i)).status == Status.FAILED


async def test_retryforever_defers_without_consuming_attempts(db):
    """Ollama being down must never eventually mark items failed."""
    i = await _seed(db, msg_id=5)

    def boom(item):
        raise Retryforever("ollama unreachable")

    worker = Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)}, max_attempts=3)
    for _ in range(5):
        await db.clear_backoff(i)
        await worker.tick()

    item = await db.get_item(i)
    assert item.status == Status.INGESTED
    assert item.attempts == 0


async def test_unclassified_exception_is_treated_as_retryable(db):
    i = await _seed(db, msg_id=6)

    def boom(item):
        raise ValueError("surprise")

    await Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)}).tick()
    item = await db.get_item(i)
    assert item.status == Status.INGESTED
    assert item.attempts == 1
    assert "ValueError" in item.last_error


async def test_recompose_routes_back_to_composed_with_a_note(db):
    i = await _seed(db, Status.RENDERED, msg_id=7, brief="THE BRIEF")

    def boom(item):
        raise Recompose(2, "bullets overflow")

    await Worker(db, {Status.RENDERED: (boom, Status.AWAITING_APPROVAL)}).tick()
    item = await db.get_item(i)
    assert item.status == Status.COMPOSED
    assert "Slide 3" in item.regen_note
    assert item.brief == "THE BRIEF", "research must survive a recompose"


async def test_failure_notification_fires_only_on_final_failure(db):
    i = await _seed(db, msg_id=8)
    alerted = []

    async def on_failure(item, reason):
        alerted.append((item.id, reason))

    def boom(item):
        raise Retryable("nope")

    worker = Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)},
                    max_attempts=2, on_failure=on_failure)
    await worker.tick()
    assert alerted == [], "no alert while retries remain"

    await db.clear_backoff(i)
    await worker.tick()
    assert len(alerted) == 1 and alerted[0][0] == i


async def test_tick_processes_a_batch_and_isolates_failures(db):
    good = await _seed(db, msg_id=10)
    bad = await _seed(db, msg_id=11)

    def handler(item):
        if item.id == bad:
            raise Retryable("only this one")
        return {"triage_score": 5}

    await Worker(db, {Status.INGESTED: (handler, Status.TRIAGED)}, batch=5).tick()

    assert (await db.get_item(good)).status == Status.TRIAGED
    assert (await db.get_item(bad)).status == Status.INGESTED
