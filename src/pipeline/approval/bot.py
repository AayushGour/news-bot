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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..errors import Retryable
from ..limits import TELEGRAM_CALLBACK_DATA_LIMIT, TELEGRAM_TEXT_LIMIT
from ..models import Item, Status
from ..revision import copy as rcopy
from ..revision.record import latest_run
from ..revision.request import (
    ConfirmResult,
    Origin,
    Surface,
    confirm_revision,
    pending_confirm,
    request_revision,
)
from .auth import operator_only

log = logging.getLogger(__name__)

APPROVE, REJECT, REGEN, CAPTION = "approve", "reject", "regen", "caption"

KEYBOARD_ROWS = [
    [("✅ Approve", APPROVE), ("🔄 Regenerate", REGEN)],
    [("✏️ Caption", CAPTION), ("❌ Reject", REJECT)],
]

#: The pre-agentic prompt: what REVISE_ENABLED=false (the rollback switch) still shows.
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
#: What ``handle_pending_reply`` returns when it answered a stray command with the hint.
COMMAND_HINT = "command-hint"
#: Returned by ``handle_pending_reply`` when the operator's reply was consumed but the
#: item had moved on: truthy, so the reply is not then ingested as a new request.
MOVED = "moved"

#: Callback data for the Yes/No under a revision confirm: ``rvc:y:{item id}:{token}``.
#: The token is ``pending_confirm.token``; it is what makes a stale button detectable.
CONFIRM_PREFIX = "rvc"
_CONFIRM_YES, _CONFIRM_NO = "y", "n"
_TOKEN = re.compile(r"[0-9a-f]{1,32}")
#: An item id is a SQLite INTEGER (64-bit signed, at most 19 digits); a longer number in
#: callback data cannot be one of ours and would raise OverflowError in the query.
_MAX_ID_DIGITS = 18
#: Statuses a Regenerate may start a revision from (the bot's policy; REVISION_CONFIRM
#: supersedes the open question).
REGEN_FROM = (Status.AWAITING_APPROVAL, Status.REVISION_CONFIRM)


class Pending:
    """Which item the operator is currently typing a reply for.

    Deliberately in-memory: a restart simply means pressing the button again,
    which is cheaper than a migration and a cleanup job for abandoned state.
    """

    def __init__(self) -> None:
        self._by_user: dict[int, tuple[str, int]] = {}
        # item id -> the message id of its open Yes/No, so a newer note can retire it.
        self._confirm_msgs: dict[int, int] = {}

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

    def remember_confirm(self, item_id: int, message_id: int | None) -> None:
        if message_id is not None:
            self._confirm_msgs[item_id] = message_id

    def take_confirm(self, item_id: int) -> int | None:
        return self._confirm_msgs.pop(item_id, None)


# ------------------------------------------------------------------- preview


def _legacy_preview_text(item: Item) -> str:
    """The preview of an item that has no revision to report. Byte-for-byte what it
    was before agentic regeneration (pinned by a snapshot test)."""
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


@dataclass(frozen=True)
class Preview:
    """The preview text, plus the Changes block when it cannot share a message with
    the caption (spec §5.9 step 7: send it first, as its own message)."""

    text: str
    standalone: str | None = None


def _available_themes() -> list[str]:
    from ..stages.render import available_themes  # lazy: the render stage is heavy

    return available_themes()


def build_preview(
    item: Item, *, limit: int = TELEGRAM_TEXT_LIMIT, with_changes: bool = True
) -> Preview:
    """Changes block (what the last revision did, built from ``items.revision``) on top,
    then the caption and stats exactly as before. The caption is never cut: the block
    shrinks to fit (``copy.fit_changes``). An item with no current revision run gets
    the legacy text unchanged. A failure to render the block never blocks the preview."""
    base = _legacy_preview_text(item)
    run = latest_run(item.revision, item.slides) if with_changes else None
    if run is None:
        return Preview(base)
    try:
        block = rcopy.fit_changes(
            run, slides=item.slides, themes=_available_themes(),
            budget=rcopy.preview_budget(len(base), limit),
        )
    except Exception as exc:  # advisory text: log the class only (rule 15), send the preview
        log.warning("item %s: could not render the Changes block (%s)", item.id, type(exc).__name__)
        return Preview(base)
    if not block.text:
        return Preview(base)
    if block.standalone:
        return Preview(base, block.text)
    return Preview(f"{block.text}\n\n{rcopy.SEPARATOR}\n{base}")


