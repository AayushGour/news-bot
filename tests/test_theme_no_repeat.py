"""T358: no two consecutive Instagram posts share a theme.

Fake renderer / uploader / Telegram throughout: no Chromium, no network, tmp DBs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.publish.instagram as ig
from pipeline.db import Database
from pipeline.errors import Recompose, Retryable, Retryforever
from pipeline.models import Item, Status
from pipeline.publish import theme_guard
from pipeline.publish.theme_guard import KEPT_NOTE, RETHEMED_NOTE, ensure_distinct_theme
from pipeline.requeue import fields_to_clear
from pipeline.stages import render as render_mod
from pipeline.stages.render import (
    TEXT_ONLY_THEMES,
    available_themes,
    render,
    resolve_theme_path,
)

THEMES = available_themes()
HOOK = [{"type": "hook", "headline": "x"}]
PHOTO = [{"type": "hook", "headline": "x"}, {"type": "photo", "headline": "y"}]


def _item(item_id=1, slides=HOOK, **kw) -> Item:
    return Item(id=item_id, source="channel", status=Status.COMPOSED, slides=slides, **kw)


async def _published(db, theme, minutes_ago=5) -> int:
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=hash((theme, minutes_ago)) % 10**9,
                             raw_text=f"{theme}{minutes_ago}")
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    await db.update_fields(i, {"rendered_theme": theme, "published_at": when})
    return i


@pytest.fixture
def fake_worker(monkeypatch):
    async def run(html_path, out_dir, count, prefix):
        return {"paths": [f"{out_dir}/{prefix}-{n}.png" for n in range(count)]}

    monkeypatch.setattr(render_mod, "_run_worker", run)


def _s(settings, tmp_path, theme):
    return replace(settings, theme=theme, media_dir=tmp_path / "m",
                   template_dir=render_mod.THEMES_DIR.parents[1] / "templates")


# ------------------------------------------------------------- pick (render)


def test_there_are_enough_themes_for_the_tests():
    assert len(THEMES) >= 3


@pytest.mark.parametrize("name", ["random", "rotate", ""])
def test_avoided_theme_is_never_picked_and_pick_is_stable(name):
    for avoided in THEMES:
        for item_id in range(1, 80):
            a = resolve_theme_path(name, item_id=item_id, avoid=frozenset({avoided}))
            b = resolve_theme_path(name, item_id=item_id, avoid=frozenset({avoided}))
            assert a == b
            assert a.stem != avoided


def test_avoid_never_empties_the_set(tmp_path):
    (tmp_path / "only.json").write_text("{}")
    assert resolve_theme_path("random", tmp_path, 3, avoid=frozenset({"only"})).stem == "only"
    assert resolve_theme_path("rotate", tmp_path, 3, avoid=frozenset({"only"})).stem == "only"


def test_avoid_is_dropped_before_the_text_only_exclusion(tmp_path):
    (tmp_path / "doodle.json").write_text("{}")
    (tmp_path / "a.json").write_text("{}")
    got = resolve_theme_path("random", tmp_path, 1, exclude=TEXT_ONLY_THEMES, avoid=frozenset({"a"}))
    assert got.stem == "a"  # not doodle, even though a was the avoided one


def test_a_named_theme_ignores_avoid():
    assert resolve_theme_path("signal", avoid=frozenset({"signal"})).stem == "signal"


async def test_render_records_theme_and_avoids_last_published(db, settings, tmp_path, fake_worker):
    cfg = _s(settings, tmp_path, "random")
    for item_id in range(1001, 1040):
        natural = (await render(_item(item_id), cfg))["rendered_theme"]
        await _published(db, natural, minutes_ago=2000 - item_id)
        out = await render(_item(item_id), cfg, db=db)
        assert out["rendered_theme"] != natural
        assert out["rendered_theme"] in THEMES
        assert out["rendered_paths"]


async def test_render_with_nothing_published_is_unchanged(db, settings, tmp_path, fake_worker):
    cfg = _s(settings, tmp_path, "random")
    plain = await render(_item(5), cfg)
    assert (await render(_item(5), cfg, db=db)) == plain


async def test_pin_and_named_theme_win_over_last_published(db, settings, tmp_path, fake_worker):
    await _published(db, "signal")
    pinned = await render(_item(3, theme_pin="signal"), _s(settings, tmp_path, "random"), db=db)
    assert pinned["rendered_theme"] == "signal"
    named = await render(_item(3), _s(settings, tmp_path, "signal"), db=db)
    assert named["rendered_theme"] == "signal"


async def test_photo_deck_still_avoids_doodle(db, settings, tmp_path, fake_worker):
    cfg = _s(settings, tmp_path, "random")
    for item_id in range(1001, 1060):
        await _published(db, THEMES[item_id % len(THEMES)], minutes_ago=2000 - item_id)
        out = await render(_item(item_id, PHOTO), cfg, db=db)
        assert out["rendered_theme"] not in TEXT_ONLY_THEMES


# --------------------------------------------------------------------- db


async def test_last_published_theme_is_the_most_recent_and_skips_self(db):
    assert await db.last_published_theme() is None
    older = await _published(db, "aurora", 30)
    newer = await _published(db, "tidal", 10)
    assert await db.last_published_theme() == "tidal"
    assert await db.last_published_theme(exclude_id=newer) == "aurora"
    assert older != newer


async def test_legacy_published_row_without_theme_means_nothing_to_avoid(db):
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=1, raw_text="x")
    await db.update_fields(i, {"published_at": datetime.now(timezone.utc).isoformat()})
    await _published(db, "aurora", 600)
    assert await db.last_published_theme() is None


async def test_unpublished_items_do_not_count(db):
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=1, raw_text="x")
    await db.update_fields(i, {"rendered_theme": "tidal"})
    assert await db.last_published_theme() is None


async def test_migration_adds_column_to_an_existing_db_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    fresh = await Database(path).connect()
    await fresh.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE items DROP COLUMN rendered_theme")
    con.execute("INSERT INTO items (source, created_at, status, status_updated_at, raw_text)"
                " VALUES ('c','2026-01-01','published','2026-01-01','kept')")
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    con.close()
    assert "rendered_theme" not in cols

    for _ in range(2):
        d = await Database(path).connect()
        await d.close()
    d = await Database(path).connect()
    cols = {r["name"] for r in await d.conn.execute_fetchall("PRAGMA table_info(items)")}
    assert "rendered_theme" in cols
    item = await d.get_item(1)
    assert item.raw_text == "kept" and item.rendered_theme is None
    await d.close()


def test_requeue_clears_rendered_theme_with_the_render_output():
    for target in (Status.INGESTED, Status.TRIAGED, Status.SYNTHESIZED, Status.COMPOSED):
        assert fields_to_clear(target)["rendered_theme"] is None
    assert "rendered_theme" not in fields_to_clear(Status.RENDERED)
    assert "rendered_theme" not in fields_to_clear(Status.APPROVED)


# ------------------------------------------------------------ publish guard


class Rec:
    def __init__(self, theme="tidal", exc=None):
        self.theme, self.exc = theme, exc
        self.renders, self.uploads, self.notes = [], [], []

    async def render(self, item, settings, db=None, out_dir=None):
        self.renders.append(item.id)
        # Like the real renderer: files are written BEFORE the overflow check.
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "item-0.png").write_bytes(b"NEW")
        self.scratch = out_dir
        if self.exc:
            raise self.exc
        return {"rendered_paths": [str(out_dir / "item-0.png")], "rendered_theme": self.theme}

    async def upload(self, paths, item_id, settings):
        self.uploads.append(paths)
        assert all(__import__("pathlib").Path(p).read_bytes() == b"NEW" for p in paths)
        return ["https://cdn.example/n0.png"]

    async def notify(self, text):
        self.notes.append(text)


def _leftovers(settings, item_id):
    d = _gs(settings).media_dir / str(item_id)
    return sorted(p.name for p in d.iterdir() if p.name != "item-0.png")


def _gs(settings):
    """Auto-pick settings with the media dir under the test's tmp dir."""
    return replace(settings, theme="random", media_dir=settings.db_path.parent / "media")


