"""Approval interface: preview, four buttons, and the operator's replies.

Telegram album media groups cannot carry inline keyboards, so a preview is two
messages: the album of rendered slides, then a reply carrying the caption and
the buttons.

The preview deliberately lists the **source domains**. It is the operator's last
chance to notice that research went somewhere irrelevant before it reaches
Instagram — the failure mode that made the PoC unsafe.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import Database
from ..errors import Retryable
from ..models import Item, Status
from .auth import operator_only

log = logging.getLogger(__name__)

APPROVE, REJECT, REGEN, CAPTION = "approve", "reject", "regen", "caption"

KEYBOARD_ROWS = [
    [("✅ Approve", APPROVE), ("🔄 Regenerate", REGEN)],
    [("✏️ Caption", CAPTION), ("❌ Reject", REJECT)],
]

REGEN_PROMPT = (
    "What should change? Reply with a note (e.g. \"punchier hook\", "
    "\"fewer words on slide 3\"), or send /skip to just regenerate."
)
CAPTION_PROMPT = "Send the replacement caption."
SKIP = "/skip"


class Pending:
    """Which item the operator is currently typing a reply for.

    Deliberately in-memory: a restart simply means pressing the button again,
    which is cheaper than a migration and a cleanup job for abandoned state.
    """

    def __init__(self) -> None:
        self._by_user: dict[int, tuple[str, int]] = {}

    def set(self, user_id: int, action: str, item_id: int) -> None:
        self._by_user[user_id] = (action, item_id)

    def pop(self, user_id: int) -> tuple[str, int] | None:
        return self._by_user.pop(user_id, None)

    def clear(self, user_id: int) -> None:
        self._by_user.pop(user_id, None)


# ------------------------------------------------------------------- preview


def build_preview_text(item: Item) -> str:
    notes = item.research or []
    domains = item.source_domains
    lines = [
        (item.caption or "").strip(),
        "",
        "— — —",
        f"triage {item.triage_score if item.triage_score is not None else '-'}/10"
        f" · {len(notes)} research notes"
        f" · {len(item.slides or [])} slides",
    ]
    if domains:
        lines.append("sources: " + ", ".join(domains[:8]))
    else:
        lines.append("sources: none — check this carefully")
    return "\n".join(lines).strip()


def build_keyboard(item_id: int) -> list[list[tuple[str, str]]]:
    """Rows of ``(label, callback_data)``, framework-agnostic."""
    return [
        [(label, f"{action}:{item_id}") for label, action in row]
        for row in KEYBOARD_ROWS
    ]


def to_markup(rows: list[list[tuple[str, str]]]) -> Any:
    """Rows of (label, callback_data) -> the object aiogram actually requires."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def to_album(paths: list[str]) -> list[Any]:
    """File paths -> InputMediaPhoto objects.

    aiogram will not accept bare path strings here; passing them raises a
    pydantic ValidationError at send time, not at import or test time.
    """
    from aiogram.types import FSInputFile, InputMediaPhoto

    return [InputMediaPhoto(media=FSInputFile(str(path))) for path in paths]


async def send_preview(bot: Any, item: Item, settings: Any) -> dict:
    """Send the album, then the caption plus keyboard as a separate message.

    Returns stage fields for the worker to commit. The transition to
    ``AWAITING_APPROVAL`` belongs to the worker so that a send failure leaves
    the item retryable rather than parked in the human gate with no preview.
    """
    paths = item.rendered_paths or []
    if not paths:
        raise Retryable(f"item {item.id} has no rendered slides to preview")

    album = await bot.send_media_group(
        chat_id=settings.operator_user_id, media=to_album(paths)
    )
    album_id = getattr(album[0], "message_id", None) if isinstance(album, list) else None

    message = await bot.send_message(
        chat_id=settings.operator_user_id,
        text=build_preview_text(item),
        reply_markup=to_markup(build_keyboard(item.id)),
        reply_to_message_id=album_id,
    )
    return {"approval_msg_id": getattr(message, "message_id", None)}


# ----------------------------------------------------------------- callbacks