def build_preview_text(item: Item, *, limit: int = TELEGRAM_TEXT_LIMIT, with_changes: bool = True) -> str:
    return build_preview(item, limit=limit, with_changes=with_changes).text


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

    preview = build_preview(item)
    if preview.standalone:
        try:
            await bot.send_message(
                chat_id=settings.operator_user_id, text=preview.standalone,
                reply_to_message_id=album_id,
            )
        except Exception as exc:  # the report is advisory: the preview itself must still go out
            log.warning("item %s: could not send the Changes block (%s)", item.id, type(exc).__name__)
    message = await bot.send_message(
        chat_id=settings.operator_user_id,
        text=preview.text,
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


def confirm_callback_data(yes: bool, item_id: int, token: str) -> str:
    """``rvc:y|n:{id}:{token}``, checked against Telegram's 64-BYTE cap (a platform
    limit): too long raises here, at build time, not as a rejected send."""
    data = f"{CONFIRM_PREFIX}:{_CONFIRM_YES if yes else _CONFIRM_NO}:{item_id}:{token}"
    if len(data.encode()) > TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("confirm callback data exceeds Telegram's limit")
    return data


def parse_confirm_callback(data: str) -> tuple[bool, int, str] | None:
    """``(yes, item id, token)`` for a confirm button, else None. Strict: anything
    that is not exactly the shape ``confirm_callback_data`` writes is ignored."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != CONFIRM_PREFIX or parts[1] not in (_CONFIRM_YES, _CONFIRM_NO):
        return None
    if not (parts[2].isascii() and parts[2].isdigit() and len(parts[2]) <= _MAX_ID_DIGITS):
        return None
    if not _TOKEN.fullmatch(parts[3]):
        return None
    return parts[1] == _CONFIRM_YES, int(parts[2]), parts[3]


def _already_handled(item: Item) -> str:
    """The toast for a button on an item that is no longer at the gate. A revision
    status gets its own words (the raw status name is not for the operator)."""
    if item.status == Status.REVISION_CONFIRM:
        return rcopy.BUSY_CONFIRM_TOAST
    if item.status == Status.REVISION_REQUESTED:
        return rcopy.BUSY_REVISING_TOAST
    return f"Already handled — that item is '{item.status}'."


async def handle_callback(
    callback: Any, db: Database, settings: Any, pending: Pending,
    answer: Any = None, send: Any = None, edit: Any = None,
) -> str | None:
    """Route one button press. Returns the action taken, for logging and tests.

    ``answer`` is the toast (<= 200 characters), ``send`` a chat message to the
    operator (the Regenerate prompt is too long for a toast), ``edit`` rewrites the
    message the tapped button was on (used by the Yes/No)."""
    data = getattr(callback, "data", "")
    confirm = parse_confirm_callback(data)
    if confirm is not None:
        return await handle_confirm_callback(confirm, db, pending, answer, edit)
    parsed = parse_callback(data)
    if parsed is None:
        return None
    action, item_id = parsed

    item = await db.get_item(item_id)
    if item is None:
        await _say(answer, "That item no longer exists.")
        return None

    revise_on = bool(settings.revise_enabled)
    if item.status != Status.AWAITING_APPROVAL:
        # Double-tap, or the operator acted from an old message. The one exception:
        # a newer note may replace a Yes/No that is still open.
        if not (revise_on and action == REGEN and item.status == Status.REVISION_CONFIRM):
            await _say(answer, _already_handled(item))
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
        suffix = DISPLACED.format(action=displaced[0], id=displaced[1]) if displaced else ""
        if action == REGEN and revise_on:
            # Too long for a toast (Telegram rejects text over 200 characters): a chat
            # message, with a short pointer as the toast.
            await (send or answer or _discard)(rcopy.regen_prompt(item_id, _available_themes()) + suffix)
            if send is not None:
                await _say(answer, rcopy.REGEN_TOAST)
            return action
        prompt = (REGEN_PROMPT if action == REGEN else CAPTION_PROMPT).format(id=item_id)
        await _say(answer, prompt + suffix)
        return action

    return None  # pragma: no cover - parse_callback already filtered


async def _discard(_text: str) -> None:
    return None


def _answer_of(item: Item | None) -> bool | None:
    pc = pending_confirm(item.revision_request) if item is not None else None
    return pc["answer"] if pc is not None else None


async def handle_confirm_callback(
    confirm: tuple[bool, int, str], db: Database, pending: Pending,
    answer: Any = None, edit: Any = None,
) -> str | None:
    """The Yes/No under a destructive revision. ``confirm_revision`` is the only thing
    that moves the item (idempotent; two taps have one winner), so this only maps its
    result to words: a toast, and the question edited (buttons gone) to the outcome."""
    yes, item_id, token = confirm
    item = await db.get_item(item_id)
    view = rcopy.confirm_view_for(item) if item is not None else None  # before the answer clears it
    result = await confirm_revision(db, item_id, yes, token)
    await _say(answer, rcopy.toast(rcopy.CONFIRM_TOASTS[result]))
    text = rcopy.confirm_answered_text(result, view, item_id, answered=_answer_of(item))
    if text and edit is not None:
        await edit(text)
    if result in (ConfirmResult.YES, ConfirmResult.NO_CONTINUE, ConfirmResult.NO_NOTHING_LEFT):
        pending.take_confirm(item_id)
    return f"confirm:{result.value}"


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


#: Words that make a reply a note ABOUT THE DECK ("find a better photo for slide 2"): a
#: Regenerate reply with one of these is a revision note whatever its opening word.
_DECK_WORDS = re.compile(
    r"\b(?:slides?|caption|hashtags?|photos?|images?|pictures?|theme|headline|hook|bullets?|title|deck|"
    r"carousel|chart|kpi|font|colou?rs?|layout)\b", re.I,
)
#: "make it better", "make slide 3 shorter": the commonest way to word a note. The
#: Regenerate prompt itself teaches it, so for a Regenerate reply it is not a new request.
_REGEN_NOTE_OPENERS = ("make ",)
#: A reply that names a thing of THIS deck ("slide 3", the caption) is a note, always.
_THIS_DECK = re.compile(r"\b(?:slides?\s*#?\d+|caption|hashtags?)\b", re.I)
#: ... and one that asks for a NEW piece of content is a new request, whatever the verb.
#: Anchored at the start ("make post about tariffs", "write a carousel about the caption
#: contest winners", "do another one about quantum"): the topic may mention deck words
#: without making it a note about THIS deck. "make it a deck with fewer slides" has no
#: "about/on/for" after the noun, so it is not matched.
_CONTENT_ADJ = r"(?:(?:new|short|quick|fresh)\s+)?"
#: The noun and what must follow it. "one" only before about/on ("do one on rust"): "make one
#: of the photos bigger" and "make one for each point" are notes. No "of": "a deck of 6 slides".
_CONTENT_PHRASE = r"(?:(?:post|carousel|deck|thread)\s+(?:about|on|for|from)|one\s+(?:about|on))\b"
_CREATE_VERBS = r"(?:make|create|write|build|draft|produce|generate|do|give\s+me|need)"
_NEW_CONTENT_FIRST = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    rf"(?:{_CREATE_VERBS}\s+(?:me\s+)?(?:(?:a|an|another|one)\s+)?|(?:a|an|another|one)\s+)"
    rf"{_CONTENT_ADJ}{_CONTENT_PHRASE}",
    re.I,
)
#: Anywhere in the reply, but only with an article: "... for a new post", "a deck about X".
_NEW_CONTENT = re.compile(
    rf"\b(?:a|an|another|new|one)\s+{_CONTENT_ADJ}(?:post|carousel|deck|thread)\s+(?:about|on|for|from)\b"
    r"|\bfor\s+(?:a|an|another|one)\s+" + _CONTENT_ADJ + r"(?:post|carousel|deck|thread)\b"
    r"|\bdo\s+(?:another\s+)?(?:one|another)\s+(?:on|about)\b",
    re.I,
)


def looks_like_a_new_request(text: str, *, regen_note: bool = False) -> bool:
    """Does this read as a fresh instruction rather than an answer?

    Deliberately narrow. A false positive costs the operator one extra tap to
    confirm; treating every reply as possibly-a-request would make the caption
    flow unusable, and the common case — a caption — must stay one message.

    ``regen_note`` (a Regenerate reply under agentic regeneration): the reply is
    expected to be an instruction, so only openers that start a NEW topic count, and
    not when the reply talks about the deck.
    """
    stripped = (text or "").strip().lower()
    if len(stripped) < 12:
        return False
    if regen_note:
        if _NEW_CONTENT_FIRST.search(stripped):
            return True
        if _THIS_DECK.search(stripped):
            return False
        if _NEW_CONTENT.search(stripped):
            return True
        if stripped.startswith(_REGEN_NOTE_OPENERS) or _DECK_WORDS.search(stripped):
            return False
    return stripped.startswith(_REQUEST_OPENERS)


#: A slash command ("/post", "/drop@bot"), as opposed to a note that merely starts with
#: a slash. The one command a pending prompt understands is ``SKIP``.
_SLASH_COMMAND = re.compile(r"/[A-Za-z_]+(?:@\w+)?(?:\s|$)")

#: Said instead of consuming a command as the operator's note (T366: "/post" sent under a
#: preview became a revision note and was dropped as ungrounded).
COMMAND_FOR_PROMPT_HINT = (
    "That's a command, not the {action} text I'm waiting for on item {id}. "
    "Send the text, or /skip to drop the prompt. (/post and /drop work on items "
    "that are waiting on a question, not on a finished preview.)"
)


def is_command_reply(text: str, action: str) -> bool:
    """Is this reply a slash command that must not be taken as the prompt's text?
    ``/skip`` is the Regenerate prompt's own "no note" answer, so it stays one."""
    stripped = (text or "").strip()
    if not _SLASH_COMMAND.match(stripped):
        return False
    return not (action == REGEN and stripped == SKIP)


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

    if is_command_reply(text, action):
        # Nothing consumed: the prompt stays outstanding, the command is not a note.
        pending.set(user_id, action, item_id)
        if bot:
            await bot.send_message(user_id, COMMAND_FOR_PROMPT_HINT.format(action=action, id=item_id))
        return COMMAND_HINT

    regen_note = action == REGEN and bool(settings.revise_enabled)
    if text != SKIP and looks_like_a_new_request(text, regen_note=regen_note):
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
        if settings.revise_enabled:
            return await _request_regen(db, pending, bot, user_id, item_id, note)
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
                user_id, build_preview_text(refreshed, with_changes=False),
                reply_markup=to_markup(build_keyboard(item_id)),
            )
        return CAPTION

    return None  # pragma: no cover


