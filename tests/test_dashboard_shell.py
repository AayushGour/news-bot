"""UX-1: the dashboard's design system and app shell.

Pins what every later package builds on: the shell landmarks and skip link, the draft-safe refresh
policy, the label maps, the shared helpers, the CSS fences, and — measured, not eyeballed — that
every text/control token pair in both themes meets WCAG contrast.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from pipeline.models import Status

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dashboard.py"
XSS = "<script>alert('x')</script>"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dash():
    return _load(SCRIPT, "dashboard_shell_under_test")


def markup(html: str) -> str:
    """The page without its inlined <style>/<script>, so assertions see only the document."""
    return re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)


def shell(dash, body="<p>x</p>", counts=None, title="Items", *a, **kw) -> str:
    return markup(dash.page(title, body, counts if counts is not None else {}, *a, **kw).text)


class FakeRequest:
    def __init__(self, db, item_id=0, query=None):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=False)}
        self.match_info = {"id": str(item_id)}
        self.query = query or {}
        self.headers = {}


# =========================================================================== contrast (WCAG)


def _lum(hex_):
    h = hex_.lstrip("#")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(fg, bg, pct):
    """CSS ``color-mix(in srgb, fg pct%, bg)``."""
    f, b = fg.lstrip("#"), bg.lstrip("#")
    return "#" + "".join(
        f"{round(int(f[i:i + 2], 16) * pct + int(b[i:i + 2], 16) * (1 - pct)):02x}" for i in (0, 2, 4))


def _tokens(css: str, start: str, end: str) -> dict[str, str]:
    block = css[css.index(start):css.index(end)]
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})\b", block))


@pytest.fixture(params=["dark", "light"])
def theme(request, dash):
    css = dash.CSS
    if request.param == "dark":
        return request.param, _tokens(css, "/* ---- tokens: dark", "/* ---- tokens: light")
    return request.param, _tokens(css, "/* ---- tokens: light", "/* ---- tokens: scale")


def test_text_tokens_clear_4_5_on_every_surface(theme):
    name, t = theme
    surfaces = ("bg", "raise", "sink", "side")
    for fg in ("ink", "dim", "faint", "attn", "bad", "work"):
        for bg in surfaces:
            assert ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg, ratio(t[fg], t[bg]))
    for bg in ("bg", "raise", "side"):
        assert ratio(t["brand"], t[bg]) >= 4.5, (name, "brand", bg)
    assert ratio(t["dim"], t["sink"]) >= 4.5


def test_buttons_bubble_and_tints_clear_4_5(theme):
    name, t = theme
    assert ratio(t["on-good"], t["good"]) >= 4.5
    assert ratio(t["on-good"], mix(t["good"], "#000000", 0.88)) >= 4.5, "primary hover"
    assert ratio(t["on-bad"], t["bad"]) >= 4.5, "danger hover fill"
    assert ratio(t["on-bubble"], t["bubble"]) >= 4.5
    assert ratio(t["bg"], t["attn"]) >= 4.5, "regenerate hover"
    pct = 0.14 if name == "dark" else 0.09
    assert ratio(t["brand"], mix(t["brand"], t["raise"], pct)) >= 4.5, "active nav"
    warn = mix(t["attn-dot"], t["raise"], 0.12)
    assert ratio(t["attn"], warn) >= 4.5 and ratio(t["dim"], warn) >= 4.5, "already-published banner"
    for kind in ("good", "bad", "attn", "work", "neutral"):
        fg = t[f"pill-{kind}"]
        assert ratio(fg, mix(fg, t["raise"], 0.12)) >= 4.5, (name, "pill", kind)


def test_control_edges_and_focus_clear_3(theme):
    name, t = theme
    for bg in ("bg", "raise", "side"):
        assert ratio(t["edge"], t[bg]) >= 3, (name, "edge", bg)
        assert ratio(t["focus"], t[bg]) >= 3, (name, "focus", bg)
    assert ratio(t["edge"], t["sink"]) >= 3, "input edge on its own fill"
    for series in ("good", "bad", "work"):
        assert ratio(t[series], t["raise"]) >= 3, (name, "chart", series)


def test_css_never_fades_text_or_actions_and_keeps_the_12px_floor(dash):
    css = re.sub(r"/\*.*?\*/", "", dash.CSS, flags=re.S)
    for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for value in re.findall(r"(?<![-\w])opacity:\s*([\d.]+)", decls):
            assert float(value) >= 1 or "aria-disabled" in selector, (selector.strip(), value)
    sizes = re.findall(r"font-size:\s*([\d.]+)px", css) + re.findall(r"--fs-[a-z]+:\s*([\d.]+)px", css)
    sizes += re.findall(r"font:[^;}]*?(?:^|\s)(?:\d{3}\s+)?([\d.]+)px", css)
    assert sizes and min(float(x) for x in sizes) >= 12, sorted(set(sizes))


def test_every_package_has_its_own_fenced_block_in_css_and_js(dash):
    for n in range(2, 8):
        for asset in (dash.CSS, dash.JS):
            opens = asset.count(f"==== UX-{n} ====")
            assert opens == 1 and asset.count(f"==== /UX-{n} ====") == 1, (n, opens)


def test_a_missing_asset_fails_at_import(tmp_path):
    shutil.copy(SCRIPT, tmp_path / "dashboard.py")
    with pytest.raises(FileNotFoundError):
        _load(tmp_path / "dashboard.py", "dashboard_missing_assets")


def test_assets_are_inlined_not_served(dash):
    html = dash.page("t", "", {}).text
    assert dash.CSS in html and dash.JS in html
    assert "</script" not in dash.JS.lower() and "</style" not in dash.CSS.lower()
    doc = markup(html)
    assert "<link" not in doc and not re.search(r"(?:src|href)=['\"]?https?:", doc)
    assert not re.search(r"https?://|url\(|@import", dash.CSS + dash.JS), "no external fetches"


# =========================================================================== page() shell


def test_shell_landmarks_skip_link_and_language(dash):
    html = shell(dash)
    assert html.startswith("<!doctype html><html lang=en>")
    assert html.index("class=skip href='#main'") < html.index("<aside") < html.index("<main id=main")
    assert html.count("<main ") == 1 and "tabindex=-1>" in html
    assert "<nav aria-label=Main>" in html and "<header class=top>" in html
    assert html.count("<h1>") == 1


def test_one_h1_is_the_heading_and_the_title_is_unique_per_page(dash):
    html = shell(dash, title="#567 - Ready to review", heading="Item 567")
    assert "<h1>Item 567</h1>" in html
    assert "<title>#567 - Ready to review - Mission control</title>" in html
    assert "<title>Mission control</title>" in shell(dash, title="Mission control")
    assert "<h1>Run order</h1>" in shell(dash, title="Run order")


def test_title_heading_subtitle_and_query_are_escaped(dash):
    """Payload is NOT a <script>, so markup() cannot hide a failure; the raw page is checked too."""
    evil = "'><img src=x onerror=alert(1)>"
    raw = dash.page(evil, "", {}, query=evil, heading=evil, subtitle=evil).text
    assert "<img src=x" not in raw
    assert raw.count("&lt;img src=x onerror=alert(1)&gt;") >= 4   # title, h1, subtitle, search value
    assert f"value='{dash.esc(evil)}'" in raw
    baseline = dash.page("t", "", {}).text.count("<script>")
    scripted = dash.page(XSS, "", {}, query=XSS, heading=XSS, subtitle=XSS).text
    assert scripted.count("<script>") == baseline, "a hostile <script> must not add a script tag"


def test_nav_has_every_destination_in_order_with_counts(dash):
    counts = {"awaiting_approval": 2, "needs_input": 1, "failed": 4, "dropped": 1, "published": 7,
              "triaged": 3}
    html = shell(dash, counts=counts)
    labels = ["Overview", "Review", "Running", "Published", "Stopped", "All items", "Run order", "Logs"]
    seen = [html.index(f"<span class=lbl>{label}</span>") for label in labels]
    assert seen == sorted(seen)
    for label, n in (("Review", 3), ("Running", 3), ("Published", 7), ("Stopped", 5), ("All items", 18)):
        assert f"<span class=lbl>{label}</span><span class=cnt>{n}</span>" in html, label


@pytest.mark.parametrize("active,label", [
    ("home", "Overview"), ("attn", "Review"), ("work", "Running"), ("good", "Published"),
    ("bad", "Stopped"), ("", "All items"), ("queue", "Run order"), ("logs", "Logs")])
def test_exactly_one_nav_link_is_current(dash, active, label):
    html = shell(dash, active=active)
    assert html.count("aria-current=page") == 1
    current = re.search(r"aria-current=page[^>]*>.*?<span class=lbl>([^<]+)</span>", html).group(1)
    assert current == label


def test_notice_renders_only_whitelisted_codes(dash):
    assert "Saved." in shell(dash, notice="saved") and "role=status" in shell(dash, notice="saved")
    for bad in (None, "", "<b>x", "nope", XSS):
        html = shell(dash, notice=bad)
        assert "class='toast" not in html, bad
    assert not any("<" in text or ">" in text for _, text in dash.NOTICES.values())


def test_error_notices_are_alerts(dash, monkeypatch):
    monkeypatch.setitem(dash.NOTICES, "boom", ("err", "It broke."))
    assert "role=alert" in shell(dash, notice="boom")


def test_actions_slot_is_inserted_and_search_is_labelled(dash):
    html = shell(dash, actions="<a id=mine>x</a>")
    assert "<a id=mine>x</a>" in html
    assert "<label class=sr for=q>" in html and "id=q name=q" in html


# ---- refresh policy


def test_no_meta_refresh_outside_noscript_and_no_poll_when_idle(dash):
    idle = shell(dash)
    assert "http-equiv=refresh" not in idle and "data-refresh" not in idle and "id=live" not in idle
    live = shell(dash, refresh=True)
    assert "<body data-refresh=20000>" in live and "id=live" in live
    metas = [m.start() for m in re.finditer("http-equiv=refresh", live)]
    assert len(metas) == 1
    assert live.rindex("<noscript>", 0, metas[0]) < metas[0] < live.index("</noscript>")


def test_refresh_interval_comes_from_the_one_constant(dash, monkeypatch):
    monkeypatch.setattr(dash, "REFRESH_S", 7)
    live = shell(dash, refresh=True)
    assert "data-refresh=7000" in live and "content=7>" in live


def test_refresh_script_skips_drafts_focus_overlays_and_hidden_tabs(dash):
    js = dash.JS
    # The reload is gated on all four.
    assert "window.location.reload()" in js and js.count("window.location.reload()") == 1
    assert re.search(r"!d\.hidden && !paused\(\)", js)
    assert "n.value !== n.defaultValue" in js and "typing(d.activeElement)" in js
    assert "dialog[open]" in js and "overlay-open" in js
    assert "Paused while you type" in js


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("scenario,reloads", [
    ("idle", 1), ("focused", 0), ("typed", 0), ("dialog", 0), ("drawer", 0), ("hidden", 0),
    ("untouched_prefill", 1), ("select_untouched", 1), ("select_changed", 0),
    ("select_default_untouched", 1), ("select_default_changed", 0), ("select_empty", 1),
])
def test_refresh_script_behaviour_under_a_fake_dom(dash, tmp_path, scenario, reloads):
    """Run the real dashboard.js against a stub DOM and count reloads over three poll periods."""
    harness = tmp_path / "h.js"
    harness.write_text(f"""
