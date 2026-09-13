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
    "Regenerating item {id}. What should change? Reply with a note "
    "(e.g. \"punchier hook\", \"fewer words on slide 3\"), or /skip to just "
    "regenerate."
)
CAPTION_PROMPT = "Send the replacement caption for item {id}."
DISPLACED = (
    "\n\n(This replaces your pending {action} on item {id} — that one is "
    "untouched and still waiting.)"
)
SKIP = "/skip"


class Pending:
    """Which item the operator is currently typing a reply for.

    Deliberately in-memory: a restart simply means pressing the button again,
    which is cheaper than a migration and a cleanup job for abandoned state.
    """

    def __init__(self) -> None:
        self._by_user: dict[int, tuple[str, int]] = {}

    def set(self, user_id: int, action: str, item_id: int) -> tuple[str, int] | None:
        """Record what the operator is about to reply to.

        Returns whatever request this displaced, so the caller can say so. A
        plain text reply carries no item reference, so only one request can be
        outstanding — but replacing one silently means the operator's next
        message lands on a different item, as a different action, with no
        indication anything moved.
        """
        previous = self._by_user.get(user_id)
        self._by_user[user_id] = (action, item_id)
        return previous if previous and previous != (action, item_id) else None

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
    if len(paths) < 2:
        # sendMediaGroup requires 2-10 items. compose() already refuses fewer
        # than three slides, but that guard lives two stages away from the call
        # that depends on it, so a change there would break sending silently.
        raise Retryable(
            f"item {item.id} has {len(paths)} slide; a Telegram album needs at least 2"
        )
    if len(paths) > 10:
        raise Retryable(
            f"item {item.id} has {len(paths)} slides; a Telegram album allows at most 10"
        )

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

    if action in (REGEN, CAPTION):
        displaced = pending.set(settings.operator_user_id, action, item_id)
        prompt = (REGEN_PROMPT if action == REGEN else CAPTION_PROMPT).format(
            id=item_id
        )
        if displaced:
            prompt += DISPLACED.format(action=displaced[0], id=displaced[1])
        await _say(answer, prompt)
        return action

    return None  # pragma: no cover - parse_callback already filtered


#: Openings that mean "do this new thing", not "here is the caption you asked
#: for". A pending prompt is a single slot with no way to tell one from the
#: other, so an operator who typed a fresh request while a caption prompt was
#: outstanding had it silently stored as the caption — the request vanished and
#: a finished deck's caption was overwritten with it.
_REQUEST_OPENERS = (
    "research ", "write ", "create ", "make ", "find ", "build ", "generate ",
    "explain ", "post about ", "do a ", "give me ", "list ", "compare ",
    "what is ", "what are ", "how do ", "how does ", "why is ", "why are ",
    "tell me about ",
)


def looks_like_a_new_request(text: str) -> bool:
    """Does this read as a fresh instruction rather than an answer?

    Deliberately narrow. A false positive costs the operator one extra tap to
    confirm; treating every reply as possibly-a-request would make the caption
    flow unusable, and the common case — a caption — must stay one message.
    """
    stripped = (text or "").strip().lower()
    if len(stripped) < 12:
        return False
    return stripped.startswith(_REQUEST_OPENERS)


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

    if text != SKIP and looks_like_a_new_request(text):
        # Ambiguous by construction, so it is put back to the operator rather
        # than guessed. The prompt stays outstanding: whichever they meant,
        # nothing has been consumed or overwritten.
        pending.set(user_id, action, item_id)
        if bot:
            await bot.send_message(
                user_id,
                f"That reads like a new request, but I am still waiting for "
                f"the {action} text for item {item_id}.\n\n"
                f"Send it again prefixed with \"{action}:\" to use it for "
                f"item {item_id}, or /skip to drop the prompt and I will treat "
                f"your next message as a new request.",
            )
        return None

    prefix = f"{action}:"
    if text.lower().startswith(prefix):
        text = text[len(prefix):].strip()

    if action == REGEN:
        note = "" if text == SKIP else text
        # SYNTHESIZED, not COMPOSED. A status names the stage that finished,
        # and the registry maps it to the NEXT stage — so COMPOSED re-renders
        # the same slides, while SYNTHESIZED re-runs compose. The brief is
        # reused either way, so this is one model call, not a re-research.
        await db.transition(item_id, Status.SYNTHESIZED, {"regen_note": note or None})
        if bot:
            await bot.send_message(
                user_id, f"Regenerating item {item_id} — new preview shortly."
            )
        return REGEN

    if action == CAPTION:
        if not text:
            pending.set(user_id, CAPTION, item_id)  # ask again
            if bot:
                await bot.send_message(
                    user_id, "That was empty. " + CAPTION_PROMPT.format(id=item_id)
                )
            return None
        await db.transition(item_id, Status.AWAITING_APPROVAL, {"caption": text})
        if bot:
            refreshed = await db.get_item(item_id)
            await bot.send_message(
                user_id, build_preview_text(refreshed),
                reply_markup=to_markup(build_keyboard(item_id)),
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

        # Then an item waiting on a question. Same reasoning: the answer is a
        # reply, not a new request.
        from ..conversation import handle_answer

        answered = await handle_answer(message, db, settings, bot)
        if answered:
            return answered

        async def reply(text: str) -> None:
            await message.answer(text)

        return await handle_dm(message, db, settings, reply=reply)

    if dispatcher is not None:
        dispatcher.callback_query.register(on_callback)
        dispatcher.message.register(on_message)
    return on_callback, on_message
