"""Operator DM intake.

The operator can feed the pipeline directly — text, a link, an image, or a
forwarded message — using the same bot that delivers approvals.

Items from here bypass triage (spec §7.1), which is precisely why every entry
point is wrapped in :func:`~pipeline.approval.auth.operator_only` (the handler is
registered in :func:`~pipeline.approval.bot.register_approval`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from ..db import Database
from ..models import Status
from ..limits import TELEGRAM_CAPTION_LIMIT, TELEGRAM_TEXT_LIMIT  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

ACK = "Got it — researching. I'll send a preview when it's ready."
DUPLICATE_ACK = "Already have that one in the queue."
EMPTY_ACK = "Nothing to work with there — send text, a link, or an image."


#: Images we hand to the vision model. A photo is always a JPEG; an image sent
#: "as a file" keeps its own type, and anything else is not ours to read.
IMAGE_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def dm_downloader(bot: Any) -> Callable[[Any, Path], Awaitable[list[str]]]:
    """Download the image a DM carries (photo, or image sent as a file)."""

    async def download(message: Any, target: Path) -> list[str]:
        photos = getattr(message, "photo", None)
        document = getattr(message, "document", None)
        if photos:
            file, suffix = photos[-1], ".jpg"  # the largest size Telegram made
        elif document is not None and document.mime_type in IMAGE_SUFFIXES:
            file, suffix = document, IMAGE_SUFFIXES[document.mime_type]
        else:
            return []
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{message.chat.id}_{message.message_id}{suffix}"
        await bot.download(file, destination=path)
        return [str(path)]

    return download


def message_text(message: Any) -> str:
    """Text of a message, whether it arrived as body text or a media caption."""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def hit_ceiling(message: Any) -> bool:
    """Does this message sit on a Telegram length ceiling?

    A caption is truncated at exactly the limit. A text body is split by the
    client at a word or paragraph break, so a fragment lands a little under
    the limit (SPLIT_MARGIN_CHARS). Either is circumstantial evidence — a
    message can legitimately be that long.
    """
    caption = getattr(message, "caption", None)
    if caption and len(caption.strip()) >= TELEGRAM_CAPTION_LIMIT:
        return True
    body = getattr(message, "text", None)
    return bool(body) and len(body.strip()) >= TELEGRAM_TEXT_LIMIT - SPLIT_MARGIN_CHARS


def looks_truncated(message: Any) -> str | None:
    """Did Telegram cut this message before we saw it?

    Saying so is better than losing half a request in silence, which is what
    happened before: a long brief sent as an image caption arrived at 1024
    characters with no indication anything was missing. (A text body over 4096
    arrives as further messages, which :func:`handle_dm` joins; a caption is
    cut and the rest never arrives.)
    """
    caption = getattr(message, "caption", None)
    if caption and len(caption.strip()) >= TELEGRAM_CAPTION_LIMIT:
        return CAPTION_TRUNCATED.format(limit=TELEGRAM_CAPTION_LIMIT)
    return None


#: The menu Telegram shows when the operator types "/" (set at startup).
MENU = (
    ("status", "Posts: processing, pending, complete, rejected"),
    ("channels", "Channels I watch for stories"),
    ("help", "What I can do"),
    ("post", "Answer my question: post with what I have"),
    ("drop", "Answer my question: drop the item"),
)
CHANNELS = "/channels"

#: Reaches here only when no item is waiting on a question (handle_answer runs first).
NOTHING_WAITING = "Nothing is waiting on a question right now."

#: Telegram sends /start when the operator first opens the chat. Treating that
#: as content produced a fully researched carousel about Meta's model releases
#: from a message that contained no story at all.
COMMANDS = {
    "/post": NOTHING_WAITING,
    "/drop": NOTHING_WAITING,
    "/start": (
        "Ready. Send me text, a link, or an image and I'll research it and "
        "build a carousel.\n\n"
        + "\n".join(f"/{name} — {about}" for name, about in MENU)
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

# TELEGRAM_TEXT_LIMIT / TELEGRAM_CAPTION_LIMIT live in pipeline.limits (a leaf) and are
# re-exported here so existing importers keep working.

#: A text DM arriving this soon after one that sat on a Telegram length ceiling
#: is the rest of that message. Every other DM is its own request (T364).
CONTINUATION_WINDOW_S = 120

#: Telegram clients split long text at a word/paragraph break, so a fragment
#: lands slightly under TELEGRAM_TEXT_LIMIT. A text fragment within this many
#: characters of the limit counts as cut. Captions are truncated, not split, so
#: they stay exact.
SPLIT_MARGIN_CHARS = 300

COPY_OF = (
    "I already have this one: item {id} ({status}), so I'm not starting it again. "
    "/status shows where it is."
)
RETRYABLE_BY_RESENDING = (Status.DROPPED, Status.FAILED, Status.REJECTED)

CONTINUED = "Got the rest of that — researching it together."

CAPTION_TRUNCATED = (
    "⚠️ Telegram caps an image caption at {limit} characters and yours hit "
    "exactly that, so the rest never reached me.\n\n"
    "Send the full text as its own message and I'll research them together."
)

TOO_THIN = (
    "That's too short to research — I'd end up inventing a story around it.\n\n"
    "Send a headline, a link, a paragraph, or an image."
)


def command_word(text: str) -> str | None:
    """"/help@bot more" -> "/help"; None if the text is not a command."""
    if not text.startswith("/"):
        return None
    return text.split()[0].split("@")[0].lower()


def command_reply(text: str) -> str | None:
    """The canned answer for a slash command, or None if it is not one."""
    word = command_word(text)
    if word is None:
        return None
    return COMMANDS.get(word, "Unknown command. /help for what I can do.")


async def handle_dm(
    message: Any,
    db: Database,
    settings: Any,
    download: Callable[[Any, Path], Awaitable[list[str]]] | None = None,
    reply: Callable[[str], Awaitable[None]] | None = None,
    channels: Callable[[], Awaitable[str]] | None = None,
) -> int | None:
    """Turn an operator DM into an ``ingested`` item. Returns its id, or None."""
    text = message_text(message)

    if channels is not None and command_word(text) == CHANNELS:
        if reply:
            await reply(await channels())
        return None

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

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    msg_id = getattr(message, "message_id", None)
    at_ceiling = hit_ceiling(message)

    # Duplicate before the split-message join: a redelivered message has the same id and
    # is the same request again, not the next part of it.
    if await db.already_ingested(chat_id, msg_id):
        if reply:
            await reply(DUPLICATE_ACK)
        return None

    # Telegram delivers a message over 4096 characters as several; only a
    # previous fragment cut at a ceiling can have more coming. Anything else
    # is a new request, however soon it follows.
    if not media_paths:
        previous = await db.continuable_dm(chat_id, CONTINUATION_WINDOW_S)
        if previous is not None:
            # False if the item moved on (e.g. to research) since we looked.
            if await db.append_raw_text(previous.id, text, at_ceiling=at_ceiling):
                if reply:
                    await reply(CONTINUED)
                log.info("appended %d chars to item %s as a continuation",
                         len(text), previous.id)
                return previous.id

    # A repost of something already in hand. A copy of a dropped, failed or rejected
    # item is not: sending it again means "try again".
    copy = await db.recent_copy(text, ignore=RETRYABLE_BY_RESENDING)
    if copy is not None:
        if reply:
            await reply(COPY_OF.format(id=copy.id, status=str(copy.status).replace("_", " ")))
        return None

    item_id = await db.insert_item(
        source="dm",
        source_chat_id=chat_id,
        source_msg_id=msg_id,
        raw_text=text,
        raw_media_paths=media_paths,
        at_ceiling=at_ceiling,
    )

    if reply:
        await reply(ACK if item_id is not None else DUPLICATE_ACK)
    if item_id is not None:
        log.info("ingested DM item %s (%d chars, %d media)",
                 item_id, len(text), len(media_paths))
    return item_id