const S = {scenario!r};
let reloads = 0, timers = [];
const field = (v, dv) => ({{tagName:'TEXTAREA', type:'textarea', value:v, defaultValue:dv}});
const select = (defaultAt, now) => ({{tagName:'SELECT', type:'select-one', value:'x', selectedIndex:now,
  querySelectorAll: () => [0, 1, 2].map(i => ({{defaultSelected: i === defaultAt}}))}});
const fields = S === 'typed' ? [field('hello','')] : S === 'untouched_prefill' ? [field('q','q')]
  : S === 'select_untouched' ? [select(-1, 0)] : S === 'select_changed' ? [select(-1, 2)]
  : S === 'select_default_untouched' ? [select(1, 1)] : S === 'select_default_changed' ? [select(1, 0)]
  : S === 'select_empty' ? [select(-1, -1)] : [];
const el = () => ({{classList:{{add(){{}},remove(){{}},toggle(){{}},contains:()=>false}},
  setAttribute(){{}}, getAttribute:()=>null, hasAttribute:()=>false, addEventListener(){{}},
  removeAttribute(){{}}, focus(){{}}, remove(){{}}, textContent:'', hidden:false}});
const live = el(), liveText = el();
global.window = {{ location:{{ reload(){{ reloads++; }} }}, matchMedia:()=>({{matches:false}}),
  addEventListener(){{}}, confirm:()=>true }};
