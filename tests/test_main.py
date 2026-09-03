"""Registry completeness and digest.

The registry test is the one that stops items disappearing forever: a status
with no registered stage is claimed by nobody and silently sits until someone
notices weeks later.
"""

from types import SimpleNamespace

import pytest

from pipeline.__main__ import build_stage_registry, missing_statuses
from pipeline.digest import build_digest, make_failure_notifier, send_digest
from pipeline.models import WORKER_HALTS, Status


def _registry(db, settings):
    return build_stage_registry(
        db=db, llm=object(), http=object(), settings=settings, bot=object()
    )


async def test_registry_covers_every_worker_status(db, settings):
    registry = _registry(db, settings)
    assert missing_statuses(registry) == set(), "a status with no stage strands items"


async def test_registry_excludes_the_human_gate(db, settings):
    """Only the approval bot may move an item out of AWAITING_APPROVAL."""
    assert Status.AWAITING_APPROVAL not in _registry(db, settings)


async def test_registry_excludes_all_terminal_statuses(db, settings):
    registry = _registry(db, settings)
    for status in WORKER_HALTS:
        assert status not in registry


async def test_registry_next_statuses_form_the_expected_chain(db, settings):
    registry = _registry(db, settings)
    chain = {status: nxt for status, (_, nxt) in registry.items()}

    # Extraction precedes triage so triage can read image content.
    assert chain[Status.INGESTED] == Status.EXTRACTED
    assert chain[Status.EXTRACTED] == Status.TRIAGED
    assert chain[Status.TRIAGED] == Status.RESEARCHED
    assert chain[Status.RESEARCHED] == Status.SYNTHESIZED
    assert chain[Status.SYNTHESIZED] == Status.COMPOSED
    assert chain[Status.COMPOSED] == Status.RENDERED
    assert chain[Status.RENDERED] == Status.AWAITING_APPROVAL
    assert chain[Status.APPROVED] == Status.PUBLISHING
    assert chain[Status.PUBLISHING] == Status.PUBLISHED


def test_missing_statuses_detects_a_gap():
    assert Status.COMPOSED in missing_statuses({Status.INGESTED: (None, None)})


# --------------------------------------------------------------------- digest


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=1)


async def _seed(db, status, msg_id, **fields):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=msg_id, raw_text="some news text")
    if status != Status.INGESTED:
        await db.transition(i, status, fields or None)
    return i


async def test_digest_counts_each_outcome(db, settings):
    await _seed(db, Status.DROPPED, 1, triage_reason="no verifiable claim")
    await _seed(db, Status.DROPPED, 2, triage_reason="near-duplicate")
    await _seed(db, Status.AWAITING_APPROVAL, 3)
    await _seed(db, Status.FAILED, 4)

    text = await build_digest(db)

    assert "seen: 4" in text
    assert "dropped at triage: 2" in text
    assert "failed: 1" in text
    assert "awaiting your approval: 1" in text


async def test_digest_lists_drop_reasons(db, settings):
    """Without this, a mis-tuned threshold looks identical to a quiet channel."""
    await _seed(db, Status.DROPPED, 1, triage_reason="no verifiable claim")
    assert "no verifiable claim" in await build_digest(db)


async def test_digest_is_sent_to_the_operator(db, settings):
    bot = FakeBot()
    await send_digest(bot, db, settings)
    assert len(bot.sent) == 1


async def test_failure_notifier_includes_reason_and_source_text(db, settings):
    bot = FakeBot()
    notify = make_failure_notifier(bot, settings)
    i = await _seed(db, Status.FAILED, 9)

    await notify(await db.get_item(i), "Retryable: ollama gibberish")

    assert "ollama gibberish" in bot.sent[0]
    assert "some news text" in bot.sent[0]
