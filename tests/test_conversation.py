"""Asking the operator, and routing their answer back."""

from types import SimpleNamespace

import pytest

from pipeline.conversation import ask, handle_answer
from pipeline.models import WORKER_HALTS, Item, Status

OPERATOR = 424242


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=1)


def _message(text, user_id=OPERATOR):
    return SimpleNamespace(text=text, caption=None,
                           from_user=SimpleNamespace(id=user_id),
                           chat=SimpleNamespace(id=user_id), message_id=9)


async def _waiting(db, question="Which framework did you mean?"):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="research the best vector databases")
    await db.transition(i, Status.TRIAGED)
    item = await db.get_item(i)
    return i, item


def test_needs_input_is_a_worker_halt():
    """An item waiting on an answer must not be advanced by anything except the
    operator's reply — the same reason awaiting_approval must not be."""
    assert Status.NEEDS_INPUT in WORKER_HALTS


async def test_ask_parks_the_item_and_messages_the_operator(db, settings):
    i, item = await _waiting(db)
    bot = FakeBot()

    await ask(db, bot, settings, item, "Which framework?", Status.TRIAGED, confidence=20)

    stored = await db.get_item(i)
    assert stored.status == Status.NEEDS_INPUT
    assert stored.question == "Which framework?"
    assert stored.resume_status == "triaged"
    assert stored.confidence == 20

    assert "Which framework?" in bot.sent[0]
    assert "confidence 20/100" in bot.sent[0]
    assert "/drop" in bot.sent[0]
    assert "vector databases" in bot.sent[0], "quote the request being asked about"


async def test_an_answer_resumes_the_stage_that_asked(db, settings):
    i, item = await _waiting(db)
    bot = FakeBot()
    await ask(db, bot, settings, item, "Which one?", Status.TRIAGED)

    action = await handle_answer(_message("the postgres one"), db, settings, bot)

    stored = await db.get_item(i)
    assert action == "answered"
    assert stored.status == Status.TRIAGED, "resumes where it asked, not from scratch"
    assert stored.answer == "the postgres one"
    assert stored.question is None, "the question is spent"


async def test_drop_discards_the_item(db, settings):
    i, item = await _waiting(db)
    bot = FakeBot()
    await ask(db, bot, settings, item, "Which one?", Status.TRIAGED)

    action = await handle_answer(_message("/drop"), db, settings, bot)

    assert action == "dropped"
    assert (await db.get_item(i)).status == Status.REJECTED


async def test_no_waiting_item_leaves_the_message_for_intake(db, settings):
    """Otherwise an ordinary request would be swallowed as an answer."""
    assert await handle_answer(_message("a brand new request"), db, settings) is None


async def test_an_empty_reply_is_not_treated_as_an_answer(db, settings):
    i, item = await _waiting(db)
    await ask(db, None, settings, item, "Which one?", Status.TRIAGED)

    assert await handle_answer(_message("   "), db, settings) is None
    assert (await db.get_item(i)).status == Status.NEEDS_INPUT, "still waiting"


async def test_the_worker_never_advances_a_waiting_item(db, settings):
    from pipeline.worker import Worker

    i, item = await _waiting(db)
    await ask(db, None, settings, item, "Which one?", Status.TRIAGED)

    sabotage = {Status.NEEDS_INPUT: (lambda it: {}, Status.RESEARCHED)}
    assert await Worker(db, sabotage).tick() == 0
    assert (await db.get_item(i)).status == Status.NEEDS_INPUT


async def test_worker_parks_the_item_when_a_stage_asks(db, settings):
    """The stage raises, the worker asks — a stage should not need a bot."""
    from pipeline.conversation import NeedsInput
    from pipeline.worker import Worker

    i, _ = await _waiting(db)
    bot = FakeBot()

    def needs_help(item):
        raise NeedsInput("Which framework?", Status.TRIAGED, confidence=12)

    worker = Worker(db, {Status.TRIAGED: (needs_help, Status.RESEARCHED)},
                    bot=bot, settings=settings)
    await worker.tick()

    stored = await db.get_item(i)
    assert stored.status == Status.NEEDS_INPUT
    assert stored.attempts == 0, "asking is not a failure and must not spend an attempt"
    assert "Which framework?" in bot.sent[0]