global.localStorage = {{}};
global.setInterval = (f, ms) => {{ timers.push([f, ms]); return 0; }};
global.setTimeout = () => 0;
global.document = {{
  hidden: S === 'hidden',
  activeElement: S === 'focused' ? {{tagName:'TEXTAREA'}} : null,
  documentElement: el(),
  body: Object.assign(el(), {{ getAttribute: k => k === 'data-refresh' ? '1000' : null,
    classList: {{ add(){{}}, remove(){{}}, contains: c => S === 'drawer' && c === 'overlay-open' }} }}),
  addEventListener(){{}},
  querySelector: s => s === 'dialog[open]' ? (S === 'dialog' ? {{}} : null)
    : s === '#live' ? live : s === '#live-text' ? liveText : null,
  querySelectorAll: s => s === 'textarea,input,select' ? fields : [],
}};
require({str(ROOT / 'scripts' / 'dashboard_static' / 'dashboard.js')!r});
const poll = timers.find(t => t[1] === 1000 && t[0].toString().includes('reload'));
for (let i = 0; i < 3; i++) poll[0]();
console.log(reloads > 0 ? 1 : 0);
""")
    import subprocess
    out = subprocess.run([NODE, str(harness)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == reloads


# =========================================================================== helpers


def test_status_labels_cover_every_status_with_plain_words(dash):
    assert set(dash.STATUS_LABEL) == {s.value for s in Status}
    assert dash.STATUS_LABEL["awaiting_approval"] == "Ready to review"
    assert dash.STATUS_LABEL["needs_input"] == "Needs your answer"
    assert dash.STATUS_LABEL["researched"] == "Working: researched"
    assert dash.STATUS_LABEL["revision_requested"] == "Rewriting"
    assert not any("_" in v for v in dash.STATUS_LABEL.values())


def test_status_badge_shows_the_label_and_keeps_the_raw_status(dash):
    html = dash.status_badge("awaiting_approval")
    assert html.endswith("</span>Ready to review</span>")
    assert "data-status='awaiting_approval'" in html and "title='awaiting_approval'" in html
    assert "p-attn" in html and "d-attn" in html and "aria-hidden=true" in html
    assert "awaiting_approval</span>" not in html, "raw word must not be the visible text"
    assert "p-bad" in dash.status_badge("failed") and "p-good" in dash.status_badge("published")


def test_status_badge_tolerates_an_unknown_or_hostile_status(dash):
    html = dash.status_badge(XSS)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Mystery" in dash.status_badge("Mystery")


def test_source_labels(dash):
    assert dash.source_label("dm") == "DM" and dash.source_label("channel") == "Channel post"
    assert dash.source_label("web") == "web" and dash.source_label(None) == ""


@pytest.mark.parametrize("raw,want", [
    ("**Breaking** news", "Breaking news"),
    ("**OpenAI says it will pay**", "OpenAI says it will pay"),
    ("*italic* and _also_ this", "italic and also this"),
    ("keep snake_case and http://x.io/a_b_c intact", "keep snake_case and http://x.io/a_b_c intact"),
    ("two\nlines\n\n  spaced", "two lines spaced"),
    ("", "(no text)"), (None, "(no text)"), ("   \n ", "(no text)"), ("****", "(no text)"),
])
def test_plain_title(dash, raw, want):
    assert dash.plain_title(raw) == want


def test_plain_title_limit_and_no_html_is_introduced(dash):
    assert dash.plain_title("a" * 50, 10) == "a" * 10 + "…"
    assert dash.plain_title("a" * 10, 10) == "a" * 10
    assert dash.plain_title(XSS) == XSS, "plain_title strips markdown only; callers esc()"


def test_plural(dash):
    assert [dash.plural(n, "item") for n in (0, 1, 2, 247)] == ["0 items", "1 item", "2 items", "247 items"]


def test_thumb(dash):
    assert dash.thumb(5, []) == "<span class=thumb></span>"
    html = dash.thumb(5, ["/m/5/a b.png"], alt="slide 1")
    assert "src='/media/5/a b.png'" in html and "alt='slide 1'" in html and "class=thumb" in html
    assert "alt=''" in dash.thumb(5, ["/m/5/a.png"]) and "onerror" in html
    hostile = dash.thumb(5, [f"/m/5/{XSS}.png"], alt=XSS)
    assert "<script>" not in hostile


def test_action_form_variants_confirm_and_escaping(dash):
    html = dash.action(9, "approve", "approve", "go")
    assert html == ("<form method=post action='/item/9/approve'>"
                    "<button class='btn go'>approve</button></form>")
    assert "data-confirm" not in html
    asked = dash.action(9, "drop", "drop", "no", confirm="Drop #9? It's gone <b>")
    assert "data-confirm='Drop #9? It&#x27;s gone &lt;b&gt;'" in asked
    assert "class='btn no'" in asked and "action='/item/9/drop'" in asked


def test_icon_empty_state_card_segmented(dash):
    assert "<use href='#i-queue'/>" in dash.icon("queue") and "aria-hidden=true" in dash.icon("x")
    for name in dash.ICON_PATHS:
        assert f"id='i-{name}'" in dash.ICONS
    assert dash.ICONS.count("<svg") == 1, "sprite emitted once"
    assert shell(dash).count("id='i-x'") == 1
    empty = dash.empty_state(XSS, XSS, "<a>go</a>")
    assert "&lt;script&gt;" in empty and "<script>" not in empty and "<a>go</a>" in empty
    assert "class=cta" not in dash.empty_state("t")
    assert "<h2>A &amp; B</h2>" in dash.card("A & B", "<p>b</p>", link="<a>l</a>")
    seg = dash.segmented([("All <b>3</b>", "/?lane=x&y", True), ("Two", "/z", False)], "Lane")
    assert "href='/?lane=x&amp;y' aria-current=page>All <b>3</b>" in seg and seg.count("aria-current") == 1


# =========================================================================== existing pages in the shell


async def _item(db, status, raw="**Big** news", n=[0]):  # noqa: B006
    n[0] += 1
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=900 + n[0], raw_text=raw)
    await db.transition(i, status, {"rendered_paths": ["/tmp/a.png"]})
    return i


async def test_board_renders_in_the_shell_with_labels_and_plain_titles(dash, db):
    i = await _item(db, Status.AWAITING_APPROVAL)
    html = markup((await dash.index(FakeRequest(db))).text)
    assert "<h1>All items</h1>" in html and html.count("<h1>") == 1
    assert "data-status='awaiting_approval'" in html and "Ready to review" in html
    assert "Channel post" in html and ">Big news</a>" in html and "**" not in html.split("<main")[1]
    assert f"<a class=rid href='/item/{i}'>#{i}</a>" in html
    attn = markup((await dash.index(FakeRequest(db, query={"lane": "attn"}))).text)
    assert "<h1>Waiting on you</h1>" in attn and (
        attn.split("</nav>")[0].count("aria-current=page") == 1)  # the sidebar nav; UX-3 adds segmented controls below
    assert re.search(r"aria-current=page[^>]*>.*?Review", attn)
    assert "<title>Waiting on you - Mission control</title>" in attn


async def test_empty_board_uses_the_empty_state(dash, db):
    html = (await dash.index(FakeRequest(db, query={"lane": "bad"}))).text
    assert "class=empty" in html and "Nothing here" in html


async def test_live_pages_poll_and_quiet_pages_do_not(dash, db):
    await _item(db, Status.TRIAGED)
    assert "data-refresh=" in (await dash.index(FakeRequest(db, query={"lane": "work"}))).text
    assert "data-refresh=" not in (await dash.index(FakeRequest(db, query={"lane": "bad"}))).text
    assert "data-refresh=" in (await dash.queue(FakeRequest(db))).text


async def test_queue_and_logs_and_detail_in_the_shell(dash, db, tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "LOG_PATH", tmp_path / "pipeline.log")
    empty = (await dash.queue(FakeRequest(db))).text
    assert "<h1>Run order</h1>" in empty and "Nothing queued" in empty
    i = await _item(db, Status.TRIAGED)
    queued = markup((await dash.queue(FakeRequest(db))).text)
    assert "1 item queued, next first" in queued and "Working: triaged" in queued
    assert re.search(r"aria-current=page[^>]*>.*?Run order", queued)

    nolog = markup((await dash.logs(FakeRequest(db))).text)
    assert "<h1>Logs</h1>" in nolog and "No log file yet" in nolog
    assert re.search(r"aria-current=page[^>]*>.*?Logs", nolog) and "All items</span><span class=cnt>" in nolog
    (tmp_path / "pipeline.log").write_text("hello\n")
    assert "<h1>Logs</h1>" in (await dash.logs(FakeRequest(db))).text

    detail = markup((await dash.detail(FakeRequest(db, i))).text)
    assert f"<h1>Item {i}</h1>" in detail and detail.count("<h1>") == 1
    assert f"<title>#{i} - Working: triaged - Mission control</title>" in detail
    assert "opacity:.35" not in detail


async def test_item_not_found_still_404s(dash, db):
    with pytest.raises(web.HTTPNotFound):
        await dash.detail(FakeRequest(db, 999))


def test_items_is_the_list_handler_and_root_is_the_overview(dash):
    async def go():
        app = await dash.build(SimpleNamespace(db_path=":memory:"))
        try:
            return {r.resource.canonical: r.handler for r in app.router.routes() if r.method == "GET"}
        finally:
            await app["db"].close()
    import asyncio
    routes = asyncio.run(go())
    assert routes["/items"] is dash.index            # the list lives at /items
    assert routes["/"] is dash.home                  # `/` is the Overview ...
    assert routes["/overview"] is dash.overview      # ... also reachable as /overview


def test_the_closed_drawer_is_not_reachable_by_keyboard(dash):
    """<1024px the off-canvas nav must leave the tab order (visibility), not just the viewport."""
    css = re.sub(r"/\*.*?\*/", "", dash.CSS, flags=re.S)
    rules = dict((sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                 if sel.strip() in (".js .side", ".js .side.open"))
    assert "visibility:hidden" in rules[".js .side"] and "translateX(-100%)" in rules[".js .side"]
    assert "visibility:visible" in rules[".js .side.open"]
    assert re.search(r"visibility 0s linear \.2s", rules[".js .side"]), "hide only after the slide-out"
    assert re.search(r"transition:[^;]*visibility 0s(?!\s*linear)", rules[".js .side.open"]), \
        "no visibility delay on open, or focus() lands on a still-hidden aside"


def test_segmented_links_meet_the_target_size(dash):
    css = re.sub(r"/\*.*?\*/", "", dash.CSS, flags=re.S)
    seg = re.search(r"\.seg a\{([^}]*)\}", css).group(1)
    assert "min-height:var(--ctl-h)" in seg


def test_busy_label_swap_keeps_the_buttons_markup(dash):
    assert "busySaved.push([b, b.innerHTML])" in dash.JS and "p[0].innerHTML = p[1]" in dash.JS


DRAWER_HARNESS = r"""
const out = {};
const listeners = {};
const on = (who) => (ev, fn) => { (listeners[who + ':' + ev] = listeners[who + ':' + ev] || []).push(fn); };
const fire = (who, ev, e = {}) => { e.preventDefault = () => { e.prevented = true; };
  (listeners[who + ':' + ev] || []).forEach(f => f(e)); return e; };