def parse_callback(data: str) -> tuple[str, int] | None:
    action, _, raw_id = (data or "").partition(":")
    if action not in {APPROVE, REJECT, REGEN, CAPTION} or not raw_id.isdigit():
        return None
    return action, int(raw_id)


async def handle_callback(
    callback: Any, db: Database, settings: Any, pending: Pending,
    answer: Any = None,
) -> str | None:
    """Route one button press. Returns the action taken, for logging and tests."""
    parsed = parse_callback(getattr(callback, "data", ""))
    if parsed is None:
        return None
    action, item_id = parsed

    item = await db.get_item(item_id)
    if item is None:
        await _say(answer, "That item no longer exists.")
        return None

    if item.status != Status.AWAITING_APPROVAL:
        # Double-tap, or the operator acted from an old message.
        await _say(answer, f"Already handled — that item is '{item.status}'.")
        return None

    if action == APPROVE:
        await db.transition(item_id, Status.APPROVED)
        await _say(answer, "Approved — publishing now.")
        return APPROVE

    if action == REJECT:
        await db.transition(item_id, Status.REJECTED)
        await _say(answer, "Rejected.")
        return REJECT

    if action == REGEN:
        pending.set(settings.operator_user_id, REGEN, item_id)
        await _say(answer, REGEN_PROMPT)
        return REGEN

    if action == CAPTION:
        pending.set(settings.operator_user_id, CAPTION, item_id)
        await _say(answer, CAPTION_PROMPT)
        return CAPTION

    return None  # pragma: no cover - parse_callback already filtered


async def handle_pending_reply(
    message: Any, db: Database, settings: Any, pending: Pending, bot: Any = None
) -> str | None:
    """Consume a reply the operator owes us. Returns the action, or None."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    if user_id is None:
        return None
    waiting = pending.pop(user_id)
    if waiting is None:
        return None

    action, item_id = waiting
    text = (getattr(message, "text", None) or "").strip()

    if action == REGEN:
        note = "" if text == SKIP else text
        # Back to COMPOSED, not to RESEARCHED: the brief is reused, so this
        # costs one model call rather than a full re-research.
        await db.transition(item_id, Status.COMPOSED, {"regen_note": note or None})
        if bot:
            await bot.send_message(user_id, "Regenerating — new preview shortly.")
        return REGEN

    if action == CAPTION:
        if not text:
            pending.set(user_id, CAPTION, item_id)  # ask again
            if bot:
                await bot.send_message(user_id, "That was empty. " + CAPTION_PROMPT)
            return None
        await db.transition(item_id, Status.AWAITING_APPROVAL, {"caption": text})
        if bot:
            refreshed = await db.get_item(item_id)
            await bot.send_message(
                user_id, build_preview_text(refreshed),
                reply_markup=build_keyboard(item_id),
            )
        return CAPTION

    return None  # pragma: no cover


async def _say(answer: Any, text: str) -> None:
    if answer is None:
        return
    await answer(text)


# ---------------------------------------------------------------- wiring


def register_approval(
    dispatcher: Any, db: Database, settings: Any, bot: Any, pending: Pending
) -> tuple:
    """Attach approval handlers. Returns them so tests can call them directly."""
    from ..intake.bot_intake import handle_dm

    @operator_only(settings)
    async def on_callback(callback: Any) -> str | None:
        async def answer(text: str) -> None:
            await callback.answer(text, show_alert=False)

        return await handle_callback(callback, db, settings, pending, answer=answer)

    @operator_only(settings)
    async def on_message(message: Any) -> Any:
        # A reply the operator owes us takes precedence over new intake,
        # otherwise a caption edit would be ingested as a fresh item.
        consumed = await handle_pending_reply(message, db, settings, pending, bot)
        if consumed:
            return consumed

        async def reply(text: str) -> None:
            await message.answer(text)

        return await handle_dm(message, db, settings, reply=reply)

    if dispatcher is not None:
        dispatcher.callback_query.register(on_callback)
        dispatcher.message.register(on_message)
    return on_callback, on_message
