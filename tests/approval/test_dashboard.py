from types import SimpleNamespace

import pytest

from pipeline.approval import dashboard
from pipeline.approval.bot import Pending, register_approval
from pipeline.limits import TELEGRAM_CALLBACK_DATA_LIMIT
from pipeline.models import Status

from .test_bot import OPERATOR, STRANGER, FakeBot, _message


class Callback:
    def __init__(self, data, user_id=OPERATOR):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.toasts, self.edits = [], []
        self.message = SimpleNamespace(edit_text=self._edit)

    async def _edit(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text=None, show_alert=False):
        self.toasts.append(text)


async def _item(db, status, n=1, source="channel", **fields):
    i = await db.insert_item(source=source, source_chat_id=-1001526709058 if source == "channel" else OPERATOR,
                             source_msg_id=n, raw_text=f"story number {n}\nmore")
    if status != Status.INGESTED:
        await db.transition(i, status, fields)
    return i


def _buttons(rows):
    return [b for row in rows for b in row]


def _data(rows):
    return [b.get("callback_data") for b in _buttons(rows)]


async def test_tabs_count_each_group(db):
    await _item(db, Status.TRIAGED, 1)
    await _item(db, Status.AWAITING_APPROVAL, 2, approval_msg_id=50)
    await _item(db, Status.NEEDS_INPUT, 3, question_msg_id=51)
    await _item(db, Status.PUBLISHED, 4)
    for n, status in enumerate((Status.REJECTED, Status.DROPPED, Status.FAILED), 5):
        await _item(db, status, n)

    text, rows = await dashboard.build(db)

    assert "⏳ Processing 1 · 🕓 Pending 2 · ✅ Complete 1 · ❌ Rejected 3" in text
    assert [b["text"] for b in rows[0]] == ["⏳ 1", "• 🕓 2", "✅ 1", "❌ 3"]


async def test_page_lists_newest_first_with_status_and_title(db):
    old = await _item(db, Status.AWAITING_APPROVAL, 1, approval_msg_id=50)
    new = await _item(db, Status.NEEDS_INPUT, 2, question_msg_id=51)

    _, rows = await dashboard.build(db, "p")

    labels = [b["text"] for b in _buttons(rows[1:-1])]
    assert labels == [f"#{new} · question · story number 2", f"#{old} · preview · story number 1"]
    assert rows[1][0]["callback_data"] == f"sj:{new}"


async def test_paging_moves_and_stops_at_the_ends(db):
    for n in range(dashboard.PAGE_SIZE + 1):
        await _item(db, Status.PUBLISHED, n + 1)

    text, rows = await dashboard.build(db, "c", 0)
    assert "Page 1 of 2" in text and len(rows) == 2 + dashboard.PAGE_SIZE
    assert [b["text"] for b in rows[-1]] == ["🔄", "▶️"]

    text, rows = await dashboard.build(db, "c", 1)
    assert "Page 2 of 2" in text and len(rows) == 3
    assert [b["text"] for b in rows[-1]] == ["◀️", "🔄"]


async def test_page_past_the_end_shows_the_last_page(db):
    """Items moved since the page was drawn: a stale ▶️ must not show an empty page."""
    await _item(db, Status.PUBLISHED, 1)
    text, rows = await dashboard.build(db, "c", 7)
    assert "Page 1 of 1" in text and len(rows) == 3


async def test_empty_tab_says_so(db):
    text, rows = await dashboard.build(db, "r")
    assert "Nothing here." in text and len(rows) == 2


async def test_channel_post_without_a_bot_message_links_to_the_channel(db):
    i = await _item(db, Status.TRIAGED, 10700)
    _, rows = await dashboard.build(db, "w")
    assert rows[1][0] == {"text": f"#{i} · triaged · story number 10700",
                          "url": "https://t.me/c/1526709058/10700"}


async def test_dm_without_a_preview_jumps_to_the_dm_itself(db):
    i = await _item(db, Status.TRIAGED, 7219, source="dm")
    assert dashboard.anchor(await db.get_item(i)) == 7219


async def test_preview_beats_question_beats_dm(db):
    i = await _item(db, Status.AWAITING_APPROVAL, 5, source="dm", approval_msg_id=50, question_msg_id=40)
    assert dashboard.anchor(await db.get_item(i)) == 50
    await db.transition(i, Status.NEEDS_INPUT, {"approval_msg_id": None})
    assert dashboard.anchor(await db.get_item(i)) == 40