const classes = (set = new Set()) => ({ add: c => set.add(c), remove: c => set.delete(c),
  toggle: (c, f) => (f ? set.add(c) : set.delete(c)), contains: c => set.has(c) });
let active = null;
const raf = [];
const mk = (name, extra = {}) => Object.assign({ name, classList: classes(), attrs: {}, inert: false,
  setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
  hasAttribute: () => false, removeAttribute() {}, addEventListener: on(name), offsetParent: {},
  remove() {}, contains(n) { return n === this || (this.kids || []).includes(n); } }, extra);
const side = mk('side'), scrim = mk('scrim', { hidden: true }), menu = mk('menu'), close = mk('close');
const links = ['a', 'b', 'c'].map(n => mk(n));
side.kids = [...links, close];
const order = [close, ...links];                     // DOM order inside the aside
side.querySelectorAll = () => order;
// focus() only works on something inside a visible aside: that is what visibility:hidden would break
[menu, close, ...links].forEach(el => { el.focus = function () {
  const inside = order.includes(el);
  if (!inside || side.classList.contains('open')) active = el; }; });
const main = mk('main'), top = mk('top'), skip = mk('skip');
const sel = { '#side': side, '#scrim': scrim, '#menu-btn': menu, '#side-close': close };
global.window = { matchMedia: () => ({ matches: true }), addEventListener() {}, location: { reload() {} },
  confirm: () => true, requestAnimationFrame: f => { raf.push(f); } };   // deferred, never flushed: focus must be synchronous
