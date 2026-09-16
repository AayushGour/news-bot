"""“Post what you have” has to actually post what we have.

Three phrasings were offered to the operator across the questions this
pipeline asks — "/skip" from the research gate, "post what you have" from both
enumeration gates — and none of them were implemented. The handler had exactly
two branches: /drop, or store the text as the answer and retry. So answering
"post what you have" stored that as the answer and fed it back in as the
search subject, and the item went looking for pages about "post what have".
"""

import pytest

from pipeline.conversation import PROCEED, handle_answer, is_proceed
from pipeline.models import Status
from pipeline.requeue import fields_to_clear
from pipeline.stages.enumerate_items import subject_terms


class FakeMessage:
    def __init__(self, text, reply_to=None):
        self.text = text
        self.reply_to_message = reply_to


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)


async def _park(db, question="nothing answers this"):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1,
                             raw_text="top 5 repos for X")
    await db.transition(i, Status.NEEDS_INPUT, {
        "question": question, "resume_status": Status.TRIAGED.value,
        "research": [{"claim": "a", "detail": "b"}],
    })
    return i


# ----------------------------------------------------------- recognition


@pytest.mark.parametrize("text", sorted(PROCEED))
def test_every_promised_phrasing_is_recognised(text):
    assert is_proceed(text)


@pytest.mark.parametrize("text", [
    "POST WHAT YOU HAVE", "  post what you have  ", "Post what you have.",
    "post  what   you  have",
])
def test_recognition_survives_case_spacing_and_punctuation(text):
    assert is_proceed(text)


@pytest.mark.parametrize("text", [
    "try searching for MCP servers instead",
    "post about the funding round",     # a real instruction containing "post"
    "/drop", "", "   ",
])
def test_a_real_answer_is_not_mistaken_for_proceed(text):
    assert not is_proceed(text)


def test_the_phrase_would_have_poisoned_the_search():
    """Why storing it as the answer was not merely useless: enumeration takes
    its subject terms from item.answer."""
    assert subject_terms("post what you have") == ["post", "what", "have"]


# ------------------------------------------------------------- behaviour


async def test_proceed_resumes_without_storing_an_answer(db, settings):
    i = await _park(db)
    bot = FakeBot()
    action = await handle_answer(FakeMessage("post what you have"), db, settings, bot)

    assert action == "proceeding"
    item = await db.get_item(i)
    assert item.status == Status.TRIAGED.value
    assert item.proceed_anyway is True
    assert not item.answer, "storing it makes it the next search subject"
    assert any("what I already have" in m for m in bot.sent)


async def test_an_ordinary_answer_still_becomes_the_answer(db, settings):
    i = await _park(db)
    action = await handle_answer(
        FakeMessage("look at the MCP spec repo"), db, settings, FakeBot())

    assert action == "answered"
    item = await db.get_item(i)
    assert item.answer == "look at the MCP spec repo"
    assert item.proceed_anyway is False


async def test_drop_still_wins(db, settings):
    i = await _park(db)
    assert await handle_answer(FakeMessage("/drop"), db, settings, FakeBot()) == "dropped"
    assert (await db.get_item(i)).status == Status.REJECTED.value


async def test_the_research_already_gathered_survives(db, settings):
    """Proceeding is only useful if what was found is still there."""
    i = await _park(db)
    await handle_answer(FakeMessage("/skip"), db, settings, FakeBot())
    assert (await db.get_item(i)).research == [{"claim": "a", "detail": "b"}]


def test_a_requeue_clears_the_override():
    """A fresh attempt must not keep waving the gate through on new material."""
    assert fields_to_clear(Status.TRIAGED)["proceed_anyway"] == 0