async def _request_regen(
    db: Database, pending: Pending, bot: Any, user_id: int, item_id: int, note: str
) -> str | None:
    """The operator's reply to the Regenerate prompt: a revision request. Written with
    ``request_revision`` (guarded on the item's status and timestamp), so an approve
    that landed first wins and this one is told the item moved."""
    item = await db.get_item(item_id)
    skipped = not note
    ok = (
        item is not None and item.status in REGEN_FROM
        and await request_revision(
            db, item, origin=Origin.SKIP if skipped else Origin.OPERATOR,
            note=note or None, surface=Surface.TELEGRAM,
        )
    )
    if not ok:
        if bot:
            await bot.send_message(user_id, rcopy.ITEM_MOVED.format(id=item_id))
        return MOVED
    if bot:
        await bot.send_message(
            user_id, (rcopy.ACK_SKIP if skipped else rcopy.ACK_NOTE).format(id=item_id)
        )
        if item.status == Status.REVISION_CONFIRM and (mid := pending.take_confirm(item_id)):
            await _retire_confirm(bot, user_id, mid)
    return REGEN


async def _retire_confirm(bot: Any, chat_id: int, message_id: int) -> None:
    """A newer note replaced an open Yes/No: say so on it and remove its buttons."""
    try:
        await bot.edit_message_text(
            text=rcopy.REPLACED_CONFIRM, chat_id=chat_id, message_id=message_id
        )
    except Exception as exc:  # cosmetic: the stale token already makes the buttons harmless
        log.warning("could not retire a superseded confirm (%s)", type(exc).__name__)


