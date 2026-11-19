"""/status: a dashboard of every post in the bot chat.

One message, edited in place: four tabs with counts, a page of posts, and paging
buttons. A post's button jumps to it. Telegram has no "scroll to message" for a
bot chat, so the jump is a reply to the post's own message (its preview, the
question, or the DM it came from): tapping the quoted reply scrolls the chat there.
A channel post with no message of ours yet links to the channel post instead.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import Database
from ..models import Item, Status

log = logging.getLogger(__name__)

STATUS = "/status"
PAGE_SIZE = 8
TITLE_CHARS = 40

#: tab key -> (label, statuses). Dropped and failed sit with rejected: none will post.
TABS = {
    "w": ("⏳ Processing", (
        Status.INGESTED, Status.TRIAGED, Status.EXTRACTED, Status.RESEARCHED,
        Status.SYNTHESIZED, Status.COMPOSED, Status.RENDERED,
        Status.REVISION_REQUESTED, Status.APPROVED, Status.PUBLISHING,
    )),
    "p": ("🕓 Pending", (Status.AWAITING_APPROVAL, Status.NEEDS_INPUT, Status.REVISION_CONFIRM)),
    "c": ("✅ Complete", (Status.PUBLISHED,)),
    "r": ("❌ Rejected", (Status.REJECTED, Status.DROPPED, Status.FAILED)),
}
DEFAULT_TAB = "p"

#: Status words a row shows where the raw name would not read well.
STATUS_WORDS = {
    Status.AWAITING_APPROVAL: "preview",
    Status.NEEDS_INPUT: "question",
    Status.REVISION_CONFIRM: "confirm",
    Status.REVISION_REQUESTED: "revising",
}

#: Callback data: "st:{tab}:{page}" redraws, "sj:{item id}" jumps.
VIEW, JUMP = "st", "sj"
_MAX_DIGITS = 18


def parse(data: str) -> tuple[str, str, int] | None:
    """``(VIEW, tab, page)`` or ``(JUMP, "", item id)``; None if not ours or malformed."""
    parts = (data or "").split(":")
    numeric = parts[-1].isascii() and parts[-1].isdigit() and len(parts[-1]) <= _MAX_DIGITS
    if parts[0] == VIEW and len(parts) == 3 and parts[1] in TABS and numeric:
        return VIEW, parts[1], int(parts[-1])
    if parts[0] == JUMP and len(parts) == 2 and numeric:
        return JUMP, "", int(parts[-1])
    return None


def word(item: Item) -> str:
    return STATUS_WORDS.get(item.status, str(item.status))


def title(item: Item) -> str:
    text = " ".join(((item.caption or item.raw_text or "").strip().splitlines() or [""])[0].split())
    text = text or "(no text)"
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1] + "…"


def anchor(item: Item) -> int | None:
    """The message in the bot chat a jump replies to."""
    if item.approval_msg_id or item.question_msg_id:
        return item.approval_msg_id or item.question_msg_id
    return item.source_msg_id if item.source == "dm" else None


def channel_link(item: Item) -> str | None:
    """t.me link to a channel post (-1001234 -> t.me/c/1234/msg); works for members."""
    chat = str(abs(item.source_chat_id or 0))
    if item.source != "channel" or not chat.startswith("100") or not item.source_msg_id:
        return None
    return f"https://t.me/c/{chat[3:]}/{item.source_msg_id}"


async def build(db: Database, tab: str = DEFAULT_TAB, page: int = 0) -> tuple[str, list[list[dict]]]:
    """The dashboard text and its keyboard rows (dicts of aiogram button fields)."""
    counts = await db.status_counts()
    totals = {key: sum(counts.get(str(s), 0) for s in statuses) for key, (_, statuses) in TABS.items()}
    pages = max(1, -(-totals[tab] // PAGE_SIZE))
    page = min(page, pages - 1)
    items = await db.page_by_statuses(TABS[tab][1], PAGE_SIZE, page * PAGE_SIZE)

    summary = " · ".join(f"{label} {totals[key]}" for key, (label, _) in TABS.items())
    body = (f"Page {page + 1} of {pages}, newest first. Tap a post to jump to it."
            if items else "Nothing here.")
    text = f"📊 Posts — {TABS[tab][0]}\n{summary}\n\n{body}"

    rows = [[
        {"text": ("• " if key == tab else "") + f"{label.split()[0]} {totals[key]}",
         "callback_data": f"{VIEW}:{key}:0"}
        for key, (label, _) in TABS.items()
    ]]
    for item in items:
        label = f"#{item.id} · {word(item)} · {title(item)}"
        link = None if anchor(item) else channel_link(item)
        rows.append([{"text": label, "url": link} if link else
                     {"text": label, "callback_data": f"{JUMP}:{item.id}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"{VIEW}:{tab}:{page - 1}"})
    nav.append({"text": "🔄", "callback_data": f"{VIEW}:{tab}:{page}"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": f"{VIEW}:{tab}:{page + 1}"})
    rows.append(nav)
    return text, rows


def markup(rows: list[list[dict]]) -> Any:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**b) for b in row] for row in rows])


async def send(bot: Any, chat_id: int, db: Database) -> None:
    text, rows = await build(db)
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup(rows))


async def handle(callback: Any, db: Database, bot: Any, chat_id: int) -> str | None:
    """A dashboard button. Returns what it did, or None if the data is not ours."""
    parsed = parse(getattr(callback, "data", ""))
    if parsed is None:
        return None
    kind, tab, number = parsed
    if kind == VIEW:
        text, rows = await build(db, tab, number)
        try:
            await callback.message.edit_text(text, reply_markup=markup(rows))
        except Exception as exc:  # "message is not modified" on a refresh that changed nothing
            log.debug("dashboard not redrawn (%s)", type(exc).__name__)
        await callback.answer()
        return VIEW

    item = await db.get_item(number)
    target = anchor(item) if item is not None else None
    if target is None:
        await callback.answer("No message for that post in this chat yet.")
        return JUMP
    try:
        await bot.send_message(
            chat_id=chat_id, reply_to_message_id=target,
            text=f"#{item.id} · {word(item)} — tap the quote to jump to it.",
        )
    except Exception as exc:  # the message was deleted
        log.info("item %s: jump target gone (%s)", item.id, type(exc).__name__)
        await callback.answer("That post's message is no longer in this chat.")
        return JUMP
    await callback.answer()
    return JUMP