def _orig(settings, item_id) -> Path:
    d = _gs(settings).media_dir / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "item-0.png").write_bytes(b"OLD")
    return d / "item-0.png"


async def _guard_case(db, settings, rec, *, rendered="signal", last="signal", **item_kw):
    await _published(db, last)
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=777, raw_text="x")
    orig = _orig(settings, i)
    await db.update_fields(i, {"rendered_theme": rendered, "rendered_paths": [str(orig)],
                               "media_urls": ["https://cdn.example/o0.png"], **item_kw})
    out = await ensure_distinct_theme(
        await db.get_item(i), db=db, settings=_gs(settings), notify=rec.notify,
        render_fn=rec.render, upload_fn=rec.upload)
    return out, await db.get_item(i)


async def test_guard_swaps_when_equal_and_notes_once(db, settings):
    rec = Rec()
    out, row = await _guard_case(db, settings, rec)
    assert out == ["https://cdn.example/n0.png"]
    final = _gs(settings).media_dir / str(row.id) / "item-0.png"
    assert (row.rendered_theme, row.rendered_paths, row.media_urls) == (
        "tidal", [str(final)], ["https://cdn.example/n0.png"])
    assert final.read_bytes() == b"NEW"
    assert _leftovers(settings, row.id) == []
    assert rec.notes == [RETHEMED_NOTE.format(id=row.id, old="signal", new="tidal")]
    assert len(rec.renders) == len(rec.uploads) == 1


