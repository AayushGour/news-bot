"""UX-2: the Overview page. Pure chart helpers, the read-only aggregation (seeded temp DB), and
the rendered page: empty DB, hostile text, per-card failure, and `/` delegation to the list."""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.config import IG_MIN_PUBLISH_GAP_S
from pipeline.models import Status

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dashboard.py"
XSS = "<script>alert('x')</script>"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def dash():
    spec = importlib.util.spec_from_file_location("dashboard_ux2_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRequest:
    def __init__(self, db, query=None):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=False)}
        self.match_info = {}
        self.query = query or {}
        self.headers = {}


def markup(html: str) -> str:
    return re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)


_MSG = iter(range(1, 100_000))


async def make(db, status=Status.AWAITING_APPROVAL, text="news", **fields):
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=next(_MSG),
                             raw_text=text)
    await db.transition(i, status, fields or None)
    return i


async def sql(db, query, *params):
    await db.conn.execute(query, params)
    await db.conn.commit()


# ============================================================ chart helpers (pure)


def test_count_guards_none_negative_nan_and_junk(dash):
    assert [dash._count(v) for v in (None, -3, -1, -0.5, 0, float("nan"), float("inf"), "x", 2.9, "4", True)] \
        == [0, 0, 0, 0, 0, 0, 0, 0, 2, 4, 1]


def test_sparkline_needs_two_points(dash):
    assert dash.sparkline([]) == "" and dash.sparkline(None) == "" and dash.sparkline([5]) == ""


def test_sparkline_points_scale_to_peak_and_stay_inside_the_box(dash):
    svg = dash.sparkline([0, 5, 10])
    pts = re.search(r"points='([^']+)'", svg).group(1).split()
    ys = [float(p.split(",")[1]) for p in pts]
    assert len(pts) == 3 and ys[0] > ys[1] > ys[2]          # rising series draws upward
    assert min(ys) == 3.0 and max(ys) == 25.0               # pad 3 on a 28px box
    xs = [float(p.split(",")[0]) for p in pts]
    assert xs[0] == 3.0 and xs[-1] == 93.0


@pytest.mark.parametrize("values", [[0, 0, 0], [7, 7, 7]])
def test_sparkline_flat_series_is_a_flat_line_without_dividing_by_zero(dash, values):
    ys = {p.split(",")[1] for p in re.search(r"points='([^']+)'", dash.sparkline(values)).group(1).split()}
    assert len(ys) == 1


def test_sparkline_hostile_values_are_clamped_not_rendered(dash):
    svg = dash.sparkline([None, -5, "<b>", 3])
    assert "nan" not in svg.lower() and "<b>" not in svg and "-" not in re.search(r"points='([^']+)'", svg).group(1)


def test_sparkline_is_decorative_unless_labelled(dash):
    assert "aria-hidden=true" in dash.sparkline([1, 2]) and "role=img" not in dash.sparkline([1, 2])
    labelled = dash.sparkline([1, 2], label="Published")
    assert "role=img" in labelled and "aria-label='Published: 1, 2'" in labelled and "<title>Published</title>" in labelled


def test_sparkline_unknown_tone_falls_back_never_injects(dash):
    svg = dash.sparkline([1, 2], tone="x' onload='evil")
    assert "evil" not in svg and "s-work" in svg


def test_bars_empty_and_single_point_are_not_a_chart(dash):
    assert dash.bars([], title="t") == "" and dash.bars([3], title="t") == "" and dash.bars(None, title="t") == ""


def test_bars_label_carries_every_value_total_and_peak(dash):
    svg = dash.bars([0, 2, 5], ["a", "b", "c"], title="Per day")
    assert "role=img" in svg and "<title>Per day</title>" in svg
    assert "Per day: a 0, b 2, c 5. Total 7, peak 5." in svg
    assert svg.count("<rect") == 3


def test_bars_heights_are_proportional_zero_is_flat_and_nonzero_is_visible(dash):
    svg = dash.bars([0, 1, 100], title="t")
    heights = [int(h) for h in re.findall(r"height='(\d+)'", svg)]
    assert heights[0] == 0 and heights[1] >= 3 and heights[2] == 74 and heights[1] < heights[2]