# ------------------------------------------------------------ notices (T302)


def make_revision_notifier(bot: Any, db: Database, settings: Any, pending: Pending) -> Callable[..., Awaitable[None]]:
    """The ``notify(item, Notice)`` the revise stage calls AFTER its transition committed.

    CONFIRM -> the Yes/No; ASK_OPERATOR -> the "what do you mean?" message and the reply
    slot re-armed; DEFERRED -> one "the model is busy" message. Each re-reads the item and
    sends nothing if it has already moved on (a late message about a dead state is worse
    than none). Never raises into the stage's own error handling beyond what Telegram does."""
    from ..stages.revise import NoticeKind  # lazy: the revise stage pulls in compose

    chat_id = settings.operator_user_id

    async def notify(item: Item, notice: Any) -> None:
        if notice.kind is NoticeKind.DEFERRED:
            await bot.send_message(chat_id=chat_id, text=rcopy.DEFERRED.format(id=item.id))
            return
        fresh = await db.get_item(item.id)
        if fresh is None:
            return
        if notice.kind is NoticeKind.CONFIRM:
            await _send_confirm(bot, chat_id, pending, item, fresh, notice)
        elif notice.kind is NoticeKind.ASK_OPERATOR:
            await _send_ask(bot, chat_id, pending, fresh)

    return notify


