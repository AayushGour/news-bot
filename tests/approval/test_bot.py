from types import SimpleNamespace

import pytest

from pipeline.approval.auth import operator_only
from pipeline.approval.bot import (
    APPROVE,
    CAPTION,
    REGEN,
    REJECT,
    Pending,
    build_keyboard,
    build_preview_text,
    handle_callback,
    handle_pending_reply,
    parse_callback,
    send_preview,
)
from pipeline.approval.bot import to_album, to_markup
from pipeline.errors import Retryable
from pipeline.models import Status

OPERATOR = 424242
STRANGER = 999999


class FakeBot:
    """Rejects what aiogram rejects.

    An earlier version accepted bare path strings and a list-of-tuples keyboard.
    Both are invalid — aiogram raises a pydantic ValidationError at send time —
    but the permissive fake let 170 tests pass while the live pipeline failed on
    the first preview it tried to send. A fake looser than the real API tests
    nothing.
    """

    def __init__(self):
        self.albums = []
        self.messages = []

    async def send_media_group(self, chat_id, media):
        from aiogram.types import InputMediaPhoto

        assert isinstance(media, list) and media, "media must be a non-empty list"
        for entry in media:
            assert isinstance(entry, InputMediaPhoto), (
                f"aiogram requires InputMediaPhoto, got {type(entry).__name__}"
            )
        self.albums.append((chat_id, media))
        return [SimpleNamespace(message_id=1000 + len(self.albums))]

    async def send_message(self, chat_id, text, reply_markup=None,
                           reply_to_message_id=None):
        if reply_markup is not None:
            from aiogram.types import InlineKeyboardMarkup

            assert isinstance(reply_markup, InlineKeyboardMarkup), (
                f"aiogram requires InlineKeyboardMarkup, got "
                f"{type(reply_markup).__name__}"
            )
        self.messages.append(SimpleNamespace(
            chat_id=chat_id, text=text, reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            message_id=2000 + len(self.messages),
        ))
        return self.messages[-1]


def _callback(data, user_id=OPERATOR):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id))


def _message(text, user_id=OPERATOR):
    return SimpleNamespace(text=text, caption=None,
                           from_user=SimpleNamespace(id=user_id),
                           chat=SimpleNamespace(id=user_id), message_id=77)


async def _seed_awaiting(db, **fields):
    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=1, raw_text="news")
    base = {
        "caption": "A caption about the story",
        "triage_score": 8,
        "slides": [{"type": "hook", "headline": "H"}],
        "rendered_paths": ["/tmp/a.png", "/tmp/b.png"],
        "research": [{"claim": "c", "sources": ["https://teslarati.example/a"]}],
    }
    base.update(fields)
    await db.transition(i, Status.AWAITING_APPROVAL, base)
    return i


# ------------------------------------------------------------------- preview


def test_keyboard_carries_all_four_actions_bound_to_the_item():
    rows = build_keyboard(42)
    data = [d for row in rows for _, d in row]
    assert data == ["approve:42", "regen:42", "caption:42", "reject:42"]


async def test_preview_lists_source_domains(db):
    """The operator's last chance to catch a relevance failure."""
    i = await _seed_awaiting(db, research=[
        {"claim": "c", "sources": ["https://teslarati.example/a",
                                   "https://livemint.example/b"]},
    ])
    text = build_preview_text(await db.get_item(i))

    assert "teslarati.example" in text and "livemint.example" in text


async def test_preview_flags_when_there_are_no_sources(db):
    i = await _seed_awaiting(db, research=[])
    assert "check this carefully" in build_preview_text(await db.get_item(i))


async def test_preview_shows_triage_score_and_counts(db):
    i = await _seed_awaiting(db)
    text = build_preview_text(await db.get_item(i))
    assert "triage 8/10" in text and "1 research notes" in text


async def test_keyboard_is_on_a_separate_message_from_the_album(db, settings):
    """Telegram albums cannot carry inline keyboards."""
    bot = FakeBot()
    i = await _seed_awaiting(db)

    fields = await send_preview(bot, await db.get_item(i), settings)

    assert len(bot.albums) == 1 and len(bot.messages) == 1
    assert len(bot.albums[0][1]) == 2, "one InputMediaPhoto per rendered slide"
    assert bot.messages[0].reply_markup is not None, "buttons go on the text message"
    assert bot.messages[0].reply_to_message_id == 1001, "and it replies to the album"
    assert fields["approval_msg_id"] == bot.messages[0].message_id


async def test_send_preview_without_images_is_retryable(db, settings):
    i = await _seed_awaiting(db, rendered_paths=[])
    with pytest.raises(Retryable):
        await send_preview(FakeBot(), await db.get_item(i), settings)


# ----------------------------------------------------------------- callbacks


