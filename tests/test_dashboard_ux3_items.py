"""UX-3 (T325): the items table, lane/sort controls, row actions and the new-post draft."""
from __future__ import annotations

import importlib.util
from html import escape as html_escape
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import web

from pipeline.intake.manual import TOO_THIN, TooThin
from pipeline.models import Status

ROOT = Path(__file__).resolve().parents[1]
XSS = "<img src=x onerror=alert(1)>'\"><b>x</b>"
ESC_XSS = "&lt;img src=x onerror=alert(1)&gt;"
_N = iter(range(1, 10_000))


@pytest.fixture
def dash(monkeypatch):
    spec = importlib.util.spec_from_file_location("dashboard_ux3_under_test", ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # @dataclass needs its module registered
    spec.loader.exec_module(module)
    return module


class Req:
    def __init__(self, db, query=None, form=None):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=False)}
        self.query = query or {}
        self._form = form or {}
        self.headers = {}

    async def post(self):
        return self._form


async def make(db, status, raw="Big news", **fields):
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=next(_N), raw_text=raw)
    await db.transition(i, status, {"rendered_paths": ["/tmp/a.png"], **fields})
    return i


def body(dash, resp) -> str:
    """The page minus the exact inlined stylesheet/script (they legitimately contain markup)."""
    return resp.text.replace(dash.CSS, "").replace(dash.JS, "")


async def board(dash, db, **query) -> str:
    return body(dash, await dash.index(Req(db, query)))


# ------------------------------------------------------------------ table


async def test_rows_are_a_real_table_with_pills_and_escaped_titles(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, raw=f"**Hot** {XSS}")
    html = await board(dash, db)
    assert "<table class='tbl items'>" in html and html.count("<th scope=col") == 7
    assert "data-status='awaiting_approval'" in html and "Ready to review" in html
    assert f"<a class=rid href='/item/{i}'>#{i}</a>" in html
    assert XSS not in html and ESC_XSS in html and "**" not in html.split("<main")[1]
    assert "Channel post" in html


async def test_failed_row_shows_one_error_line_only(dash, db):
    i = await make(db, Status.FAILED)
    await db.update_fields(i, {"last_error": "first line\nTraceback second line"})
    html = await board(dash, db)
    # our sentence, one line, never the exception text (rule 15)
    assert f"<div class=err>{dash.GENERIC_REASON}</div>" in html
    assert "first line" not in html and "second line" not in html


async def test_paging_and_show_all_keep_filters(dash, db):
    for _ in range(dash.PAGE_SIZE + 2):
        await make(db, Status.PUBLISHED)
    html = await board(dash, db, lane="good")
    assert html.count("<tr><td class=c-thumb>") == dash.PAGE_SIZE
    assert "Showing 25 of 27" in html and "/items?lane=good&amp;all=1" in html
    assert (await board(dash, db, lane="good", all="1")).count("<tr><td class=c-thumb>") == 27


# ------------------------------------------------------------------ filters / sort


async def test_lane_filter_and_counts_and_unknown_lane(dash, db):
    a = await make(db, Status.AWAITING_APPROVAL)
    p = await make(db, Status.PUBLISHED)
    attn = await board(dash, db, lane="attn")
    assert f"/item/{a}'>#{a}" in attn and f"/item/{p}'>#{p}" not in attn
    assert attn.count("aria-current=page") == 3  # nav + lane seg + sort seg
    assert "All <span class=cnt>2</span>" in attn
    # hostile / unknown lane: treated as "all", never echoed
    odd = await board(dash, db, lane=f"{XSS}' OR 1=1 --")
    assert f"#{a}<" in odd and f"#{p}<" in odd and XSS not in odd


async def test_sort_oldest_vs_newest_and_bad_sort_falls_back(dash, db):
    first = await make(db, Status.PUBLISHED)
    second = await make(db, Status.PUBLISHED)
    ids = lambda h: [int(x) for x in re.findall(r"class=rid href='/item/(\d+)'", h)]  # noqa: E731
    assert ids(await board(dash, db)) == [second, first]
    assert ids(await board(dash, db, sort="age")) == [first, second]
    assert ids(await board(dash, db, sort="id; DROP TABLE items")) == [second, first]
    assert len(ids(await board(dash, db))) == 2  # table intact


async def test_search_escaped_everywhere_and_survives_in_links(dash, db):
    await make(db, Status.PUBLISHED, raw="alpha story")
    html = await board(dash, db, q=XSS)
    assert XSS not in html and "<b>x</b>" not in html
    assert html.count(ESC_XSS) >= 3  # empty-state title, summary/crumb-free echo, search box value
    assert f"value='{html_escape(XSS)}'" in html
    hit = await board(dash, db, q="alpha", lane="good")
    assert "matching “alpha”" in hit and "q=alpha" in hit  # lane/sort links keep the search
    quote_attack = await board(dash, db, q="'; DROP TABLE items; --")
    assert "No matches" in quote_attack and "alpha story" not in quote_attack
    assert "alpha story" in await board(dash, db, q="alpha")  # table still there


