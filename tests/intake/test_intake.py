"""Intake and authorisation.

The authorisation tests guard the only path from a stranger to the operator's
Instagram account, so they are the most important tests in the suite.
"""

import asyncio
import dataclasses
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.approval.auth import is_operator, operator_only
from pipeline.approval.bot import Pending, register_approval
from pipeline.intake.bot_intake import (
    ACK, EMPTY_ACK, MENU, SPLIT_MARGIN_CHARS, dm_downloader, handle_dm, message_text,
)
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


async def test_start_lists_only_commands_that_work(db, settings):
    """/start advertised /status, which nothing handled ("Unknown command")."""
    replies = []
    await handle_dm(_message(text="/start"), db, settings,
                    reply=lambda t: _collect(replies, t))
    assert "/status" not in replies[0]
    assert all(f"/{name}" in replies[0] for name, _ in MENU)


async def test_channels_command_replies_with_the_watched_channels(db, settings):
    replies = []

    async def channels():
        return "I watch these channels:\n• The Hacker News — -1001"

    await handle_dm(_message(text="/channels@news_pi_ai_bot"), db, settings,
                    reply=lambda t: _collect(replies, t), channels=channels)
    assert replies == ["I watch these channels:\n• The Hacker News — -1001"]
    assert await db.list_by_status(Status.INGESTED) == []


async def test_channels_reaches_the_listener_through_the_bot(db, settings):
    replies = []
    message = _message(text="/channels")
    message.answer = lambda t: _collect(replies, t)

    async def channels():
        return "listed"

    _, on_message = register_approval(None, db, settings, SimpleNamespace(), Pending(),
                                      channels=channels)
    await on_message(message)
    assert replies == ["listed"]


async def test_a_command_is_not_taken_as_a_waiting_items_answer(db, settings):
    """T378 review: with one item waiting on a question, /channels resumed it
    with "/channels" as the operator's steer and the list was never sent."""
    from pipeline.conversation import handle_answer

    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=77, raw_text="x")
    await db.transition(i, Status.NEEDS_INPUT, {"question": "Which one?"})
    replies = []
    message = _message(text="/channels")
    message.answer = lambda t: _collect(replies, t)

    async def channels():
        return "listed"

    assert await handle_answer(message, db, settings) is None
    _, on_message = register_approval(None, db, settings, SimpleNamespace(), Pending(),
                                      channels=channels)
    await on_message(message)
    assert replies == ["listed"]
    item = await db.get_item(i)
    assert item.status == Status.NEEDS_INPUT and not item.answer


async def test_post_with_nothing_waiting_says_so(db, settings):
    replies = []
    await handle_dm(_message(text="/post"), db, settings, reply=lambda t: _collect(replies, t))
    assert replies == ["Nothing is waiting on a question right now."]


async def test_describe_names_each_channel_and_flags_an_unreachable_one(db):
    class Client:
        async def get_entity(self, chat_id):
            if chat_id == -1002:
                raise ValueError("Could not find the input entity")
            return SimpleNamespace(title="The Hacker News")

    listener = ChannelListener(db, Client(), SimpleNamespace(channel_ids=(-1001, -1002)))
    text = await listener.describe()
    assert "The Hacker News — -1001" in text
    assert "name unavailable" in text and "-1002" in text
    empty = ChannelListener(db, Client(), SimpleNamespace(channel_ids=()))
    assert "CHANNEL_IDS is empty" in await empty.describe()


async def test_the_command_menu_is_set_and_a_failure_is_not_fatal(caplog):
    from pipeline.__main__ import set_command_menu

    sent = []

    class Bot:
        async def set_my_commands(self, commands):
            sent.extend(commands)

    await set_command_menu(Bot())
    assert [(c.command, c.description) for c in sent] == list(MENU)

    class DownBot:
        async def set_my_commands(self, commands):
            raise ConnectionError("down")

    await set_command_menu(DownBot())
    assert "command menu" in caplog.text


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


# --- long requests arrive in pieces -----------------------------------------
#
# Telegram caps a text message at 4096 characters and a media caption at 1024.
# A longer request is split by the sending client into separate messages, each
# with its own id — so each became its own item and was researched on a
# fragment, with nothing saying so. Only a fragment sitting exactly on a
# ceiling can have more coming; ordinary back-to-back messages stay separate.

TEXT_CAP = 4096
CAPTION_CAP = 1024


async def test_back_to_back_messages_are_separate_items(db, settings):
    """T364: the operator just wants to send messages and get things done."""
    replies = []
    first = await handle_dm(_message(text="a" * 60, message_id=1), db, settings,
                            reply=lambda t: _collect(replies, t))
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings,
                             reply=lambda t: _collect(replies, t))

    assert first is not None and second is not None and second != first
    assert (await db.get_item(first)).raw_text == "a" * 60
    assert (await db.get_item(second)).raw_text == "b" * 60
    assert len(replies) == 2 and all("researching" in r for r in replies)
    assert not any("/new" in r for r in replies)