def test_parse_callback_rejects_junk():
    assert parse_callback("approve:12") == ("approve", 12)
    assert parse_callback("drop:12") is None
    assert parse_callback("approve:abc") is None
    assert parse_callback("") is None


async def test_stranger_callback_does_not_transition(db, settings):
    """Without this check, anyone could publish to the operator's Instagram."""
    i = await _seed_awaiting(db)
    pending = Pending()

    @operator_only(settings)
    async def guarded(callback):
        return await handle_callback(callback, db, settings, pending)

    await guarded(_callback(f"approve:{i}", user_id=STRANGER))

    assert (await db.get_item(i)).status == Status.AWAITING_APPROVAL


async def test_approve_transitions_to_approved(db, settings):
    i = await _seed_awaiting(db)
    action = await handle_callback(_callback(f"approve:{i}"), db, settings, Pending())

    assert action == APPROVE
    assert (await db.get_item(i)).status == Status.APPROVED


async def test_reject_transitions_to_rejected(db, settings):
    i = await _seed_awaiting(db)
    assert await handle_callback(_callback(f"reject:{i}"), db, settings, Pending()) == REJECT
    assert (await db.get_item(i)).status == Status.REJECTED


async def test_second_press_is_ignored(db, settings):
    """A double tap must not re-approve an already-published item."""
    i = await _seed_awaiting(db)
    pending = Pending()
    await handle_callback(_callback(f"approve:{i}"), db, settings, pending)

    assert await handle_callback(_callback(f"approve:{i}"), db, settings, pending) is None
    assert (await db.get_item(i)).status == Status.APPROVED


async def test_callback_for_missing_item_is_ignored(db, settings):
    assert await handle_callback(_callback("approve:9999"), db, settings, Pending()) is None


# -------------------------------------------------------- regenerate & caption


async def test_regenerate_returns_to_composed_and_keeps_the_brief(db, settings):
    """Regeneration must not re-research — that is why brief is stored apart."""
    i = await _seed_awaiting(db, brief="THE BRIEF")
    pending = Pending()

    await handle_callback(_callback(f"regen:{i}"), db, settings, pending)
    action = await handle_pending_reply(_message("punchier hook"), db, settings, pending)

    item = await db.get_item(i)
    assert action == REGEN
    assert item.status == Status.COMPOSED
    assert item.brief == "THE BRIEF"
    assert item.regen_note == "punchier hook"
    assert item.research, "research must survive regeneration"


async def test_regenerate_with_skip_carries_no_note(db, settings):
    i = await _seed_awaiting(db, brief="B")
    pending = Pending()
    await handle_callback(_callback(f"regen:{i}"), db, settings, pending)
    await handle_pending_reply(_message("/skip"), db, settings, pending)

    item = await db.get_item(i)
    assert item.status == Status.COMPOSED and item.regen_note is None


async def test_caption_edit_updates_text_without_re_rendering(db, settings):
    i = await _seed_awaiting(db)
    before = (await db.get_item(i)).rendered_paths
    pending = Pending()

    await handle_callback(_callback(f"caption:{i}"), db, settings, pending)
    action = await handle_pending_reply(_message("A better caption"), db, settings, pending)

    item = await db.get_item(i)
    assert action == CAPTION
    assert item.caption == "A better caption"
    assert item.status == Status.AWAITING_APPROVAL
    assert item.rendered_paths == before, "caption edits must not re-render"


async def test_empty_caption_reply_asks_again(db, settings):
    i = await _seed_awaiting(db)
    pending = Pending()
    bot = FakeBot()
    await handle_callback(_callback(f"caption:{i}"), db, settings, pending)

    assert await handle_pending_reply(_message("   "), db, settings, pending, bot) is None
    # Still waiting, so the next reply is still treated as the caption.
    assert await handle_pending_reply(_message("Real caption"), db, settings, pending) == CAPTION


async def test_pending_reply_is_ignored_when_nothing_is_pending(db, settings):
    assert await handle_pending_reply(_message("hello"), db, settings, Pending()) is None


# ------------------------------------------------- aiogram type conformance


def test_to_album_builds_input_media_photos():
    """Regression: raw path strings were passed straight to send_media_group,
    which aiogram rejects with a pydantic ValidationError at send time."""
    from aiogram.types import InputMediaPhoto

    album = to_album(["/tmp/a.png", "/tmp/b.png"])
    assert len(album) == 2
    assert all(isinstance(entry, InputMediaPhoto) for entry in album)


def test_to_markup_builds_an_inline_keyboard():
    """Regression: build_keyboard's list-of-tuples was passed as reply_markup."""
    from aiogram.types import InlineKeyboardMarkup

    markup = to_markup(build_keyboard(42))
    assert isinstance(markup, InlineKeyboardMarkup)
    flat = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert flat == ["approve:42", "regen:42", "caption:42", "reject:42"]
