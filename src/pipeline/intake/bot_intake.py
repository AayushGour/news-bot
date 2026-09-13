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


def looks_truncated(message: Any) -> str | None:
    """Did Telegram cut this message before we saw it?

    Only a length sitting exactly on a Telegram ceiling is evidence, and it is
    circumstantial — a message can legitimately be 4096 characters. Saying so
    is still better than losing half a request in silence, which is what
    happened before: a long brief sent as an image caption arrived at 1024
    characters with no indication anything was missing.
    """
    caption = getattr(message, "caption", None)
    if caption and len(caption.strip()) >= TELEGRAM_CAPTION_LIMIT:
        return CAPTION_TRUNCATED.format(limit=TELEGRAM_CAPTION_LIMIT)
    return None


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

#: Telegram's own ceilings, not ours. A text message is capped at 4096
#: characters and a media caption at 1024; the sending client splits or trims
#: anything longer before the bot is ever called. Both were silent.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

#: A message arriving this soon after the last one, on an item still short of
#: research, is treated as a continuation of it rather than a new request.
CONTINUATION_WINDOW_S = 120

CONTINUED = (
    "Added that to item {id} — I'll research them together.\n\n"
    "Send /new first if you meant to start a separate request."
)

CAPTION_TRUNCATED = (
    "⚠️ Telegram caps an image caption at {limit} characters and yours hit "
    "exactly that, so the rest never reached me.\n\n"
    "Send the full text as its own message and I'll research them together."
)

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

    warning = looks_truncated(message)
    if warning and reply:
        await reply(warning)

    # A split message arrives as several; without this each fragment becomes
    # its own item and is researched on a piece of the request.
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    msg_id = getattr(message, "message_id", None)

    # Duplicate before continuation: a redelivered message has the same id and
    # is the same request again, not the next part of it.
    if await db.already_ingested(chat_id, msg_id):
        if reply:
            await reply(DUPLICATE_ACK)
        return None

    if not media_paths:
        previous = await db.continuable_dm(chat_id, CONTINUATION_WINDOW_S)
        if previous is not None:
            await db.append_raw_text(previous.id, text)
            if reply:
                await reply(CONTINUED.format(id=previous.id))
            log.info("appended %d chars to item %s as a continuation",
                     len(text), previous.id)
            return previous.id

    item_id = await db.insert_item(
        source="dm",
        source_chat_id=chat_id,
        source_msg_id=msg_id,
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