async def test_guard_does_nothing_when_themes_differ(db, settings):
    rec = Rec()
    out, row = await _guard_case(db, settings, rec, rendered="aurora", last="signal")
    assert out is None and not rec.renders and not rec.notes
    assert row.rendered_theme == "aurora"


async def test_guard_leaves_legacy_untracked_items_alone(db, settings):
    rec = Rec()
    out, _ = await _guard_case(db, settings, rec, rendered=None, last="signal")
    assert out is None and not rec.renders


async def test_theme_pin_wins_no_swap(db, settings):
    rec = Rec()
    out, row = await _guard_case(db, settings, rec, theme_pin="signal")
    assert out is None and not rec.renders and not rec.notes
    assert row.rendered_theme == "signal"


async def test_named_theme_setting_wins_no_swap(db, settings):
    rec = Rec()
    await _published(db, "signal")
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=778, raw_text="x")
    await db.update_fields(i, {"rendered_theme": "signal"})
    out = await ensure_distinct_theme(
        await db.get_item(i), db=db, settings=replace(settings, theme="signal"),
        notify=rec.notify, render_fn=rec.render, upload_fn=rec.upload)
    assert out is None and not rec.renders


async def test_recompose_publishes_original_and_says_so(db, settings):
    rec = Rec(exc=Recompose(0, "too long"))
    out, row = await _guard_case(db, settings, rec)
    assert out is None and row.rendered_theme == "signal"
    # The overflowing alternative render was written to scratch and discarded.
    assert rec.scratch.name.startswith(theme_guard.SCRATCH_PREFIX) and not rec.scratch.exists()
    assert Path(row.rendered_paths[0]).read_bytes() == b"OLD"
    assert _leftovers(settings, row.id) == []
    assert rec.notes == [KEPT_NOTE.format(id=row.id, old="signal")]


async def test_render_failure_is_retryable_and_row_untouched(db, settings):
    rec = Rec(exc=Retryable("chromium died"))
    with pytest.raises(Retryable):
        await _guard_case(db, settings, rec)
    row = await db.get_item(2)
    assert Path(row.rendered_paths[0]).read_bytes() == b"OLD" and not rec.scratch.exists()
    assert row.rendered_theme == "signal" and row.media_urls == ["https://cdn.example/o0.png"]
    assert not rec.notes


async def test_upload_failure_leaves_row_unchanged_and_sends_no_note(db, settings):
    rec = Rec()

    async def boom(paths, item_id, settings):
        raise Retryable("r2 down")

    await _published(db, "signal")
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=779, raw_text="x")
    orig = _orig(settings, i)
    await db.update_fields(i, {"rendered_theme": "signal", "media_urls": ["old"],
                               "rendered_paths": [str(orig)]})
    with pytest.raises(Retryable):
        await ensure_distinct_theme(
            await db.get_item(i), db=db, settings=_gs(settings),
            notify=rec.notify, render_fn=rec.render, upload_fn=boom)
    row = await db.get_item(i)
    assert orig.read_bytes() == b"OLD" and not rec.scratch.exists()
    assert _leftovers(settings, i) == []
    assert row.rendered_theme == "signal" and row.media_urls == ["old"] and not rec.notes