def test_bars_all_zero_or_all_equal_do_not_crash(dash):
    assert "Total 0, peak 0" in dash.bars([0, 0, 0], title="t")
    eq = [int(h) for h in re.findall(r"height='(\d+)'", dash.bars([4, 4, 4], title="t"))]
    assert len(set(eq)) == 1 and eq[0] == 74


def test_bars_escape_labels_and_pad_missing_ones(dash):
    svg = dash.bars([1, 2, 3], [XSS], title="t")
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "2: 2" in svg and "3: 3" in svg


def test_chart_titles_are_escaped_in_title_and_label(dash):
    for svg in (dash.bars([1, 2], title=XSS), dash.stacked_bar([("a", 1, "good")], title=XSS),
                dash.sparkline([1, 2], label=XSS)):
        assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_bars_negative_and_none_values_clamp_to_zero(dash):
    assert "Total 3, peak 3" in dash.bars([-9, None, 3], title="t")


def test_stacked_bar_widths_sum_to_100_and_drop_empty_parts(dash):
    svg = dash.stacked_bar([("A", 1, "good"), ("B", 3, "bad"), ("Z", 0, "work"), ("N", None, "attn")], title="Mix")
    widths = [float(w) for w in re.findall(r"width='([\d.]+)'", svg)]
    assert widths == [25.0, 75.0] and "Z:" not in svg and "N:" not in svg
    assert "Mix: A 1, B 3. Total 4." in svg and "role=img" in svg


def test_stacked_bar_nothing_to_show_is_empty_string(dash):
    assert dash.stacked_bar([], title="m") == "" and dash.stacked_bar([("a", 0, "good")], title="m") == ""
    assert dash.stacked_bar(None, title="m") == ""


def test_stacked_bar_single_part_fills_the_bar_and_escapes(dash):
    svg = dash.stacked_bar([(XSS, 9, "good")], title="m")
    assert "width='100.00'" in svg and "<script>" not in svg


def test_reason_line_first_line_truncated_and_never_a_traceback(dash):
    assert dash.reason_line(None) == dash.NO_REASON and dash.reason_line("  \n ") == dash.NO_REASON
    assert dash.reason_line("boom\nsecond") == "boom"
    tb = "Traceback (most recent call last):\n  File \"x.py\", line 1, in f\n    g()\nValueError: bad thing"
    assert dash.reason_line(tb) == "ValueError: bad thing"
    out = dash.reason_line("x" * 200, 90)
    assert len(out) == 91 and out.endswith("…")
    assert dash.reason_line("y" * 90, 90) == "y" * 90        # exactly at the limit: untouched


def test_utc_days_window_is_oldest_first_ending_today(dash):
    days = dash._utc_days(NOW)
    assert len(days) == dash.OVERVIEW_DAYS == 14
    assert days[-1] == "2026-11-07" and days[0] == "2026-10-25"


def test_hourly_cap_comes_from_config_not_a_literal(dash):
    assert dash.HOURLY_PUBLISH_CAP == 3600 // IG_MIN_PUBLISH_GAP_S


# ============================================================ overview_stats (seeded DB)


async def test_empty_db_stats_are_all_zero_and_nothing_failed(dash, db):
    s = await dash.overview_stats(db, NOW)
    assert not s.failed and s.counts == {} and s.attn == 0 and s.failed_now == 0
    assert s.attn_oldest is None and s.next_up is None and s.last_published is None
    assert s.pub_daily == [0] * 14 and s.failures_daily == [0] * 14 and s.intake_daily == [0] * 14
    assert s.waiting == [] and s.recent == [] and s.stopped_reasons == [] and s.held_count == 0


async def test_counts_and_lane_kpis_trace_to_status(dash, db):
    await make(db, Status.AWAITING_APPROVAL)
    await make(db, Status.NEEDS_INPUT)
    await make(db, Status.REVISION_CONFIRM)
    await make(db, Status.FAILED)
    await make(db, Status.TRIAGED)
    await make(db, Status.PUBLISHED)
    s = await dash.overview_stats(db)
    assert (s.attn, s.failed_now, s.running) == (3, 1, 1)
    assert dict(s.mix) == {"attn": 3, "work": 1, "good": 1, "bad": 1}