def test_long_title_is_cut():
    item = SimpleNamespace(caption=None, raw_text="word " * 30)
    assert len(dashboard.title(item)) == dashboard.TITLE_CHARS and dashboard.title(item).endswith("…")
    exact = "x" * dashboard.TITLE_CHARS
    assert dashboard.title(SimpleNamespace(caption=exact, raw_text="")) == exact


@pytest.mark.parametrize("data", [
    "st:x:0", "st:p", "st:p:-1", "st:p:1:2", "sj:", "sj:abc", "sj:" + "9" * 19, "sj:１", "approve:3", "",
])
def test_malformed_data_is_not_ours(data):
    assert dashboard.parse(data) is None


def test_largest_callback_data_fits_telegram_and_parses_back():
    biggest = (f"{dashboard.JUMP}:{10 ** 18 - 1}", f"{dashboard.VIEW}:w:{10 ** 18 - 1}")
    assert all(len(d.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT for d in biggest)
    assert [dashboard.parse(d)[2] for d in biggest] == [10 ** 18 - 1] * 2


# ------------------------------------------------------------- through the bot


async def _wired(db, settings):
    bot = FakeBot()
    on_callback, on_message = register_approval(None, db, settings, bot, Pending())
    return bot, on_callback, on_message


async def test_status_command_sends_the_dashboard(db, settings):
    bot, _, on_message = await _wired(db, settings)
    await _item(db, Status.PUBLISHED, 1)

    assert await on_message(_message("/status")) == dashboard.STATUS
    assert bot.messages[0].text.startswith("📊 Posts")
    assert bot.messages[0].reply_markup is not None
    assert await db.list_by_status(Status.INGESTED) == []


async def test_status_does_not_consume_a_pending_caption_prompt(db, settings):
    pending = Pending()
    bot = FakeBot()
    _, on_message = register_approval(None, db, settings, bot, pending)
    pending.set(OPERATOR, "caption", 5)

    await on_message(_message("/status"))

    assert pending.pop(OPERATOR) == ("caption", 5)


async def test_tab_button_edits_the_dashboard_in_place(db, settings):
    bot, on_callback, _ = await _wired(db, settings)
    await _item(db, Status.PUBLISHED, 1)
    cb = Callback("st:c:0")

    assert await on_callback(cb) == dashboard.VIEW
    assert cb.edits[0][0].startswith("📊 Posts — ✅ Complete") and bot.messages == []


async def test_jump_replies_to_the_posts_message(db, settings):
    bot, on_callback, _ = await _wired(db, settings)
    i = await _item(db, Status.AWAITING_APPROVAL, 1, approval_msg_id=4321)

    assert await on_callback(Callback(f"sj:{i}")) == dashboard.JUMP
    assert bot.messages[0].reply_to_message_id == 4321
    assert f"#{i}" in bot.messages[0].text


async def test_jump_to_a_deleted_message_says_so(db, settings):
    bot, on_callback, _ = await _wired(db, settings)
    i = await _item(db, Status.AWAITING_APPROVAL, 1, approval_msg_id=4321)

    async def gone(**_):
        raise RuntimeError("message to be replied not found")
    bot.send_message = gone
    cb = Callback(f"sj:{i}")

    await on_callback(cb)
    assert "no longer" in cb.toasts[0]


async def test_jump_with_nothing_to_jump_to_says_so(db, settings):
    _, on_callback, _ = await _wired(db, settings)
    cb = Callback("sj:999")
    await on_callback(cb)
    assert "No message" in cb.toasts[0]


async def test_stranger_cannot_open_the_dashboard(db, settings):
    bot, on_callback, on_message = await _wired(db, settings)
    await _item(db, Status.PUBLISHED, 1)

    await on_message(_message("/status", user_id=STRANGER))
    cb = Callback("st:c:0", user_id=STRANGER)
    await on_callback(cb)

    assert bot.messages == [] and cb.edits == []


async def test_approval_buttons_still_route(db, settings):
    bot, on_callback, _ = await _wired(db, settings)
    i = await _item(db, Status.AWAITING_APPROVAL, 1, approval_msg_id=50)
    assert await on_callback(Callback(f"approve:{i}")) == "approve"
