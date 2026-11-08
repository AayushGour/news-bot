"""UX-7: run order and logs, driven through the real ``queue()`` / ``logs()`` handlers.

Every test renders a page from a temp DB / temp log file; none re-implements the logic.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.models import Status

ROOT = Path(__file__).resolve().parents[1]
XSS = "<script>alert('x')</script>"
EVIL = "'><img src=x onerror=alert(1)>"   # not a <script>, so doc() cannot strip it


@pytest.fixture
def dash(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dashboard_ux7_under_test", ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "pipeline.log")
    return module


class FakeRequest:
    def __init__(self, db, query=None, item_id=0):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=False)}
        self.query = query or {}
        self.match_info = {"id": str(item_id)}
        self.headers = {}


def doc(html: str) -> str:
    return re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)


def pre(html: str) -> list[str]:
    """The lines inside the log <pre>, tags stripped, exactly as displayed (still escaped)."""
    inner = re.search(r"<pre>(.*?)</pre>", doc(html), re.S).group(1)
    return [re.sub(r"<[^>]+>", "", ln) for ln in inner.split("\n")]


def entry(level: str, msg: str, n: int = 0, logger: str = "pipeline.worker") -> str:
    return f"2026-11-0{1 + n // 60 % 9} 10:{n % 60:02d}:00,000 {level:<7} {logger}: {msg}"


def write_log(dash, lines: list[str]) -> None:
    dash.LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def logs(dash, db, **query) -> str:
    return (await dash.logs(FakeRequest(db, query))).text


# ===================================================================== logs: filtering


async def test_no_log_file_is_the_empty_state_without_filters(dash, db):
    html = doc(await logs(dash, db))
    assert "No log file yet" in html and "log-q" not in html


async def test_default_view_is_newest_first_with_the_count_line(dash, db):
    write_log(dash, [entry("INFO", "first", 0), entry("INFO", "second", 1), entry("INFO", "third", 2)])
    html = await logs(dash, db)
    lines = pre(html)
    assert [ln.split(": ", 1)[1] for ln in lines] == ["third", "second", "first"]
    assert "Showing 3 of 3 lines" in html and "<h1>Logs</h1>" in html


async def test_text_filter_is_case_insensitive_and_keeps_order(dash, db):
    write_log(dash, [entry("INFO", "Alpha one", 0), entry("INFO", "beta", 1), entry("INFO", "alpha TWO", 2)])
    html = await logs(dash, db, q="ALPHA")
    assert [ln.split(": ", 1)[1] for ln in pre(html)] == ["alpha TWO", "Alpha one"]
    assert "Showing 2 of 2 lines (filtered from 3)" in html


async def test_level_filters_the_level_column_not_message_text(dash, db):
    write_log(dash, [
        entry("INFO", "ConnectionResetError was handled", 0),
        entry("ERROR", "real failure", 1),
        entry("WARNING", "slow", 2),
        entry("DEBUG", "an ERROR word in debug", 3),
        entry("CRITICAL", "dead", 4),
    ])
    err = [ln.split(": ", 1)[1] for ln in pre(await logs(dash, db, level="error"))]
    assert err == ["dead", "real failure"], "error means ERROR and above, by level column only"
    warn = [ln.split(": ", 1)[1] for ln in pre(await logs(dash, db, level="warning"))]
    assert warn == ["dead", "slow", "real failure"]
    assert len(pre(await logs(dash, db, level="all"))) == 5


async def test_a_traceback_stays_attached_to_its_error_and_is_never_split_by_reversal(dash, db):
    write_log(dash, [
        entry("INFO", "before", 0),
        entry("ERROR", "item 5 failed", 1),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in boom',
        "ValueError: nope",
        entry("INFO", "after", 2),
    ])
    frames = ["Traceback (most recent call last):", "  File &quot;x.py&quot;, line 1, in boom", "ValueError: nope"]
    shown = pre(await logs(dash, db, level="error"))
    assert shown[0].endswith("item 5 failed") and shown[1:] == frames
    full = pre(await logs(dash, db))
    start = next(i for i, ln in enumerate(full) if ln.endswith("item 5 failed"))
    assert full[start + 1:start + 4] == frames, "newest-first reverses entries, not the lines inside one"
    assert full[0].endswith("after") and full[-1].endswith("before")
    by_frame = pre(await logs(dash, db, q="ValueError"))
    assert by_frame[0].endswith("item 5 failed") and by_frame[-1] == "ValueError: nope"


async def test_unknown_level_falls_back_to_all(dash, db):
    write_log(dash, [entry("INFO", "a", 0), entry("ERROR", "b", 1)])
    junk = await logs(dash, db, level="junk")
    assert len(pre(junk)) == 2 and "<option value='all' selected>" in junk and "filtered from" not in junk
    assert len(pre(await logs(dash, db, level="'; DROP"))) == 2


@pytest.mark.parametrize("n,want", [("", 500), ("abc", 500), ("-5", 500), ("1.5", 500),
                                    ("0", 7), ("3", 3), ("1", 1), ("501", 7), ("7", 7)])
async def test_line_cap(dash, db, n, want):
    write_log(dash, [entry("INFO", f"m{i}", i) for i in range(7)])
    query = {"n": n} if n != "" else {}
    html = await logs(dash, db, **query)
    assert len(pre(html)) == min(want, 7)
    assert f"Showing {min(want, 7)} of 7 lines" in html


async def test_default_cap_is_500_and_show_all_link_keeps_filters(dash, db):
    write_log(dash, [entry("INFO", f"row {i} needle", i) for i in range(620)] + [entry("INFO", "other", 0)])
    html = await logs(dash, db, q="needle")
    assert len(pre(html)) == 500 and "Showing 500 of 620 lines (filtered from 621)" in html
    assert pre(html)[0].endswith("row 619 needle"), "newest first"
    link = re.search(r"<a class=card-link href='([^']+)'>Show all", html).group(1)
    assert "q=needle" in link and "n=0" in link
    everything = await logs(dash, db, q="needle", n="0")
    assert len(pre(everything)) == 620 and "Show all" not in everything


async def test_hostile_text_is_escaped_everywhere(dash, db):
    """Payloads are not <script>, so doc() cannot hide a failure; the raw page is asserted."""
    evil = EVIL
    safe = dash.esc(evil)
    write_log(dash, [entry("INFO", f"line {evil}", 0), entry("ERROR", f"bad {evil}", 1)])
    for query in ({"q": evil}, {"q": "line"}, {"level": "error"}):
        html = await logs(dash, db, **query)
        assert "<img src=x" not in html, query
    html = await logs(dash, db, q=evil)
    assert f"id=log-q name=q value='{safe}'" in html
    assert f"line {safe}" in html
    plain = await logs(dash, db, q="line")
    assert f"line {safe}" in plain and "&lt;img src=x onerror=alert(1)&gt;" in plain
    baseline = dash.page("t", "", {}).text.count("<script>")
    scripted = XSS
    write_log(dash, [entry("INFO", f"x {scripted}", 0)])
    assert (await logs(dash, db, q=scripted)).count("<script>") == baseline


async def test_negative_n_means_the_default_not_all(dash, db):
    write_log(dash, [entry("INFO", f"m{i}", i) for i in range(620)])
    assert len(pre(await logs(dash, db, n="-5"))) == 500
    assert len(pre(await logs(dash, db, n="abc"))) == 500
    assert len(pre(await logs(dash, db, n="0"))) == 620


async def test_the_page_lists_the_files_it_read(dash, db, tmp_path):
    write_log(dash, [entry("INFO", "x", 0)])
    (tmp_path / "pipeline.log.1").write_text("old\n")
    html = await logs(dash, db)
    assert "pipeline.log.1, pipeline.log" in html


async def test_no_match_keeps_the_filter_form_and_offers_a_way_out(dash, db):
    write_log(dash, [entry("INFO", "hello", 0)])
    html = doc(await logs(dash, db, q="zzz-nothing", level="error"))
    assert "No log lines match" in html and "class=empty" in html
    assert "name=q value='zzz-nothing'" in html and "<option value='error' selected>" in html
    assert "href='/logs'" in html


async def test_filter_controls_are_labelled_and_do_not_clash_with_the_shell_search(dash, db):
    write_log(dash, [entry("INFO", "x", 0)])
    html = doc(await logs(dash, db))
    ids = re.findall(r"\bid=([\w-]+)", html)
    assert len(ids) == len(set(ids)), "duplicate ids"
    for field in ("log-q", "log-level", "log-n"):
        assert f"<label for={field}>" in html and f"id={field}" in html
    assert html.count("id=q ") == 1, "only the shell search owns id=q"


async def test_errors_and_warnings_are_styled_by_their_level(dash, db):
    write_log(dash, [entry("INFO", "i", 0), entry("WARNING", "w", 1), entry("ERROR", "e", 2)])
    html = doc(await logs(dash, db))
    assert re.search(r"<span class='lv-error'>[^<]*ERROR[^<]*</span>", html)
    assert re.search(r"<span class='lv-warning'>[^<]*WARNING[^<]*</span>", html)
    assert html.count("class='lv-") == 2
    assert "lv-error" in dash.CSS and "lv-warning" in dash.CSS


# ===================================================================== shared filter helper


def test_filter_log_unit(dash):
    text = "\n".join([entry("INFO", "a item 3 x", 0), "  continuation item 4", entry("ERROR", "b", 1)])
    assert [body.splitlines()[0].endswith(": b") for _, body in dash.filter_log(text, level="error")] == [True]
    only = dash.filter_log(text, pattern=re.compile(r"\bitem 4\b"))
    assert len(only) == 1 and "continuation item 4" in only[0][1], "match anywhere in the entry"
    assert dash.filter_log("") == [] and dash.filter_log("no header at all")[0][0] == 0
    assert dash.filter_log("no header at all", level="warning") == []
    assert dash.filter_log(text, q="CONTINUATION")[0][1].startswith("2026")


async def test_item_log_uses_the_same_filter_and_stays_newest_first(dash, db):
    write_log(dash, [entry("INFO", "item 5 started", 0), entry("INFO", "item 6 other", 1),
                     entry("ERROR", "item 5 failed", 2), "Traceback (most recent call last):",
                     entry("INFO", "item 55 not it", 3)])
    got = dash.item_log(5).splitlines()
    assert [ln.split(": ", 1)[-1] for ln in got] == [
        "item 5 failed", "Traceback (most recent call last):", "item 5 started"]
    assert dash.item_log(999) == ""


def test_item_log_with_no_log_file(dash):
    assert dash.item_log(1) == ""


# ===================================================================== run order


async def make(db, status=Status.TRIAGED, text="a post", n=[0]):  # noqa: B006
    n[0] += 1
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=7000 + n[0], raw_text=text)
    await db.transition(i, status, {})
    return i


async def test_empty_queue_state_links_out(dash, db):
    html = doc((await dash.queue(FakeRequest(db))).text)
    assert "Nothing queued" in html and "class=empty" in html and "<h1>Run order</h1>" in html


async def test_queue_table_semantics_and_aria_labels(dash, db):
    a, b, c = [await make(db, text=t) for t in ("**one**", "two", EVIL)]
    raw = (await dash.queue(FakeRequest(db))).text
    html = doc(raw)
    assert html.count("<th scope=col") == 8 and "role=table" not in html
    assert "<table class='tbl queue' aria-label='Run order'>" in html
    assert ">one</a>" in html and "**" not in html.split("<main")[1]
    assert "<img src=x" not in raw and f">{dash.esc(EVIL)}</a>" in raw
    rows = html.split("<tbody>")[1].split("</tr>")[:3]
    # first row: no top/up; last row: no down
    assert "Move #%d up" % a not in rows[0] and "Move #%d to top" % a not in rows[0]
    assert "Move #%d down" % a in rows[0]
    assert "Move #%d to top" % b in rows[1] and "Move #%d up" % b in rows[1] and "Move #%d down" % b in rows[1]
    assert "Move #%d down" % c not in rows[2] and "Move #%d up" % c in rows[2]
    assert f"action='/queue/{b}/up'" in rows[1] and "style=" not in html.split("<tbody>")[1]
    # button order: top, up, down
    assert rows[1].index("to top") < rows[1].index("Move #%d up" % b) < rows[1].index("Move #%d down" % b)


async def test_backing_off_chip_shows_the_retry_time(dash, db):
    i = await make(db)
    await db.update_fields(i, {"next_attempt_at": "2999-01-02T03:04:05+00:00"})
    html = doc((await dash.queue(FakeRequest(db))).text)
    assert "backing off until 2999-01-02 03:04" in html
    await db.update_fields(i, {"next_attempt_at": "2000-01-02T03:04:05+00:00"})
    assert "backing off" not in doc((await dash.queue(FakeRequest(db))).text)


def test_move_forms_need_no_inline_style(dash):
    src = (ROOT / "scripts" / "dashboard.py").read_text()
    queue_src = src[src.index("async def queue("):src.index("async def move(")]
    assert "style=" not in queue_src


async def test_queue_row_uses_the_clamped_plain_title_and_links_only_it(dash, db):
    long_text = "**Big news** " + "word " * 200 + "THE-TAIL-MARKER"
    i = await make(db, text=long_text)
    html = doc((await dash.queue(FakeRequest(db))).text)
    row = html.split("<tbody>")[1].split("</tr>")[0]
    assert "THE-TAIL-MARKER" not in html, "the full raw_text must not be rendered"
    link = re.search(r"<a class=qtitle href='/item/%d'>([^<]*)</a>" % i, row).group(1)
    assert link.startswith("Big news word word") and link.endswith("…") and len(link) <= dash.QUEUE_TITLE_CHARS + 1
    assert "**" not in row
    assert row.count("<a ") == 2, "the #id link and the title link only (no giant link, no stray anchors)"
    assert "-webkit-line-clamp:2" in re.search(r"\.tbl\.queue \.qtitle\{[^}]*\}", dash.CSS).group(0)


async def test_queue_short_columns_do_not_wrap_and_the_title_column_has_a_floor(dash, db):
    await make(db, text="short")
    html = doc((await dash.queue(FakeRequest(db))).text)
    head, row = html.split("<thead>")[1].split("</thead>")[0], html.split("<tbody>")[1]
    for label in ("Position", "#", "Status", "Source", "Age", "Retry"):
        assert re.search(r"<th scope=col class=[^>]*nw[^>]*>%s</th>" % re.escape(label), head), label
    assert "<th scope=col class=qt>Title</th>" in head
    assert len(re.findall(r"<td class=[^>]*\bnw\b", row)) == 6 and "<td class=qt>" in row
    css = re.sub(r"/\*.*?\*/", "", dash.CSS, flags=re.S)
    assert "white-space:nowrap" in re.search(r"\.tbl\.queue \.nw\{[^}]*\}", css).group(0)
    qt = re.search(r"\.tbl\.queue \.qt\{[^}]*\}", css).group(0)
    assert "min-width:220px" in qt and "width:100%" in qt
    assert ".tbl-wrap{overflow-x:clip" in css and "@media (max-width:1279px){.tbl-wrap{overflow-x:auto}" in css, \
        "the shared table wrapper rules stay intact"