async def test_oldest_waiting_ignores_non_attn_and_orders_longest_first(dash, db):
    old = await make(db, Status.AWAITING_APPROVAL, text="old")
    new = await make(db, Status.NEEDS_INPUT, text="new")
    other = await make(db, Status.TRIAGED, text="running")
    await sql(db, "UPDATE items SET status_updated_at=? WHERE id=?", "2026-01-01T00:00:00+00:00", old)
    await sql(db, "UPDATE items SET status_updated_at=? WHERE id=?", "2025-01-01T00:00:00+00:00", other)
    s = await dash.overview_stats(db)
    assert s.attn_oldest == "2026-01-01T00:00:00+00:00"          # not the older running item
    assert [w["id"] for w in s.waiting] == [old, new]


async def test_waiting_longest_is_capped_at_five(dash, db):
    for _ in range(8):
        await make(db, Status.AWAITING_APPROVAL)
    assert len((await dash.overview_stats(db)).waiting) == 5


async def test_failed_24h_counts_events_inside_the_window_only(dash, db):
    a = await make(db, Status.FAILED)
    b = await make(db, Status.FAILED)
    await sql(db, "UPDATE events SET at=? WHERE item_id=? AND to_status='failed'",
              (NOW - timedelta(hours=25)).isoformat(), b)
    s = await dash.overview_stats(db, NOW)
    assert a and s.failed_24h == 1


async def test_next_up_is_first_in_queue_order(dash, db):
    first = await make(db, Status.TRIAGED, text="first")
    await make(db, Status.RENDERED, text="second")
    s = await dash.overview_stats(db)
    assert s.next_up == (first, "first")


async def test_published_windows_and_daily_zero_fill(dash, db):
    now = datetime.now(timezone.utc)
    recent = await make(db, Status.PUBLISHED)
    week = await make(db, Status.PUBLISHED)
    ancient = await make(db, Status.PUBLISHED)
    await sql(db, "UPDATE items SET published_at=? WHERE id=?", now.isoformat(), recent)
    await sql(db, "UPDATE items SET published_at=? WHERE id=?", (now - timedelta(days=3)).isoformat(), week)
    await sql(db, "UPDATE items SET published_at=? WHERE id=?", (now - timedelta(days=30)).isoformat(), ancient)
    s = await dash.overview_stats(db, now)
    assert (s.pub_24h, s.pub_7d) == (1, 2)
    assert len(s.pub_daily) == 14 and s.pub_daily[-1] == 1 and sum(s.pub_daily) == 2   # the 30d-old one is outside the chart
    assert s.last_published == now


async def test_failures_and_intake_per_day_zero_filled(dash, db):
    await make(db, Status.FAILED)
    s = await dash.overview_stats(db)
    assert len(s.failures_daily) == 14 and sum(s.failures_daily) == 1 and s.failures_daily[-1] == 1
    assert sum(s.intake_daily) == 1 and s.intake_daily[-1] == 1


async def test_held_state_only_counts_future_retries_of_approved_or_publishing(dash, db):
    held = await make(db, Status.APPROVED)
    past = await make(db, Status.PUBLISHING)
    other = await make(db, Status.TRIAGED)
    future = (NOW + timedelta(minutes=30)).isoformat()
    await sql(db, "UPDATE items SET next_attempt_at=?, last_error=? WHERE id=?", future, "rate limited\nmore", held)
    await sql(db, "UPDATE items SET next_attempt_at=? WHERE id=?", (NOW - timedelta(minutes=1)).isoformat(), past)
    await sql(db, "UPDATE items SET next_attempt_at=? WHERE id=?", future, other)
    s = await dash.overview_stats(db, NOW)
    # the exception text is classified, never shown (rule 15)
    assert (s.held_count, s.held_until, s.held_error) == (1, future, dash.GENERIC_REASON)


