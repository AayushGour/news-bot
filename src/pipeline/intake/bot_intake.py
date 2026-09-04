"""Operator DM intake.

The operator can feed the pipeline directly — text, a link, an image, or a
forwarded message — using the same bot that delivers approvals.

Items from here bypass triage (spec §7.1), which is precisely why every entry
point is wrapped in :func:`~pipeline.approval.auth.operator_only`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from ..approval.auth import operator_only
from ..db import Database

log = logging.getLogger(__name__)

ACK = "Got it — researching. I'll send a preview when it's ready."
DUPLICATE_ACK = "Already have that one in the queue."
EMPTY_ACK = "Nothing to work with there — send text, a link, or an image."


def message_text(message: Any) -> str:
    """Text of a message, whether it arrived as body text or a media caption."""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


#: Telegram sends /start when the operator first opens the chat. Treating that
#: as content produced a fully researched carousel about Meta's model releases
#: from a message that contained no story at all.
COMMANDS = {
    "/start": (
        "Ready. Send me text, a link, or an image and I'll research it and "
        "build a carousel.\n\n"
        "/status — what's in the queue\n"
        "/help — this message"
    ),
    "/help": (
        "Send text, a link, or an image. I research it, write slides, render "
        "them, and send a preview here with Approve / Regenerate / Caption / "
        "Reject.\n\n"
        "I also watch the configured channel automatically."
    ),
}

#: Below this, a DM has nothing to research. Research will always find
#: *something* on the open web, so an empty prompt yields a confident,
#: sourced-looking post about whatever it stumbled across.
MIN_DM_CHARS = 15

TOO_THIN = (
    "That's too short to research — I'd end up inventing a story around it.\n\n"
    "Send a headline, a link, a paragraph, or an image."
)


def command_reply(text: str) -> str | None:
    """The canned answer for a slash command, or None if it is not one."""
    if not text.startswith("/"):
        return None
    word = text.split()[0].split("@")[0].lower()
    return COMMANDS.get(word, "Unknown command. /help for what I can do.")


async def handle_dm(
    message: Any,
    db: Database,
    settings: Any,
    download: Callable[[Any, Path], Awaitable[list[str]]] | None = None,
    reply: Callable[[str], Awaitable[None]] | None = None,
) -> int | None:
    """Turn an operator DM into an ``ingested`` item. Returns its id, or None."""
    text = message_text(message)

    # Commands are instructions to the bot, not material to publish.
    canned = command_reply(text)
    if canned is not None:
        if reply:
            await reply(canned)
        return None

    media_paths: list[str] = []
    if download is not None:
        target = Path(settings.media_dir) / "inbox"
        try:
            media_paths = await download(message, target)
        except Exception as exc:
            # A failed download must not lose the message; the text is usually
            # enough on its own.
            log.warning("DM media download failed: %s", exc)

    if not text and not media_paths:
        if reply:
            await reply(EMPTY_ACK)
        return None

    # An image carries its own substance; bare text has to stand on its own.
    if not media_paths and len(text) < MIN_DM_CHARS:
        if reply:
            await reply(TOO_THIN)
        return None

    item_id = await db.insert_item(
        source="dm",
        source_chat_id=getattr(getattr(message, "chat", None), "id", None),
        source_msg_id=getattr(message, "message_id", None),
        raw_text=text,
        raw_media_paths=media_paths,
    )

    if reply:
        await reply(ACK if item_id is not None else DUPLICATE_ACK)
    if item_id is not None:
        log.info("ingested DM item %s (%d chars, %d media)",
                 item_id, len(text), len(media_paths))
    return item_id


def register_intake(dispatcher: Any, db: Database, settings: Any, bot: Any = None) -> Callable:
    """Attach the DM handler to an aiogram dispatcher.

    Returns the wrapped handler so tests can exercise it directly.
    """

    @operator_only(settings)
    async def on_message(message: Any) -> int | None:
        async def reply(text: str) -> None:
            await message.answer(text)

        return await handle_dm(message, db, settings, download=None, reply=reply)

    if dispatcher is not None:
        dispatcher.message.register(on_message)
    return on_message