async def _send_confirm(bot: Any, chat_id: int, pending: Pending, item: Item, fresh: Item, notice: Any) -> None:
    pc = pending_confirm(fresh.revision_request)
    if fresh.status != Status.REVISION_CONFIRM or pc is None or pc["token"] != notice.token:
        log.info("item %s: the confirm moved on before it was sent; not sending", item.id)
        return
    view = rcopy.confirm_view(
        notice.held, notice.rest, slides=item.slides, note=(item.revision_request or {}).get("note")
    )
    rows = [[
        (view.yes, confirm_callback_data(True, item.id, notice.token)),
        (view.no, confirm_callback_data(False, item.id, notice.token)),
    ]]
    sent = await bot.send_message(chat_id=chat_id, text=view.text, reply_markup=to_markup(rows))
    pending.remember_confirm(item.id, getattr(sent, "message_id", None))


async def _send_ask(bot: Any, chat_id: int, pending: Pending, fresh: Item) -> None:
    run = latest_run(fresh.revision, fresh.slides, current_deck=False)
    if fresh.status != Status.AWAITING_APPROVAL or not run or not run.get("ask_operator"):
        return
    text = rcopy.render_ask_operator(
        fresh.id, run.get("dropped") or [], fresh.slides, _available_themes(),
        plan_failed=bool(run.get("plan_failed")),
    )
    displaced = pending.set(chat_id, REGEN, fresh.id)  # the next message is the rephrased note
    if displaced:
        text += DISPLACED.format(action=displaced[0], id=displaced[1])
    await bot.send_message(chat_id=chat_id, text=text)


async def _say(answer: Any, text: str) -> None:
    if answer is None:
        return
    await answer(text)


# ---------------------------------------------------------------- wiring


def register_approval(
    dispatcher: Any, db: Database, settings: Any, bot: Any, pending: Pending
) -> tuple:
    """Attach approval handlers. Returns them so tests can call them directly."""
    from ..intake.bot_intake import dm_downloader, handle_dm

    @operator_only(settings)
    async def on_callback(callback: Any) -> str | None:
        async def answer(text: str) -> None:
            await callback.answer(text, show_alert=False)

        async def send(text: str) -> None:
            await bot.send_message(chat_id=settings.operator_user_id, text=text)

        async def edit(text: str) -> None:
            # Editing with no reply_markup removes the buttons: a second tap is impossible.
            try:
                await callback.message.edit_text(text)
            except Exception as exc:  # cosmetic; the token makes a second tap harmless
                log.warning("could not edit the confirm message (%s)", type(exc).__name__)

        return await handle_callback(
            callback, db, settings, pending, answer=answer, send=send, edit=edit
        )

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

        return await handle_dm(message, db, settings, download=dm_downloader(bot), reply=reply)

    if dispatcher is not None:
        dispatcher.callback_query.register(on_callback)
        dispatcher.message.register(on_message)
    return on_callback, on_message