async def test_a_message_split_at_the_text_ceiling_is_joined(db, settings):
    replies = []
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    second = await handle_dm(
        _message(text="b" * 60, message_id=2), db, settings,
        reply=lambda t: _collect(replies, t))

    assert second == first, "the rest lands on the same item"
    item = await db.get_item(first)
    assert "a" * TEXT_CAP in item.raw_text and "b" * 60 in item.raw_text
    assert replies == ["Got the rest of that — researching it together."]
    assert not any("/new" in r for r in replies)


async def test_a_fragment_split_just_under_the_ceiling_is_joined(db, settings):
    """Clients split at a word/paragraph break, so fragments fall a little short."""
    first = await handle_dm(
        _message(text="a" * (TEXT_CAP - SPLIT_MARGIN_CHARS), message_id=1), db, settings)
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert SPLIT_MARGIN_CHARS == 300 and second == first


async def test_just_outside_the_split_margin_is_not_joined(db, settings):
    first = await handle_dm(
        _message(text="a" * (TEXT_CAP - SPLIT_MARGIN_CHARS - 1), message_id=1),
        db, settings)
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert second != first


async def test_a_three_part_split_joins_while_each_part_hits_the_ceiling(db, settings):
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    mid = await handle_dm(_message(text="b" * TEXT_CAP, message_id=2), db, settings)
    last = await handle_dm(_message(text="c" * 60, message_id=3), db, settings)
    after = await handle_dm(_message(text="d" * 60, message_id=4), db, settings)

    assert mid == first and last == first
    assert after != first, "a short tail closes the split"
    assert "c" * 60 in (await db.get_item(first)).raw_text


async def test_text_after_a_capped_caption_is_joined(db, settings):
    async def download(message, target):
        return ["/tmp/shot.png"]

    message = _message(text=None, message_id=1)
    message.caption = "c" * CAPTION_CAP
    first = await handle_dm(message, db, settings, download=download)
    second = await handle_dm(_message(text="d" * 60, message_id=2), db, settings)

    assert second == first


async def test_a_long_caption_under_the_ceiling_is_not_joined(db, settings):
    async def download(message, target):
        return ["/tmp/shot.png"]

    message = _message(text=None, message_id=1)
    message.caption = "c" * (CAPTION_CAP - 1)
    first = await handle_dm(message, db, settings, download=download)
    second = await handle_dm(_message(text="d" * 60, message_id=2), db, settings)

    assert second != first


async def test_a_text_dm_of_caption_length_is_not_a_capped_caption(db, settings):
    """1024 chars is only a ceiling for captions; as plain text it is ordinary."""
    first = await handle_dm(_message(text="a" * CAPTION_CAP, message_id=1), db, settings)
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert second != first


async def test_an_image_dm_after_a_capped_message_is_its_own_item(db, settings):
    async def download(message, target):
        return ["/tmp/shot.png"]

    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings,
                             download=download)

    assert second != first
    assert (await db.get_item(first)).raw_text == "a" * TEXT_CAP


async def test_an_item_researched_mid_join_is_not_touched(db, settings, monkeypatch):
    """Race: the item moves on between continuable_dm and the append."""
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    real = db.continuable_dm

    async def stale(chat_id, within_s):
        found = await real(chat_id, within_s)
        await db.transition(first, Status.RESEARCHED)
        return found

    monkeypatch.setattr(db, "continuable_dm", stale)
    replies = []
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings,
                             reply=lambda t: _collect(replies, t))

    assert second is not None and second != first
    original = await db.get_item(first)
    assert original.raw_text == "a" * TEXT_CAP and original.status == Status.RESEARCHED
    assert "researching" in replies[0] and "rest of that" not in replies[0]


async def test_a_split_after_the_window_is_a_new_item(db, settings):
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    await db.conn.execute(
        "UPDATE items SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (first,))
    await db.conn.commit()
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert second != first


async def test_a_continuation_resets_the_item_to_ingested(db, settings):
    """The stages that already ran saw only half the request."""
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    await db.transition(first, Status.TRIAGED)
    await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert (await db.get_item(first)).status == Status.INGESTED


async def test_a_redelivered_message_is_a_duplicate_not_a_continuation(db, settings):
    replies = []
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=7), db, settings)
    second = await handle_dm(
        _message(text="a" * TEXT_CAP, message_id=7), db, settings,
        reply=lambda t: _collect(replies, t))

    assert second is None
    assert "Already have" in replies[0]
    assert (await db.get_item(first)).raw_text == "a" * TEXT_CAP, "not appended to itself"


async def test_an_item_past_research_starts_a_new_request(db, settings):
    """Appending to something already researched would silently invalidate it."""
    first = await handle_dm(_message(text="a" * TEXT_CAP, message_id=1), db, settings)
    await db.transition(first, Status.RESEARCHED)
    second = await handle_dm(_message(text="b" * 60, message_id=2), db, settings)

    assert second != first


