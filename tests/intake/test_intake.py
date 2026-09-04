"""Intake and authorisation.

The authorisation tests guard the only path from a stranger to the operator's
Instagram account, so they are the most important tests in the suite.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pipeline.approval.auth import is_operator, operator_only
from pipeline.intake.bot_intake import handle_dm, message_text
from pipeline.intake.channel import ChannelListener, is_usable, message_fields
from pipeline.models import Status

OPERATOR = 424242
STRANGER = 999999


def _message(user_id=OPERATOR, text="A real news item worth posting about",
             message_id=1, chat_id=555, caption=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        text=text,
        caption=caption,
    )


# ------------------------------------------------------------ authorisation


def test_is_operator_matches_only_the_configured_id():
    assert is_operator(_message(user_id=OPERATOR), OPERATOR)
    assert not is_operator(_message(user_id=STRANGER), OPERATOR)


def test_is_operator_rejects_events_with_no_user():
    assert not is_operator(SimpleNamespace(), OPERATOR)
    assert not is_operator(SimpleNamespace(from_user=None), OPERATOR)


def test_is_operator_is_false_when_operator_id_is_unset():
    """An unconfigured operator id must never mean 'allow everyone'."""
    assert not is_operator(_message(user_id=0), 0)
    assert not is_operator(_message(user_id=STRANGER), 0)


async def test_operator_only_blocks_a_stranger_silently(settings):
    calls = []

    @operator_only(settings)
    async def handler(event):
        calls.append(event)
        return "ran"

    assert await handler(_message(user_id=STRANGER)) is None
    assert calls == [], "handler body must never run for a stranger"


async def test_operator_only_allows_the_operator(settings):
    @operator_only(settings)
    async def handler(event):
        return "ran"

    assert await handler(_message(user_id=OPERATOR)) == "ran"


# --------------------------------------------------------------- DM intake


async def test_dm_from_stranger_creates_no_item(db, settings):
    """DM bypasses triage, so an unauthorised DM would be a direct path in."""

    @operator_only(settings)
    async def handler(message):
        return await handle_dm(message, db, settings)

    await handler(_message(user_id=STRANGER, text="inject me"))
    assert await db.list_by_status(Status.INGESTED) == []


async def test_dm_from_operator_creates_an_item(db, settings):
    item_id = await handle_dm(_message(), db, settings)

    item = await db.get_item(item_id)
    assert item.source == "dm"
    assert item.status == Status.INGESTED


async def test_dm_acknowledges_the_operator(db, settings):
    replies = []
    await handle_dm(_message(), db, settings, reply=lambda t: _collect(replies, t))
    assert replies and "researching" in replies[0]


async def test_duplicate_dm_is_acknowledged_not_reingested(db, settings):
    replies = []
    await handle_dm(_message(message_id=7), db, settings)
    second = await handle_dm(
        _message(message_id=7), db, settings, reply=lambda t: _collect(replies, t)
    )
    assert second is None
    assert "Already have" in replies[0]


async def test_empty_dm_is_rejected(db, settings):
    replies = []
    result = await handle_dm(
        _message(text=""), db, settings, reply=lambda t: _collect(replies, t)
    )
    assert result is None
    assert "Nothing to work with" in replies[0]


async def test_dm_media_download_failure_still_ingests_the_text(db, settings):
    async def failing_download(message, target):
        raise RuntimeError("telegram hiccup")

    item_id = await handle_dm(_message(), db, settings, download=failing_download)
    assert item_id is not None, "text must survive a media failure"


def test_message_text_prefers_text_then_caption():
    assert message_text(_message(text="body")) == "body"
    assert message_text(_message(text=None, caption="cap")) == "cap"
    assert message_text(_message(text=None, caption=None)) == ""


async def _collect(sink, text):
    sink.append(text)


# ---------------------------------------------------------- channel intake


def test_message_fields_reads_chat_id_from_peer_when_absent():
    msg = SimpleNamespace(
        id=12, text="hi", chat_id=None,
        peer_id=SimpleNamespace(channel_id=-100777),
    )
    assert message_fields(msg) == {
        "source_chat_id": -100777, "source_msg_id": 12, "raw_text": "hi",
    }


def test_is_usable_accepts_text_or_media_and_rejects_neither():
    assert is_usable(SimpleNamespace(text="something", media=None))
    assert is_usable(SimpleNamespace(text="", media=object()))
    assert not is_usable(SimpleNamespace(text="  ", media=None))


class FakeTelethon:
    def __init__(self, messages):
        self._messages = messages

    async def get_messages(self, chat_id, limit=20):
        return list(self._messages)

    async def download_media(self, message, target):
        return None


def _tg_message(msg_id, text="A channel post about an AI model release"):
    return SimpleNamespace(id=msg_id, text=text, chat_id=-1001526709058, media=None)


async def test_backfill_ingests_only_messages_newer_than_the_high_water_mark(db, settings):
    await db.insert_item(
        source="channel", source_chat_id=-1001526709058, source_msg_id=5, raw_text="old"
    )
    client = FakeTelethon([_tg_message(i) for i in (7, 6, 5, 4)])

    ingested = await ChannelListener(db, client, settings).backfill()

    assert ingested == 2
    stored = sorted(i.source_msg_id for i in await db.list_by_status(Status.INGESTED))
    assert stored == [5, 6, 7]


async def test_backfill_is_safe_to_run_repeatedly(db, settings):
    """Restart replay must not duplicate. This is what the UNIQUE index is for."""
    client = FakeTelethon([_tg_message(i) for i in (3, 2, 1)])
    listener = ChannelListener(db, client, settings)

    first = await listener.backfill()
    second = await listener.backfill()

    assert (first, second) == (3, 0)


async def test_backfill_survives_an_unreachable_channel(db, settings):
    class Broken(FakeTelethon):
        async def get_messages(self, chat_id, limit=20):
            raise ConnectionError("channel unreachable")

    assert await ChannelListener(db, Broken([]), settings).backfill() == 0


async def test_ingest_skips_messages_with_neither_text_nor_media(db, settings):
    listener = ChannelListener(db, FakeTelethon([]), settings)
    assert await listener.ingest(SimpleNamespace(id=1, text="", media=None,
                                                 chat_id=-100)) is None


# ------------------------------------------------- commands and thin input


async def test_start_command_is_answered_not_ingested(db, settings):
    """Regression: /start was ingested as content and produced a fully
    researched 6-slide carousel about Meta model releases — a confident,
    sourced-looking post built from a message containing no story at all."""
    replies = []
    result = await handle_dm(
        _message(text="/start"), db, settings, reply=lambda t: _collect(replies, t)
    )

    assert result is None
    assert await db.list_by_status(Status.INGESTED) == []
    assert "Ready" in replies[0]


async def test_help_command_is_answered_not_ingested(db, settings):
    replies = []
    await handle_dm(_message(text="/help"), db, settings,
                    reply=lambda t: _collect(replies, t))
    assert await db.list_by_status(Status.INGESTED) == []
    assert "research" in replies[0].lower()


async def test_unknown_command_is_rejected_not_ingested(db, settings):
    replies = []
    await handle_dm(_message(text="/frobnicate"), db, settings,
                    reply=lambda t: _collect(replies, t))
    assert await db.list_by_status(Status.INGESTED) == []
    assert "Unknown command" in replies[0]


async def test_command_with_bot_suffix_is_recognised(db, settings):
    """Group chats deliver /start@news_pi_ai_bot."""
    replies = []
    await handle_dm(_message(text="/start@news_pi_ai_bot"), db, settings,
                    reply=lambda t: _collect(replies, t))
    assert await db.list_by_status(Status.INGESTED) == []
    assert "Ready" in replies[0]


async def test_too_thin_text_is_refused(db, settings):
    """Research always finds something, so an empty prompt manufactures a story."""
    replies = []
    result = await handle_dm(_message(text="ai news"), db, settings,
                             reply=lambda t: _collect(replies, t))

    assert result is None
    assert await db.list_by_status(Status.INGESTED) == []
    assert "too short" in replies[0]


async def test_thin_text_with_an_image_is_accepted(db, settings):
    """A screenshot carries its own substance; the caption need not."""
    async def download(message, target):
        return ["/tmp/shot.png"]

    result = await handle_dm(_message(text="this"), db, settings, download=download)
    assert result is not None


async def test_a_real_message_still_gets_through(db, settings):
    result = await handle_dm(
        _message(text="OpenAI ships GPT-5.5 to all Plus subscribers today"),
        db, settings,
    )
    assert result is not None