global.localStorage = {};
global.setInterval = () => 0; global.setTimeout = () => 0;
global.document = { hidden: false, get activeElement() { return active; },
  documentElement: mk('html'), body: mk('body', { getAttribute: () => null }),
  addEventListener: on('doc'),
  querySelector: s => sel[s] || null,
  querySelectorAll: (s, ctx) => ctx === side ? order : s === 'main, header.top, .skip' ? [main, top, skip] : [] };
require(process.argv[2]);

const tab = (shift) => fire('doc', 'keydown', { key: 'Tab', shiftKey: !!shift });
fire('menu', 'click');
out.open_adds_class = side.classList.contains('open');
out.focus_lands_on_first_in_drawer = active === order[0];
out.behind_is_inert = main.inert && top.inert && skip.inert;
out.expanded = menu.attrs['aria-expanded'] === 'true' && scrim.hidden === false;
active = order[order.length - 1];
let e = tab(false);
out.tab_from_last_wraps_to_first = e.prevented === true && active === order[0];
e = tab(true);
out.shift_tab_from_first_wraps_to_last = e.prevented === true && active === order[order.length - 1];
active = order[1]; e = tab(false);
out.tab_in_the_middle_is_left_alone = !e.prevented && active === order[1];
active = menu; e = tab(false);
out.focus_that_escaped_is_pulled_back = e.prevented === true && active === order[0];
active = menu; e = tab(true);
out.escaped_shift_tab_goes_to_last = e.prevented === true && active === order[order.length - 1];
fire('doc', 'keydown', { key: 'Escape' });
out.esc_closes_restores_focus_and_page = !side.classList.contains('open') && active === menu
  && !main.inert && !top.inert && !skip.inert && scrim.hidden === true && menu.attrs['aria-expanded'] === 'false';
fire('menu', 'click'); fire('scrim', 'click');
out.scrim_closes_and_restores = !side.classList.contains('open') && active === menu && !main.inert;
fire('menu', 'click'); fire('close', 'click');
out.close_button_closes_and_restores = !side.classList.contains('open') && active === menu && !main.inert;
active = menu; e = tab(false);
out.tab_is_untouched_when_closed = !e.prevented && active === menu;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_drawer_takes_focus_traps_tab_at_document_level_and_restores(dash, tmp_path):
    """Run the real dashboard.js against a stub DOM where focus() only works once the aside is open."""
    import json
    import subprocess
    harness = tmp_path / "drawer.js"
    harness.write_text(DRAWER_HARNESS)
    out = subprocess.run([NODE, str(harness), str(ROOT / "scripts" / "dashboard_static" / "dashboard.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout)
    failed = [k for k, v in results.items() if v is not True]
    assert not failed, failed
    assert len(results) == 13, "a check went missing"