async def test_why_stopped_groups_truncates_and_is_windowed(dash, db):
    for _ in range(3):
        i = await make(db, Status.TRIAGED)
        await db.transition(i, Status.FAILED, detail="Timeout talking to model\nattempt 1")
    j = await make(db, Status.TRIAGED)
    await db.transition(j, Status.FAILED, detail="Traceback (most recent call last):\n  File x\nKeyError: 'k'")
    old = await make(db, Status.TRIAGED)
    await db.transition(old, Status.FAILED, detail="ancient problem")
    await sql(db, "UPDATE events SET at=? WHERE item_id=?", (NOW - timedelta(days=9)).isoformat(), old)
    s = await dash.overview_stats(db, datetime.now(timezone.utc))
    assert s.stopped_reasons[0] == (dash.explain_error("timeout"), 3)
    assert s.stopped_reasons[1] == (dash.GENERIC_REASON, 1)   # the 9-day-old "ancient problem" is windowed out
    assert len(s.stopped_reasons) == 2


async def test_why_stopped_merges_details_sharing_a_first_line(dash, db):
    for n in range(4):
        i = await make(db, Status.TRIAGED)
        await db.transition(i, Status.FAILED, detail=f"HTTP 500\nbody {n}")
    s = await dash.overview_stats(db)
    assert s.stopped_reasons == [(dash.GENERIC_REASON, 4)]


async def test_dropped_reasons_use_the_existing_db_method(dash, db):
    i = await make(db, Status.TRIAGED)
    await db.transition(i, Status.DROPPED, {"triage_reason": "off topic"})
    assert (await dash.overview_stats(db)).dropped_reasons == [("off topic", 1)]


async def test_recent_activity_is_newest_first_and_capped_at_twelve(dash, db):
    ids = [await make(db, Status.TRIAGED) for _ in range(15)]
    s = await dash.overview_stats(db)
    assert len(s.recent) == 12 and s.recent[0]["item_id"] == ids[-1]


async def test_overview_stats_never_writes(dash, db):
    await make(db, Status.FAILED)
    before = [tuple(r) for r in await db.conn.execute_fetchall("SELECT * FROM events")]
    changes = db.conn.total_changes
    await dash.overview_stats(db)
    assert db.conn.total_changes == changes
    assert [tuple(r) for r in await db.conn.execute_fetchall("SELECT * FROM events")] == before


async def test_one_failing_read_is_isolated(dash, db, monkeypatch):
    await make(db, Status.FAILED)

    async def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(db, "queue_order", boom)
    s = await dash.overview_stats(db)
    assert s.failed == {"running"} and s.failed_now == 1


# ============================================================ rendering


async def test_empty_db_page_renders_every_card_without_error(dash, db):
    resp = await dash.overview(FakeRequest(db))
    html = markup(resp.text)
    assert resp.status == 200
    assert html.count("<h1>") == 1 and "<h1>Overview</h1>" in html
    for text in ("Waiting on you", "Failed", "In progress", "Published (24h)", "Publishing", "Clear",
                 "Status mix", "Why items stopped", "Waiting longest", "Recent activity", "New post",
                 "You&#x27;re all caught up.", "No items yet", "Nothing stopped", "No activity yet"):
        assert text in html, text
    assert "load this" not in html and "Traceback" not in html
    assert 'aria-current=page' in html and "href='/' title='Overview'" in html


async def test_page_is_the_active_overview_and_polls(dash, db):
    html = (await dash.overview(FakeRequest(db))).text
    assert re.search(r"<a class=nav-link aria-current=page href='/' title='Overview'", html)
    assert "data-refresh=" in html


async def test_kpi_cards_link_to_their_filtered_lists_and_show_numbers(dash, db):
    await make(db, Status.AWAITING_APPROVAL)
    await make(db, Status.FAILED)
    html = markup((await dash.overview(FakeRequest(db))).text)
    for href in ("/?lane=attn", "/?lane=bad", "/?lane=work", "/?lane=good", "/queue"):
        assert f"class=kpi href='{href}'" in html, href
    assert "oldest:" in html and "1 new in 24h" in html
    assert f"0 of {dash.HOURLY_PUBLISH_CAP} this hour" in html and "never published" in html


