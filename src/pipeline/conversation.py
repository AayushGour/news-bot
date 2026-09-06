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
from typing import Any

from .db import Database
from .models import Item, Status

log = logging.getLogger(__name__)


class NeedsInput(Exception):
    """A stage cannot continue without an answer from the operator.

    Carries the question and the status to resume from, so the reply re-enters
    the pipeline at the stage that asked rather than restarting the item.
    """

    def __init__(self, question: str, resume_status: Status, confidence: int | None = None):
        super().__init__(question)
        self.question = question
        self.resume_status = resume_status
        self.confidence = confidence


async def ask(
    db: Database, bot: Any, settings: Any, item: Item,
    question: str, resume_status: Status, confidence: int | None = None,
) -> None:
    """Park the item and put the question to the operator."""
    await db.transition(
        item.id, Status.NEEDS_INPUT,
        {"question": question, "resume_status": str(resume_status),
         "confidence": confidence, "answer": None},
    )
    if bot is None:
        return

    head = f"❓ Item {item.id} needs your input"
    if confidence is not None:
        head += f"  (confidence {confidence}/100)"
    source = (item.raw_text or "").strip().replace("\n", " ")[:120]

    await bot.send_message(
        chat_id=settings.operator_user_id,
        text=(
            f"{head}\n\n"
            f"“{source}”\n\n"
            f"{question}\n\n"
            f"Reply with an answer to continue, or /drop to discard this item."
        ),
    )
    log.info("item %s asked the operator: %s", item.id, question[:80])


DROP = "/drop"


async def handle_answer(
    message: Any, db: Database, settings: Any, bot: Any = None
) -> str | None:
    """Route an operator reply to whichever item is waiting for one.

    Returns the action taken, or None if no item was waiting — in which case
    the message is ordinary intake and the caller should handle it.
    """
    waiting = await db.list_by_status(Status.NEEDS_INPUT, limit=1)
    if not waiting:
        return None

    item = waiting[0]
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return None

    if text.lower() == DROP:
        await db.transition(item.id, Status.REJECTED, {"answer": DROP})
        if bot:
            await bot.send_message(
                chat_id=settings.operator_user_id,
                text=f"Dropped item {item.id}.",
            )
        return "dropped"

    resume = Status(item.resume_status or Status.TRIAGED)
    await db.transition(item.id, resume, {"answer": text, "question": None})
    if bot:
        await bot.send_message(
            chat_id=settings.operator_user_id,
            text=f"Thanks — retrying item {item.id} with that.",
        )
    log.info("item %s answered, resuming at %s", item.id, resume)
    return "answered"
