"""UX-4: the item page — review-first layout, sticky decision bar, slide gallery, Next waiting."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.models import Status
from tests.test_dashboard_shell import SCRIPT, _load, markup

ROOT = Path(__file__).resolve().parents[1]
#: A payload that survives the markup() helper (which only strips <script> blocks): if any echo point
#: forgets esc(), a raw ``<img ... onerror=`` appears and the exact-form counts below stop matching.
PAYLOAD = "'><img src=x onerror=alert(1)>"
PAYLOAD_ESC = "&#x27;&gt;&lt;img src=x onerror=alert(1)&gt;"
JS = (ROOT / "scripts" / "dashboard_static" / "dashboard.js").read_text()


@pytest.fixture
def dash():
    return _load(SCRIPT, "dashboard_ux4_under_test")


class Req:
    def __init__(self, db, item_id):
        self.app = {"db": db, "settings": SimpleNamespace(revise_enabled=False)}
        self.match_info = {"id": str(item_id)}
        self.query = {}
        self.headers = {}


_n = [0]


async def make(db, status, raw="Some news", **fields):
    _n[0] += 1
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=7000 + _n[0], raw_text=raw)
    await db.transition(i, status, fields)
    return i


async def html_of(dash, db, i):
    return markup((await dash.detail(Req(db, i))).text)


DECK = {"rendered_paths": ["/m/1/s1.png", "/m/1/s2.png", "/m/1/s3.png"],
        "slides": [{"headline": "First"}, {"headline": PAYLOAD}, {}]}


async def test_awaiting_item_gets_the_decision_bar_with_existing_routes(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, caption="cap", **DECK)
    html = await html_of(dash, db, i)
    bar = html.split("class=decide", 1)[1].split("</div></div>", 1)[0]
    for verb in ("approve", "reject", "drop"):
        assert f"action='/item/{i}/{verb}'" in bar
    assert "href='#changes'>Ask for changes</a>" in bar and "id=changes" in html
    assert html.index("class=decide") < html.index("class=gallery")  # review first
    # UX-6 filled the prompts: each form carries its own (the shell script asks before submitting)
    for verb in ("approve", "reject", "drop"):
        assert f"action='/item/{i}/{verb}' data-confirm='{dash.esc(dash.DECISION_CONFIRM[verb])}'" in bar


@pytest.mark.parametrize("status", [Status.TRIAGED, Status.PUBLISHED, Status.FAILED, Status.NEEDS_INPUT])
async def test_other_statuses_have_no_bar_but_keep_drop(dash, db, status):
    i = await make(db, status)
    html = await html_of(dash, db, i)
    assert "class=decide" not in html and f"action='/item/{i}/approve'" not in html
    assert f"action='/item/{i}/drop'" in html and "Ask for changes" not in html


async def test_gallery_links_every_slide_through_media_and_has_a_labelled_dialog(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, **DECK)
    html = await html_of(dash, db, i)
    for name in ("s1.png", "s2.png", "s3.png"):
        assert f"<a class=slide href='/media/{i}/{name}'" in html
    assert "Deck · 3 slides" in html and html.count("data-slide=") == 3
    assert "<dialog id=slide-viewer class=viewer aria-label='Slide viewer'>" in html
    for label in ("Previous slide", "Next slide", "Close viewer"):
        assert f"aria-label='{label}'" in html
    assert "Slide 1 of 3: First" in html and "alt='Slide 3 of 3'" in html


async def test_no_gallery_without_rendered_slides(dash, db):
    i = await make(db, Status.TRIAGED)
    html = await html_of(dash, db, i)
    assert "<dialog" not in html and "Deck ·" not in html


async def test_hostile_text_is_escaped_in_every_new_block(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, raw=PAYLOAD, caption=PAYLOAD, research=[PAYLOAD],
                   brief=PAYLOAD, last_error=PAYLOAD, **DECK)
    await db.add_message(i, "operator", PAYLOAD, "dashboard")
    raw = (await dash.detail(Req(db, i))).text      # the unstripped page
    assert "<img src=x" not in raw
    # every echo point carries the exact escaped form and nothing else carries the handler
    assert raw.count("onerror=alert(1)") == raw.count(PAYLOAD_ESC)
    assert raw.count(PAYLOAD_ESC) >= 8   # title, source, 2 headline alts (tile + dialog attr), caption, chat, tech blocks
    assert f"<p class=item-title>{PAYLOAD_ESC}</p>" in raw
    assert f"aria-label='View Slide 2 of 3: {PAYLOAD_ESC}'" in raw


async def test_source_text_is_collapsed(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, raw="line one\nline two")
    html = await html_of(dash, db, i)
    assert re.search(r"<details class=source><summary>Source text</summary><p class=req>line one\nline two</p>", html)
    assert "<details class=source open" not in html


async def test_next_waiting_is_the_next_attn_item_not_the_next_id(dash, db):
    a = await make(db, Status.AWAITING_APPROVAL)
    b = await make(db, Status.TRIAGED)          # next id, but not waiting
    c = await make(db, Status.NEEDS_INPUT)
    d = await make(db, Status.AWAITING_APPROVAL)
    pub = await make(db, Status.PUBLISHED)
    assert f"class='btn next-waiting' href='/item/{c}'>Next waiting" in await html_of(dash, db, a)
    assert f"href='/item/{a}'>Next waiting" in await html_of(dash, db, d)   # wraps to the first
    assert f"href='/item/{c}'>Next waiting" in await html_of(dash, db, b)   # from a running item
    assert f"href='/item/{a}'>Next waiting" in await html_of(dash, db, pub)
    page = await html_of(dash, db, a)
    assert f"href='/item/{b}'>#{b} →" in page and "Waiting list" in page and "lane=attn" in page


async def test_no_next_waiting_when_this_is_the_only_one(dash, db):
    a = await make(db, Status.AWAITING_APPROVAL)
    await make(db, Status.PUBLISHED)
    assert "Next waiting" not in await html_of(dash, db, a)


async def test_technical_details_use_human_titles(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, research=[{"u": 1}, {"u": 2}, {"u": 3}], brief="b")
    html = await html_of(dash, db, i)
    assert "<h2>Technical details</h2>" in html
    assert "Research (3 sources)" in html and "Status history (1 transition)" in html
    assert "<summary>Brief</summary>" in html and "entries</summary>" not in html


async def test_contract_strings_survive(dash, db):
    i = await make(db, Status.TRIAGED)
    html = await html_of(dash, db, i)
    assert f"<h1>Item {i}</h1>" in html and "tl-node now" in html and "Stage · click to requeue" in html


def test_viewer_script_handles_keys_focus_return_and_stays_in_the_fence():
    block = JS.split("/* ==== UX-4 ====", 1)[1].split("/* ==== /UX-4 ====", 1)[0]
    for needle in ("showModal", "ArrowLeft", "ArrowRight", "opener.focus()", "'close'"):
        assert needle in block
    assert "e.target === viewer" in block   # backdrop click: the dialog's own padding is zero
    assert "<div class=viewer-in>" in (ROOT / "scripts" / "dashboard.py").read_text()
    assert JS.count("slide-viewer") == 1  # nothing outside the fence touches the viewer


async def test_quotes_and_ampersands_cannot_break_out_of_attributes_or_text(dash, db):
    evil = "x' onmouseover='boom() & <b>"
    i = await make(db, Status.AWAITING_APPROVAL, raw=evil, rendered_paths=["/m/1/s1.png"],
                   slides=[{"headline": evil}])
    html = await html_of(dash, db, i)
    assert "' onmouseover='" not in html
    assert "&#x27; onmouseover=&#x27;boom() &amp;" in html.split("<main", 1)[1].split("class=gallery", 1)[0]  # title
    tile = re.search(r"<a class=slide[^>]*>", html).group(0)
    assert "&#x27; onmouseover" in tile and "' onmouseover" not in tile


async def test_read_only_caption_is_escaped_too(dash, db):
    i = await make(db, Status.PUBLISHED, caption=PAYLOAD, brief=PAYLOAD)
    raw = (await dash.detail(Req(db, i))).text
    assert f"<pre>{PAYLOAD_ESC}</pre>" in raw
    assert raw.count("onerror=alert(1)") == raw.count(PAYLOAD_ESC) == 2


# ------------------------------------------------ T330 tester round: focus not obscured, labels, purged media

CSS = (ROOT / "scripts" / "dashboard_static" / "dashboard.css").read_text()


def fence(text, name, opener="/* ==== "):
    return text.split(f"{opener}{name} ====", 1)[1].split(f"{opener}/{name} ====", 1)[0]


def test_fixed_bar_reserves_room_and_scroll_padding_in_the_ux4_css_fence():
    css = fence(CSS, "UX-4")
    phone = css.split("@media (max-width:640px)", 1)[1]
    assert "main:has(.decide){padding-bottom:calc(var(--decide-h) + var(--s4))}" in phone
    assert "html:has(.decide){scroll-padding-bottom:calc(var(--decide-h) + var(--s4))}" in phone
    assert ":root{--decide-h:" in css and "scroll-padding-top:calc(var(--top-h) + var(--decide-h)" in css
    assert ".decide{position:fixed" in phone     # the rule the padding is for


def test_script_measures_the_bar_and_ask_for_changes_focuses_the_note_box():
    js = fence(JS, "UX-4", "/* ==== ")
    for needle in ("setProperty('--decide-h'", "ResizeObserver", "a[href=\"#changes\"]",
                   "#changes textarea", "box.focus("):
        assert needle in js


FIELD_TAGS = re.compile(r"<(input|textarea)\b([^>]*)>")


async def test_every_text_field_on_the_item_page_has_an_accessible_name(dash, db):
    shapes = [
        (Status.AWAITING_APPROVAL, {"caption": "c", "brief": "b", **DECK}),
        (Status.NEEDS_INPUT, {"question": "q?", "brief": "b"}),
        (Status.PUBLISHED, {"brief": "b"}),
    ]
    for status, fields in shapes:
        i = await make(db, status, **fields)
        html = (await html_of(dash, db, i)).split("<main", 1)[1]
        fields_found = [(t, a) for t, a in FIELD_TAGS.findall(html) if "type=hidden" not in a]
        assert fields_found, status
        for tag, attrs in fields_found:
            assert "aria-label='" in attrs or re.search(r"\bid=", attrs), (status, tag, attrs)


async def test_field_names_are_distinct_and_specific(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, caption="c", brief="b")
    html = await html_of(dash, db, i)
    labels = re.findall(r"<(?:input|textarea)[^>]*aria-label='([^']*)'", html.split("<main", 1)[1])
    assert {"Caption", "What should change", dash.REGEN_LABEL} <= set(labels)
    assert len(labels) == len(set(labels))


async def test_purged_images_say_so_instead_of_a_blank_deck(dash, db, tmp_path):
    i = await make(db, Status.AWAITING_APPROVAL, caption="c", **DECK)
    media = tmp_path / "media"
    (media / str(i)).mkdir(parents=True)
    (media / str(i) / "s1.png").write_bytes(b"x")          # only one slide survived

    class R(Req):
        def __init__(self, db, item_id, root):
            super().__init__(db, item_id)
            self.app["settings"] = SimpleNamespace(revise_enabled=False, media_dir=root)
    page_html = markup((await dash.detail(R(db, i, media))).text)
    assert "Deck · 1 slide<" in page_html and f"/media/{i}/s2.png" not in page_html
    gone = markup((await dash.detail(R(db, i, tmp_path / "empty"))).text)
    assert "Images no longer available" in gone and "class=gallery" not in gone and "<dialog" not in gone


async def test_telegram_commands_never_reach_the_question_card(dash, db):
    q = ("I researched this but found nothing that answers:\n  · A claim\n\n"
         "Reply with a better angle or a source, /post to send what I have for approval, or /drop.")
    i = await make(db, Status.NEEDS_INPUT, question=q)
    html = await html_of(dash, db, i)
    card_html = html.split("Needs your answer</h2>", 1)[1].split("</form>", 1)[0]
    shown = html.split("<h2>Technical details</h2>")[0]     # the folded log is the real log file, not ours
    assert "/post" not in shown and "or /drop" not in shown and "Reply with a better angle" not in card_html
    assert "A claim" in card_html and "can only be done in Telegram" in card_html
    assert dash.dashboard_question("use /skip please") == ("use (Telegram only) please", "")
    assert dash.dashboard_question("") == ("", "")


async def test_item_page_headings_start_with_a_capital(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL, caption="c", brief="b", **DECK)
    html = await html_of(dash, db, i)
    for heading in ("Caption", "Conversation", "Actions", "Stage · click to requeue"):
        assert f"<h2>{heading}</h2>" in html or heading == "Actions"
    assert "<h2>conversation</h2>" not in html and "<h2>caption</h2>" not in html
    assert "::first-letter{text-transform:uppercase}" in fence(CSS, "UX-4")