async def test_numeric_query_still_jumps_to_item(dash, db):
    with pytest.raises(web.HTTPFound) as e:
        await dash.index(Req(db, {"q": "#91"}))
    assert e.value.location == "/item/91"


async def test_unicode_digit_query_is_a_search_not_a_500(dash, db):
    html = await board(dash, db, q="²")
    assert "No matches" in html
    with pytest.raises(web.HTTPFound):
        await dash.index(Req(db, {"q": "12"}))


async def test_empty_states(dash, db):
    assert "Nothing here" in await board(dash, db, lane="bad")
    assert "No matches" in await board(dash, db, q="zzz")
    assert "data-refresh=" not in await board(dash, db, lane="bad")


# ------------------------------------------------------------------ row actions


def row(status, i=7):
    return {"id": i, "status": status.value}


def test_default_rows_have_no_approve_or_reject(dash):
    out = dash.row_actions(row(Status.AWAITING_APPROVAL))
    assert "/approve" not in out and "/reject" not in out
    assert "<a class='tab go' href='/item/7'>Review</a>" in out and "/item/7/drop" in out
    assert ">Answer</a>" in dash.row_actions(row(Status.NEEDS_INPUT))


def test_allow_approve_flag_restores_the_forms(dash):
    out = dash.row_actions(row(Status.AWAITING_APPROVAL), allow_approve=True)
    assert "/item/7/approve" in out and "/item/7/reject" in out and "Review" not in out
    # the flag only affects awaiting_approval
    assert dash.row_actions(row(Status.FAILED), allow_approve=True) == dash.row_actions(row(Status.FAILED))


def test_answer_link_shape_and_terminal_has_no_drop(dash):
    assert "<a class=tab href='/item/7'>answer</a>" in dash.row_actions(row(Status.REVISION_CONFIRM))
    pub = dash.row_actions(row(Status.PUBLISHED))
    assert "drop" not in pub and "<form" not in pub
    assert "href='/queue'>order</a>" in dash.row_actions(row(Status.TRIAGED))
    assert "order" not in dash.row_actions(row(Status.AWAITING_APPROVAL))


async def test_no_approve_forms_in_the_list(dash, db):
    await make(db, Status.AWAITING_APPROVAL)
    html = await board(dash, db)
    assert "/approve" not in html and "/reject" not in html and ">Review</a>" in html


# ------------------------------------------------------------------ new-post draft


async def test_toothin_hands_the_text_back_and_form_refills_escaped(dash, db, monkeypatch):
    async def thin(*a, **k):
        raise TooThin("too short")
    monkeypatch.setattr(dash, "queue_request", thin)
    with pytest.raises(web.HTTPFound) as e:
        await dash.new(Req(db, form={"text": f"hi {XSS}"}))
    qs = parse_qs(urlparse(e.value.location).query)
    assert qs["thin"] == [dash.THIN_FLAG] and qs["draft"] == [f"hi {XSS}"]
    html = await board(dash, db, thin=dash.THIN_FLAG, draft=f"hi {XSS}")
    assert f">hi {html_escape(XSS)}</textarea>" in html and XSS not in html
    assert f"role=alert>{html_escape(TOO_THIN)}</p>" in html


async def test_thin_never_echoes_request_text(dash, db):
    html = await board(dash, db, thin=XSS)
    assert "role=alert" not in html and ESC_XSS not in html and XSS not in html
    assert "role=alert" not in await board(dash, db, thin="0")


async def test_draft_is_capped(dash, db):
    html = await board(dash, db, draft="x" * (dash.DRAFT_MAX + 500))
    assert "x" * dash.DRAFT_MAX in html and "x" * (dash.DRAFT_MAX + 1) not in html


def test_table_wrap_is_not_a_scroll_container_where_the_header_sticks(dash):
    """Geometry: overflow-x:auto turns .tbl-wrap into the scroll container, so the sticky th's
    top:var(--top-h) is resolved inside it and paints 56px over the first row at scroll 0.
    Wide viewports use overflow-x:clip (no scroll container, th sticks under the top bar);
    only <=1279px scrolls the wrap, with the header pinned at the wrap's own top."""
    css = dash.CSS
    base = re.search(r"\n\.tbl-wrap\{([^}]*)\}", css).group(1)
    assert "overflow-x:clip" in base and "overflow-x:auto" not in base and "overflow-y" not in base
    assert re.search(r"\n\.tbl th\{position:sticky;top:var\(--top-h\)", css)
    narrow = re.search(r"@media \(max-width:1279px\)\{([^@]*?)\}\s*\n\.tbl td", css, re.S).group(1)
    assert ".tbl-wrap{overflow-x:auto}" in narrow and ".tbl th{top:0}" in narrow
