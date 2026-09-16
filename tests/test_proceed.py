"""/post sends what has already been gathered on for approval.

Three phrasings were offered across the questions this pipeline asks — "/skip"
from the research gate, "post what you have" from both enumeration gates — and
none were implemented. The handler had two branches: /drop, or store the text
as the answer and retry. So "post what you have" was stored as the answer and
fed back as the search subject, and the item hunted for pages about "post what
have".

The second attempt was worse in a quieter way: it resumed at the stage that
parked the item, which re-ran the search. Item 116 was parked holding eight
notes, re-searched, came back with none, and died at synthesize with "cannot
synthesize with no research notes". Resuming *after* research is the only way
"what I have" means what it says.
"""

import pytest

from pipeline.conversation import POST, PROCEED, handle_answer, is_proceed
from pipeline.models import Status
from pipeline.stages.enumerate_items import subject_terms

NOTES = [{"claim": "a", "detail": "b"}, {"claim": "c", "detail": "d"}]


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.reply_to_message = None


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)


async def _park(db, research=None, msg_id=1):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=msg_id,
                             raw_text="top 5 repos for X")
    await db.transition(i, Status.NEEDS_INPUT, {
        "question": "nothing answers this",
        "resume_status": Status.TRIAGED.value,
        "research": NOTES if research is None else research,
    })
    return i


# ----------------------------------------------------------- recognition


@pytest.mark.parametrize("text", sorted(PROCEED))
def test_every_offered_command_is_recognised(text):
    assert is_proceed(text)


@pytest.mark.parametrize("text", ["/POST", "  /post  ", "/Post"])
def test_recognition_ignores_case_and_surrounding_space(text):
    assert is_proceed(text)


@pytest.mark.parametrize("text", [
    "post what you have",               # prose is no longer a command
    "post about the funding round",     # a real answer that contains "post"
    "/post the thing", "/drop", "", "   ",
])
def test_prose_is_never_mistaken_for_the_command(text):
    """A command, not a phrase: prose matching meant a genuine answer
    containing those words was swallowed instead of used."""
    assert not is_proceed(text)


def test_the_old_phrase_would_have_poisoned_the_search():
    """Why storing it as the answer was not merely useless: enumeration takes
    its search terms from item.answer."""
    assert subject_terms("post what you have") == ["post", "what", "have"]


# ------------------------------------------------------------- behaviour


async def test_post_resumes_after_research_so_the_notes_survive(db, settings):
    """The bug that killed item 116: resuming where it was parked re-runs the
    search, which is a fresh attempt, not "what I have"."""
    i = await _park(db)
    bot = FakeBot()
    action = await handle_answer(FakeMessage(POST), db, settings, bot)

    assert action == "posting"
    item = await db.get_item(i)
    assert item.status == Status.RESEARCHED.value, "must skip re-research"
    assert item.research == NOTES, "the notes must be exactly what was gathered"
    assert not item.answer, "storing it makes it the next search subject"
    assert any("approval" in m for m in bot.sent)


async def test_post_says_it_is_going_for_approval_not_publishing(db, settings):
    """It builds a deck and sends it to the approval gate. Wording that
    implied publishing would be a lie about a real account."""
    await _park(db)
    bot = FakeBot()
    await handle_answer(FakeMessage(POST), db, settings, bot)
    said = " ".join(bot.sent).lower()
    assert "approval" in said
    assert "published" not in said


async def test_post_with_nothing_gathered_is_refused_and_stays_parked(db, settings):
    """Offering an option that cannot work is worse than not offering it."""
    i = await _park(db, research=[])
    bot = FakeBot()
    action = await handle_answer(FakeMessage(POST), db, settings, bot)

    assert action == "nothing-to-post"
    assert (await db.get_item(i)).status == Status.NEEDS_INPUT.value
    assert any("nothing to send" in m for m in bot.sent)


async def test_skip_still_works_because_it_was_offered_first(db, settings):
    i = await _park(db)
    assert await handle_answer(FakeMessage("/skip"), db, settings, FakeBot()) == "posting"
    assert (await db.get_item(i)).status == Status.RESEARCHED.value


async def test_an_ordinary_answer_still_retries_from_where_it_parked(db, settings):
    i = await _park(db)
    action = await handle_answer(
        FakeMessage("look at the MCP spec repo"), db, settings, FakeBot())

    assert action == "answered"
    item = await db.get_item(i)
    assert item.answer == "look at the MCP spec repo"
    assert item.status == Status.TRIAGED.value, "an answer is meant to be searched"


async def test_drop_still_wins(db, settings):
    i = await _park(db)
    assert await handle_answer(FakeMessage("/drop"), db, settings, FakeBot()) == "dropped"
    assert (await db.get_item(i)).status == Status.REJECTED.value


def test_every_question_offers_only_commands_that_exist():
    """A question that offers an option the handler does not implement is how
    this broke: three phrasings were promised and none were honoured. Any
    slash-command mentioned in a question must be one handle_answer accepts."""
    import re
    from pathlib import Path

    from pipeline.conversation import DROP, is_proceed

    src = Path(__file__).resolve().parents[1] / "src" / "pipeline"
    offered = set()
    for path in src.rglob("*.py"):
        for line in path.read_text().splitlines():
            if "/drop" in line or "/post" in line or "/skip" in line:
                offered |= set(re.findall(r"/(?:drop|post|skip)\b", line))

    assert offered, "expected the questions to offer something"
    for command in offered:
        assert command == DROP or is_proceed(command), \
            f"{command} is offered to the operator but nothing handles it"
