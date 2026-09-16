"""Asking the operator when the pipeline cannot proceed on its own.

The pipeline's default is to keep going and produce something. That is wrong
when the research is thin: a deck assembled from nothing is worse than no deck,
because it costs the operator a review and looks like a system working. So a
stage that cannot do its job honestly stops and asks.

An item in ``NEEDS_INPUT`` is halted for the worker exactly like
``AWAITING_APPROVAL``. Only the operator's reply moves it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .db import Database
from .models import Item, Status

log = logging.getLogger(__name__)


class NeedsInput(Exception):
    """A stage cannot continue without an answer from the operator.

    Carries the question and the status to resume from, so the reply re-enters
    the pipeline at the stage that asked rather than restarting the item.
    """

    def __init__(self, question: str, resume_status: Status,
                 confidence: int | None = None, fields: dict | None = None):
        super().__init__(question)
        self.question = question
        self.resume_status = resume_status
        self.confidence = confidence
        #: Work already paid for — research, clauses, extraction. Raising
        #: discarded all of it, so answering re-ran every search and fetch, and
        #: a parked item could not be inspected to see what parked it.
        self.fields = dict(fields or {})


async def ask(
    db: Database, bot: Any, settings: Any, item: Item,
    question: str, resume_status: Status, confidence: int | None = None,
    fields: dict | None = None,
) -> None:
    """Park the item and put the question to the operator."""
    await db.transition(
        item.id, Status.NEEDS_INPUT,
        {**(fields or {}),
         "question": question, "resume_status": str(resume_status),
         "confidence": confidence, "answer": None},
    )
    if bot is None:
        return

    head = f"❓ Item {item.id} needs your input"
    if confidence is not None:
        head += f"  (confidence {confidence}/100)"
    source = (item.raw_text or "").strip().replace("\n", " ")

    # Recorded before the send. The question exists whether or not Telegram
    # accepts it, and recording afterwards meant a network error lost it from
    # the thread entirely — the dashboard then showed an answer with no
    # question above it, reading as though the pipeline had ignored the reply.
    await db.add_message(item.id, "pipeline", question, "telegram")

    try:
        sent = await bot.send_message(
            chat_id=settings.operator_user_id,
            text=(
                f"{head}\n\n"
                f"“{source}”\n\n"
                f"{question}\n\n"
                f"Swipe-reply to THIS message to answer, or /drop to discard it."
            ),
        )
    except Exception as exc:
        # The item is already parked and the question is already stored, so a
        # delivery failure is survivable: it is visible in the dashboard. Left
        # to escape it took the whole worker pass down with it.
        log.warning("item %s question could not be delivered: %s", item.id, exc)
        return

    # Recorded so a swipe-reply identifies the item. Without it an answer is
    # matched by nothing at all, and the oldest waiting item consumes every
    # reply regardless of which question it answers.
    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        await db.update_fields(item.id, {"question_msg_id": int(message_id)})
    log.info("item %s asked the operator: %s", item.id, question[:80])


DROP = "/drop"

#: Answering with any of these means "stop asking and use what you already
#: found". Three different phrasings were promised to the operator across the
#: questions this pipeline asks — "/skip" from the research gate, "post what
#: you have" from both enumeration gates — and none of them were implemented.
#: The text was simply stored as the answer and fed back in as a search term,
#: so "post what you have" re-researched the subject "post what have". All the
#: promised spellings are honoured here, plus the obvious variants, because an
#: operator who was offered one of them should not have to guess.
PROCEED = frozenset({
    "/skip", "/post", "skip",
    "post what you have", "post what i have", "post what you've got",
    "post what youve got", "post it", "post anyway", "publish anyway",
    "go ahead", "use what you have", "use what you've got",
})


def is_proceed(text: str) -> bool:
    """Is this answer "continue with what you already have"?"""
    cleaned = (text or "").strip().strip(".!").lower()
    cleaned = " ".join(cleaned.split())
    return cleaned in PROCEED

_ID_PREFIX = re.compile(r"^\s*#?(\d{1,6})\s*[:.)-]\s*")


def _strip_id_prefix(text: str, item_id: int) -> str:
    """Remove a leading "45:" once it has done its routing job."""
    match = _ID_PREFIX.match(text)
    if match and int(match.group(1)) == item_id:
        return text[match.end():].strip() or text
    return text


def _addressed_item(message: Any, text: str, waiting: list[Item]) -> Item | None:
    """Which waiting item is this reply for?

    In order: the message it was sent as a reply to, an explicit id prefix,
    or — only when exactly one item is waiting — that one. Returning None
    means the caller must ask rather than guess.
    """
    replied = getattr(getattr(message, "reply_to_message", None), "message_id", None)
    if replied is not None:
        for item in waiting:
            if item.question_msg_id == replied:
                return item

    match = _ID_PREFIX.match(text)
    if match:
        wanted = int(match.group(1))
        for item in waiting:
            if item.id == wanted:
                return item

    return waiting[0] if len(waiting) == 1 else None


async def handle_answer(
    message: Any, db: Database, settings: Any, bot: Any = None
) -> str | None:
    """Route an operator reply to whichever item is waiting for one.

    Returns the action taken, or None if no item was waiting — in which case
    the message is ordinary intake and the caller should handle it.
    """
    waiting = await db.list_by_status(Status.NEEDS_INPUT, limit=20)
    if not waiting:
        return None

    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return None

    item = _addressed_item(message, text, waiting)
    if item is None:
        # Several items are waiting and nothing says which this answers.
        # Picking by id order is what silently fed one item's answer to
        # another, so the ambiguity goes back to the operator instead.
        if bot:
            listing = "\n".join(
                f"  · item {w.id}: {(w.question or '').splitlines()[0]}"
                for w in waiting
            )
            await bot.send_message(
                chat_id=settings.operator_user_id,
                text=("I have several items waiting and cannot tell which that "
                      f"answers:\n\n{listing}\n\nSwipe-reply to the question "
                      "you mean, or start your message with the item number "
                      "(\"45: ...\")."),
            )
        return None

    text = _strip_id_prefix(text, item.id)

    if text.lower() == DROP:
        await db.add_message(item.id, "operator", DROP, "telegram")
        await db.transition(item.id, Status.REJECTED, {"answer": DROP})
        if bot:
            await bot.send_message(
                chat_id=settings.operator_user_id,
                text=f"Dropped item {item.id}.",
            )
        return "dropped"

    resume = Status(item.resume_status or Status.TRIAGED)

    if is_proceed(text):
        # Resume WITHOUT storing this as the answer. Stored, it becomes the
        # subject of the next search — which is exactly how "post what you
        # have" turned into a hunt for pages about "post what have".
        await db.add_message(item.id, "operator", text, "telegram")
        await db.transition(item.id, resume, {
            "answer": None, "question": None, "proceed_anyway": 1,
        })
        if bot:
            await bot.send_message(
                chat_id=settings.operator_user_id,
                text=f"Posting item {item.id} with what I already have.",
            )
        log.info("item %s: proceeding with existing material from %s",
                 item.id, resume)
        return "proceeding"

    await db.add_message(item.id, "operator", text, "telegram")
    await db.transition(item.id, resume, {"answer": text, "question": None})
    if bot:
        await bot.send_message(
            chat_id=settings.operator_user_id,
            text=f"Thanks — retrying item {item.id} with that.",
        )
    log.info("item %s answered, resuming at %s", item.id, resume)
    return "answered"
