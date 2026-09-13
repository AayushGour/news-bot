"""Asking the operator, and routing their answer back."""

from types import SimpleNamespace

import pytest

from pipeline.conversation import ask, handle_answer
from pipeline.models import WORKER_HALTS, Item, Status

OPERATOR = 424242


class FakeBot:
    """Hands back a distinct message_id per send, as Telegram does — a fake
    that returned a constant would make reply-routing look like it worked."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=1000 + len(self.sent))


def _message(text, user_id=OPERATOR, reply_to=None):
    return SimpleNamespace(
        text=text, caption=None,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id), message_id=9,
        reply_to_message=(SimpleNamespace(message_id=reply_to)
                          if reply_to is not None else None),
    )


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


# --- routing an answer when several items are waiting -----------------------
#
# handle_answer took list_by_status(limit=1), which is ORDER BY id — so with
# two items waiting the reply always went to the lower id, whatever question it
# answered. The other item stayed parked and its answer was consumed elsewhere.

async def _two_waiting(db, bot, settings):
    """Two items asking at once, each with its own question message."""
    ids = []
    for n, question in enumerate(("Which framework?", "Which timeframe?"), start=1):
        i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=n,
                                 raw_text=f"request {n}")
        await db.transition(i, Status.RESEARCHED)
        item = await db.get_item(i)
        await ask(db, bot, settings, item, question, Status.TRIAGED)
        ids.append(i)
    return ids


async def test_a_swipe_reply_answers_the_item_it_replied_to(db, settings):
    bot = FakeBot()
    first, second = await _two_waiting(db, bot, settings)

    second_item = await db.get_item(second)
    out = await handle_answer(
        _message("weekly", reply_to=second_item.question_msg_id),
        db, settings, bot)

    assert out == "answered"
    assert (await db.get_item(second)).answer == "weekly"
    assert (await db.get_item(first)).answer is None, "the other item is untouched"
    assert (await db.get_item(first)).status == Status.NEEDS_INPUT


async def test_the_lower_id_no_longer_eats_every_reply(db, settings):
    """The bug: ORDER BY id meant item 1 consumed an answer meant for item 2."""
    bot = FakeBot()
    first, second = await _two_waiting(db, bot, settings)

    first_item = await db.get_item(first)
    await handle_answer(
        _message("pytorch", reply_to=first_item.question_msg_id),
        db, settings, bot)

    assert (await db.get_item(first)).answer == "pytorch"
    assert (await db.get_item(second)).answer is None


async def test_an_id_prefix_routes_when_not_replying(db, settings):
    bot = FakeBot()
    first, second = await _two_waiting(db, bot, settings)

    out = await handle_answer(_message(f"{second}: weekly"), db, settings, bot)

    assert out == "answered"
    assert (await db.get_item(second)).answer == "weekly", "the prefix is stripped"
    assert (await db.get_item(first)).answer is None


async def test_an_ambiguous_reply_asks_instead_of_guessing(db, settings):
    bot = FakeBot()
    first, second = await _two_waiting(db, bot, settings)

    out = await handle_answer(_message("weekly"), db, settings, bot)

    assert out is None
    for i in (first, second):
        item = await db.get_item(i)
        assert item.answer is None and item.status == Status.NEEDS_INPUT
    assert "cannot tell which" in bot.sent[-1]
    assert f"item {first}" in bot.sent[-1] and f"item {second}" in bot.sent[-1]


async def test_one_waiting_item_still_takes_a_plain_reply(db, settings):
    """The common case must not need a swipe."""
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)

    assert await handle_answer(_message("this one"), db, settings, bot) == "answered"
    assert (await db.get_item(i)).answer == "this one"


async def test_a_reply_to_something_else_is_still_ambiguous(db, settings):
    """Replying to an unrelated message must not silently pick an item."""
    bot = FakeBot()
    await _two_waiting(db, bot, settings)

    out = await handle_answer(_message("weekly", reply_to=999999), db, settings, bot)
    assert out is None


async def test_the_question_message_id_is_recorded(db, settings):
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)

    assert (await db.get_item(i)).question_msg_id is not None


# --- the thread records both sides ------------------------------------------

async def test_asking_records_the_question_on_the_thread(db, settings):
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which framework?",
              Status.TRIAGED)

    thread = await db.messages_for(i)
    assert [(m["role"], m["text"]) for m in thread] == [
        ("pipeline", "Which framework?")]


async def test_answering_in_telegram_appends_to_the_same_thread(db, settings):
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)
    await handle_answer(_message("pytorch"), db, settings, bot)

    thread = await db.messages_for(i)
    assert [m["role"] for m in thread] == ["pipeline", "operator"]
    assert thread[-1]["surface"] == "telegram"


async def test_dropping_is_recorded_too(db, settings):
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)
    await handle_answer(_message("/drop"), db, settings, bot)

    assert (await db.messages_for(i))[-1]["text"] == "/drop"


# --- a question survives a Telegram outage ----------------------------------
#
# ask() sent first and recorded second, so a TelegramNetworkError lost the
# question from the thread AND escaped _run_one, taking the worker pass with
# it. The dashboard then showed an answer with no question above it.

class DeadBot(FakeBot):
    async def send_message(self, chat_id, text, **kw):
        raise ConnectionError("Server disconnected")


async def test_an_undeliverable_question_is_still_recorded(db, settings):
    bot = DeadBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)

    await ask(db, bot, settings, await db.get_item(i), "Which one?", Status.TRIAGED)

    assert (await db.get_item(i)).status == Status.NEEDS_INPUT
    assert [m["text"] for m in await db.messages_for(i)] == ["Which one?"]


async def test_a_send_failure_does_not_escape(db, settings):
    """The item is parked and the question stored, so delivery is survivable."""
    bot = DeadBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)

    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)
    assert (await db.get_item(i)).question_msg_id is None


async def test_the_question_is_recorded_before_the_answer(db, settings):
    """Ordering the thread wrongly read as the pipeline ignoring the reply."""
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)
    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED)
    await handle_answer(_message("this one"), db, settings, bot)

    assert [m["role"] for m in await db.messages_for(i)] == ["pipeline", "operator"]


async def test_carried_work_is_persisted_when_parking(db, settings):
    bot = FakeBot()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="request")
    await db.transition(i, Status.RESEARCHED)

    await ask(db, bot, settings, await db.get_item(i), "Which?", Status.TRIAGED,
              fields={"clauses": ["one ask", "another"],
                      "research": [{"claim": "c", "sources": ["u"]}]})

    item = await db.get_item(i)
    assert item.clauses == ["one ask", "another"]
    assert item.research, "answering resumes from work already paid for"