async def test_same_theme_back_from_render_is_not_a_swap(db, settings):
    rec = Rec(theme="signal")
    out, row = await _guard_case(db, settings, rec)
    assert out is None and not rec.uploads and not rec.notes


async def test_note_failure_does_not_fail_the_publish(db, settings):
    rec = Rec()

    async def bad(text):
        raise RuntimeError("telegram down")

    rec.notify = bad
    out, row = await _guard_case(db, settings, rec)
    assert out and row.rendered_theme == "tidal"


def test_copy_has_no_raw_placeholders_left():
    assert "{" not in RETHEMED_NOTE.format(id=1, old="a", new="b")
    assert theme_guard.RETHEMED_NOTE.startswith("#{id}")


# ------------------------------------------- inside the serialised publish flow


@pytest.fixture
def live(settings):
    return replace(settings, dry_run=False, ig_user_id="1", ig_access_token="t",
                   r2_bucket="b", r2_public_base="https://cdn.example")


class Probe:
    """http fake: records the probed URL, then makes the host look down."""

    def __init__(self):
        self.probed = []

    async def get(self, url, **kw):
        self.probed.append(url)
        raise OSError("down")


async def _publishing_item(db, **fields):
    i = await db.insert_item(source="c", source_chat_id=-1, source_msg_id=9, raw_text="x")
    await db.transition(i, Status.PUBLISHING, {
        "caption": "c", "media_urls": ["https://cdn.example/items/9/a.png"], **fields})
    return await db.get_item(i)


async def test_hook_runs_inside_the_publish_lock_and_new_urls_are_used(db, live):
    item = await _publishing_item(db)
    seen = []

    async def hook(it):
        seen.append(ig._publish_lock.locked())
        return ["https://cdn.example/items/9/new.png"]

    http = Probe()
    with pytest.raises(Retryforever):
        await ig.publish_carousel(item, http, db, live, before_publish=hook)
    assert seen == [True]
    assert http.probed[0].endswith("/new.png")


async def test_hook_is_skipped_once_containers_exist(db, live):
    item = await _publishing_item(db, ig_child_ids=["c1"])
    called = []

    async def hook(it):
        called.append(1)

    with pytest.raises(Retryforever):
        await ig.publish_carousel(item, Probe(), db, live, before_publish=hook)
    assert not called


async def test_hook_not_reached_while_pacing(db, live, monkeypatch):
    monkeypatch.setattr(ig, "_last_publish_mono", __import__("time").monotonic())
    item = await _publishing_item(db)
    called = []

    async def hook(it):
        called.append(1)

    with pytest.raises(Retryforever):
        await ig.publish_carousel(item, Probe(), db, live, before_publish=hook)
    assert not called


# ------------------------------------------------------------------- wiring


async def test_registry_hands_render_the_db_and_publish_the_guard(db, settings, monkeypatch):
    import pipeline.__main__ as main

    seen = {}

    async def fake_render(item, settings, db=None):
        seen["render_db"] = db
        return {}

    async def fake_publish(item, http, db, settings, *, before_publish=None):
        seen["hook"] = before_publish
        return "POST"

    async def fake_guard(item, *, db, settings, notify):
        await notify("hello")
        return ["u"]

    class Bot:
        sent = []

        async def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))

    monkeypatch.setattr(main, "render", fake_render)
    monkeypatch.setattr(main, "publish_carousel", fake_publish)
    monkeypatch.setattr(main, "ensure_distinct_theme", fake_guard)
    bot = Bot()
    reg = main.build_stage_registry(db=db, llm=None, http=None, settings=settings, bot=bot)
    await reg[Status.COMPOSED][0](_item())
    assert seen["render_db"] is db
    await reg[Status.PUBLISHING][0](_item())
    assert await seen["hook"](_item()) == ["u"]
    assert bot.sent == [(settings.operator_user_id, "hello")]