async def test_held_card_says_held_and_escapes_the_error_tooltip(dash, db):
    i = await make(db, Status.APPROVED)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    await sql(db, "UPDATE items SET next_attempt_at=?, last_error=? WHERE id=?", soon, XSS, i)
    html = markup((await dash.overview(FakeRequest(db))).text)
    assert "Held: 1 waiting" in html and "retries at" in html and "Clear" not in html
    assert XSS not in html and "alert(" not in html and dash.GENERIC_REASON in html


async def test_hostile_text_everywhere_is_escaped(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, text=XSS)
    j = await make(db, Status.TRIAGED)
    await db.transition(j, Status.FAILED, detail=XSS + "\nsecond")
    k = await make(db, Status.TRIAGED)
    await db.transition(k, Status.DROPPED, {"triage_reason": XSS})
    raw = (await dash.overview(FakeRequest(db))).text
    # markup() would strip an injected <script> block, so look at the raw page: the shell has two
    # <script> tags of its own, and the hostile text must be present only escaped.
    assert i and raw.count("<script>") == 2 and raw.count("&lt;script&gt;alert") >= 2


async def test_stack_traces_never_reach_the_page(dash, db):
    j = await make(db, Status.TRIAGED)
    await db.transition(j, Status.FAILED,
                        detail="Traceback (most recent call last):\n  File \"/srv/x.py\", line 9\nValueError: nope")
    html = markup((await dash.overview(FakeRequest(db))).text)
    assert "ValueError" not in html and "Traceback" not in html and "/srv/x.py" not in html
    assert dash.GENERIC_REASON in html


async def test_charts_carry_role_img_and_labels_when_there_is_data(dash, db):
    await make(db, Status.FAILED)
    html = markup((await dash.overview(FakeRequest(db))).text)
    assert "role=img" in html and "Failures per day: " in html and "Status mix: " in html
    assert "Published per day" in html and "Nothing in the last 14 days." in html


async def test_waiting_longest_links_and_see_all(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, text="**Big** news")
    html = markup((await dash.overview(FakeRequest(db))).text)
    assert f"href='/item/{i}'>#{i}</a>" in html and f"href='/item/{i}'>Review</a>" in html
    assert "Big news" in html and "**" not in html
    assert "href='/?lane=attn'>See all 1</a>" in html


async def test_failed_section_renders_a_couldnt_load_card_not_a_500(dash, db, monkeypatch):
    await make(db, Status.FAILED)

    async def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(db, "last_published_at", boom)
    monkeypatch.setattr(db, "dropped_reasons_since", boom)
    resp = await dash.overview(FakeRequest(db))
    html = markup(resp.text)
    assert resp.status == 200 and html.count("Couldn&#x27;t load this") == 2    # Publishing KPI + Why stopped
    assert "Recent activity" in html and "Waiting longest" in html         # the rest still render


async def test_counts_failure_breaks_status_cards_only(dash, db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(db, "status_counts", boom)
    html = markup((await dash.overview(FakeRequest(db))).text)
    assert html.count("Couldn&#x27;t load this") >= 4 and "Recent activity" in html
    assert html.count("kpi kpi-err") == 3             # waiting / failed / running cards


# ============================================================ routing


async def test_root_without_list_params_is_the_overview(dash, db):
    html = markup((await dash.home(FakeRequest(db, {}))).text)
    assert "<h1>Overview</h1>" in html


@pytest.mark.parametrize("query", [{"lane": "attn"}, {"q": "foo"}, {"q": ""}, {"all": "1"}, {"thin": "too short"}])
async def test_list_params_delegate_to_index_unchanged(dash, db, query):
    await make(db, Status.AWAITING_APPROVAL, text="foo item")
    html = markup((await dash.home(FakeRequest(db, query))).text)
    assert "<h1>Overview</h1>" not in html
    direct = markup((await dash.index(FakeRequest(db, query))).text)
    assert html == direct


async def test_routes_registered(dash, tmp_path):
    app = await dash.build(SimpleNamespace(db_path=tmp_path / "r.db"))
    routes = {(r.method, r.resource.canonical): r.handler for r in app.router.routes()}
    assert routes[("GET", "/")] is dash.home and routes[("GET", "/overview")] is dash.overview
    assert routes[("GET", "/items")] is dash.index
    await app["db"].close()