async def test_a_capped_caption_warns_that_text_was_lost(db, settings):
    replies = []
    message = _message(text=None, message_id=1)
    message.caption = "c" * 1024
    await handle_dm(message, db, settings, reply=lambda t: _collect(replies, t))

    assert any("never reached me" in r for r in replies)


async def test_an_ordinary_caption_does_not_warn(db, settings):
    replies = []
    message = _message(text=None, message_id=1)
    message.caption = "a short caption about the attached screenshot"
    await handle_dm(message, db, settings, reply=lambda t: _collect(replies, t))

    assert not any("never reached me" in r for r in replies)


# ------------------------------------------------------------ DM images


class FakeBot:
    """Stands in for aiogram's Bot.download: writes the file it was asked for."""

    def __init__(self):
        self.downloaded = []

    async def download(self, file, destination):
        self.downloaded.append(file.file_id)
        destination.write_bytes(b"\xff\xd8 fake jpeg")


def _photo_message(caption=None, message_id=7):
    sizes = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
    return SimpleNamespace(**{**vars(_message(text=None, caption=caption, message_id=message_id)),
                              "photo": sizes, "document": None})


def _file_message(mime_type, message_id=8):
    doc = SimpleNamespace(file_id="doc", mime_type=mime_type)
    return SimpleNamespace(**{**vars(_message(text=None, message_id=message_id)),
                              "photo": None, "document": doc})


async def test_a_photo_without_a_caption_becomes_an_item(db, settings, tmp_path):
    """The bug: an image-only DM was answered "Nothing to work with there"."""
    settings = dataclasses.replace(settings, media_dir=str(tmp_path))
    bot = FakeBot()
    replies = []

    async def reply(text):
        replies.append(text)

    item_id = await handle_dm(_photo_message(), db, settings,
                              download=dm_downloader(bot), reply=reply)

    assert item_id is not None and replies == [ACK]
    assert bot.downloaded == ["large"]  # the biggest size, not the thumbnail
    (path,) = (await db.get_item(item_id)).raw_media_paths
    assert path.endswith("555_7.jpg") and Path(path).is_file()


async def test_an_image_sent_as_a_file_keeps_its_type(db, settings, tmp_path):
    settings = dataclasses.replace(settings, media_dir=str(tmp_path))
    item_id = await handle_dm(_file_message("image/png"), db, settings,
                              download=dm_downloader(FakeBot()))
    assert (await db.get_item(item_id)).raw_media_paths[0].endswith("555_8.png")


async def test_a_non_image_file_is_not_downloaded(db, settings, tmp_path):
    settings = dataclasses.replace(settings, media_dir=str(tmp_path))
    bot = FakeBot()
    replies = []

    async def reply(text):
        replies.append(text)

    assert await handle_dm(_file_message("application/pdf"), db, settings,
                           download=dm_downloader(bot), reply=reply) is None
    assert bot.downloaded == [] and replies == [EMPTY_ACK]


async def test_the_live_handler_downloads_dm_images(db, settings, tmp_path):
    """Through register_approval, the handler the running bot actually uses."""
    settings = dataclasses.replace(settings, media_dir=str(tmp_path))
    bot = FakeBot()
    _, on_message = register_approval(None, db, settings, bot, Pending())
    message = _photo_message()
    message.answer = lambda text: asyncio.sleep(0)

    item_id = await on_message(message)

    assert bot.downloaded == ["large"]
    assert len((await db.get_item(item_id)).raw_media_paths) == 1


# ------------------------------------------------------- T375: channel videos


async def test_a_channel_video_is_stored_as_a_still(db, settings, tmp_path, monkeypatch):
    """Items 686-691: the .mp4 went to vision, failed, and the post lost its picture."""
    import pipeline.intake.channel as channel_mod

    settings = dataclasses.replace(settings, media_dir=str(tmp_path))
    clip = tmp_path / "channel" / "123.mp4"

    class VideoClient(FakeTelethon):
        async def download_media(self, message, target):
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"video")
            return str(clip)

    async def fake_frame(video):
        frame = video.with_suffix(".jpg")
        frame.write_bytes(b"jpeg")
        return frame

    monkeypatch.setattr(channel_mod, "video_frame", fake_frame)
    message = SimpleNamespace(**{**vars(_tg_message(9)), "media": object()})
    item_id = await ChannelListener(db, VideoClient([]), settings).ingest(message)

    assert (await db.get_item(item_id)).raw_media_paths == [str(clip.with_suffix(".jpg"))]
    assert not clip.exists(), "the clip is not kept once its still is taken"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_video_frame_extracts_a_jpeg(tmp_path):
    from pipeline.intake.channel import video_frame

    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=320x240:d=1", str(clip)], check=True)
    frame = await video_frame(clip)
    assert frame == tmp_path / "c.jpg" and frame.read_bytes()[:2] == b"\xff\xd8"
    assert await video_frame(tmp_path / "missing.mp4") is None
