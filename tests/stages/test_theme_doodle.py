"""The doodle theme: marker on pure white, text only (T278).

Jinja/HTML-level only (D44: no Chromium here). The visual check is a sample
deck rendered by hand; these pin the contract that decides what it contains.
"""

import json
import re
from pathlib import Path

from pipeline.stages.render import (
    FONTS_DIR,
    TEXT_ONLY_THEMES,
    resolve_theme_path,
    available_themes,
    build_html,
    doodle_underline,
    load_theme,
)

ROOT = Path(__file__).resolve().parents[2]
THEME = load_theme(ROOT / "config" / "themes" / "doodle.json")
IMG = str(ROOT / "config" / "themes" / "doodle.json")  # any existing file

ALL_TYPES = [
    {"type": "hook", "headline": "Bullsh*t AI Advice", "sub": "s"},
    {"type": "point", "headline": "P", "bullets": ["b"]},
    {"type": "facts", "headline": "F", "rows": [["a", "b"]]},
    {"type": "kpi", "headline": "K", "tiles": [{"value": "1", "label": "l"}]},
    {"type": "chart", "headline": "C", "series": [{"label": "a", "pct": 50, "display": "5"}]},
    {"type": "code", "headline": "Co", "lang": "py", "code": "x = 1"},
    {"type": "flow", "headline": "Fl", "steps": [{"label": "one"}]},
    {"type": "compare", "headline": "Cm", "rows": [["a", "b"]]},
    {"type": "quote", "headline": "Q", "quote": "q"},
    {"type": "photo", "headline": "Ph", "caption": "cap", "image": IMG,
     "image_mode": "inset"},
    {"type": "repo", "owner": "o", "name": "n", "logo": IMG},
    {"type": "links", "headline": "L", "links": ["https://a.example"]},
    {"type": "takeaway", "headline": "T"},
    {"type": "sources", "headline": "S", "urls": ["https://a.example"]},
    {"type": "follow", "headline": "Fo"},
]


def _html(slides=ALL_TYPES):
    return build_html(slides, ROOT / "templates", THEME)


def test_doodle_joins_the_rotation_and_is_pure_white():
    assert "doodle" in available_themes(ROOT / "config" / "themes")
    raw = json.loads((ROOT / "config" / "themes" / "doodle.json").read_text())
    assert raw["style"] == "doodle" and raw["bg"].upper() == "#FFFFFF"
    assert "palette" not in raw, "a palette paints per-slide colour fields"


def test_every_slide_type_renders_with_the_style_class():
    html = _html()
    assert html.count('class="slide style-doodle') == len(ALL_TYPES)


def test_no_kicker_is_rendered():
    html = _html()
    assert '<div class="kicker">' not in html
    assert 'class="slug"' not in html


def test_images_are_never_rendered():
    html = _html()
    assert "<img" not in html, "photo, inset, logo and background all stay out"
    assert 'class="photo"' not in html and 'class="bg"' not in html
    # the photo slide degrades to its caption text
    assert "Ph" in html and "cap" in html


def test_background_image_mode_is_ignored():
    html = _html([{"type": "hook", "headline": "H", "image": IMG,
                   "image_mode": "background"}])
    assert "slide style-doodle has-bg" not in html and "<img" not in html


def test_inset_image_on_a_text_slide_is_dropped():
    html = _html([{"type": "point", "headline": "P", "image": IMG,
                   "image_mode": "inset"}])
    assert "<img" not in html and 'class="inset"' not in html


def test_repo_logo_is_dropped():
    assert 'class="repologo"' not in _html([ALL_TYPES[10]])


def test_other_themes_still_render_kickers_and_photos():
    sketch = load_theme(ROOT / "config" / "themes" / "sketchbook.json")
    html = build_html(ALL_TYPES, ROOT / "templates", sketch)
    assert '<div class="kicker">' in html and 'class="photo"' in html


def test_fonts_are_bundled_locally_and_exist_on_disk():
    html = _html()
    assert "@font-face" in html and "Doodle Marker" in html and "Doodle Hand" in html
    uris = re.findall(r"url\('(file://[^']+\.ttf)'\)", html)
    assert len(uris) == 2
    assert all(u.startswith(FONTS_DIR.resolve().as_uri()) for u in uris)
    for name in ("ShantellSans-Variable.ttf", "PatrickHand-Regular.ttf",
                 "OFL-ShantellSans.txt", "OFL-PatrickHand.txt"):
        assert (FONTS_DIR / "doodle" / name).is_file(), name
    assert "http" not in " ".join(uris)


def test_other_themes_do_not_load_the_doodle_fonts():
    sketch = load_theme(ROOT / "config" / "themes" / "sketchbook.json")
    html = build_html(ALL_TYPES[:1], ROOT / "templates", sketch)
    assert "@font-face" not in html


def test_hook_underlines_one_word_with_an_svg_stroke():
    html = _html()
    assert html.count('<span class="ul">Bullsh*t</span>') == 1
    assert "stroke='%23E0453A'" in html, "stroke is the accent, as an SVG path"


def test_doodle_underline_picks_longest_early_word_and_escapes():
    assert str(doodle_underline("The Cursor Cutoff")) == 'The <span class="ul">Cursor</span> Cutoff'
    assert str(doodle_underline("A B Supercalifragilistic tail")).count("<span") == 1
    assert "<script>" not in str(doodle_underline("<script>alert(1)</script> x"))
    assert str(doodle_underline("")) == ""
    assert str(doodle_underline("one")) == '<span class="ul">one</span>'


def test_footer_is_hidden_for_doodle_only():
    css = _html()
    rule = re.search(r"\.style-doodle footer\{([^}]*)\}", css).group(1)
    assert "display:none" in rule
    assert not re.search(r"\.style-sketchbook footer\{[^}]*display:none", css)


def test_doodle_underline_escapes_words_that_are_not_underlined_too():
    out = str(doodle_underline("Supercalifragilistic <b>x</b>"))
    assert "&lt;b&gt;" in out and "<b>" not in out
    assert str(doodle_underline("Supercalifragilistic <b>x</b>")).startswith(
        '<span class="ul">Supercalifragilistic</span>')


def test_doodle_underline_skips_empty_tokens_and_keeps_spacing():
    out = str(doodle_underline("A  B   Cccc"))
    assert out == 'A  B   <span class="ul">Cccc</span>'
    assert str(doodle_underline("   ")) == "   "


def test_random_and_rotate_skip_text_only_themes_when_a_deck_has_a_photo():
    themes = ROOT / "config" / "themes"
    for name in ("random", "rotate"):
        picked = {resolve_theme_path(name, themes, item_id=i,
                                     exclude=TEXT_ONLY_THEMES).stem
                  for i in range(60)}
        assert "doodle" not in picked and len(picked) > 3, name
        # without the exclusion, the whole set (doodle included) is reachable
        assert "doodle" in {resolve_theme_path(name, themes, item_id=i).stem
                            for i in range(60)}, name


def test_a_pinned_doodle_is_honoured_even_with_a_photo():
    themes = ROOT / "config" / "themes"
    assert resolve_theme_path("doodle", themes, exclude=TEXT_ONLY_THEMES).stem == "doodle"


def test_choice_is_deterministic_per_item_with_exclusion():
    themes = ROOT / "config" / "themes"
    a = resolve_theme_path("random", themes, 7, exclude=TEXT_ONLY_THEMES)
    assert a == resolve_theme_path("random", themes, 7, exclude=TEXT_ONLY_THEMES)


def _render_theme_name(settings, tmp_path, monkeypatch, slides, theme_setting, item_id):
    """Run render() with the worker stubbed (no Chromium) and return the theme
    name that reached the HTML."""
    import asyncio
    from dataclasses import replace

    from pipeline.models import Item, Status
    from pipeline.stages import render as render_mod

    async def fake_worker(html_path, out_dir, count, prefix):
        return {"paths": [str(out_dir / f"{i}.png") for i in range(count)], "overflows": []}

    monkeypatch.setattr(render_mod, "_run_worker", fake_worker)
    local = replace(settings, media_dir=tmp_path / "m", template_dir=ROOT / "templates",
                    theme=theme_setting)
    item = Item(id=item_id, source="c", status=Status.COMPOSED, slides=slides)
    asyncio.run(render_mod.render(item, local))
    html = (tmp_path / "m" / str(item_id) / "slides.html").read_text()
    return "doodle" if "style-doodle" in html.split("<div class=\"slide", 1)[1] else "other"


def test_render_never_gives_a_photo_deck_to_doodle_under_random(
        settings, tmp_path, monkeypatch):
    photo_deck = [{"type": "hook", "headline": "H"},
                  {"type": "photo", "headline": "P", "image": IMG, "image_mode": "inset"}]
    text_deck = [{"type": "hook", "headline": "H"}]
    # find an item id that lands on doodle when unrestricted
    ids = [i for i in range(1, 200)
           if resolve_theme_path("random", ROOT / "config" / "themes", i).stem == "doodle"]
    assert ids
    i = ids[0]
    assert _render_theme_name(settings, tmp_path, monkeypatch, text_deck, "random", i) == "doodle"
    assert _render_theme_name(settings, tmp_path, monkeypatch, photo_deck, "random", i) == "other"
    # explicit pin still honoured
    assert _render_theme_name(settings, tmp_path, monkeypatch, photo_deck, "doodle", i) == "doodle"


def test_doodle_shrink_rules_come_after_every_generic_shrink_rule():
    """Same specificity, so source order decides: a doodle shrink rule placed
    before `.slide[data-shrink]` is dead (this shipped once)."""
    css = _html()
    last_generic = max(m.start() for m in re.finditer(r'\.slide\[data-shrink="1"\]', css))
    first_doodle = min(m.start() for m in re.finditer(r'\.style-doodle\[data-shrink="1"\]', css))
    assert first_doodle > last_generic
    for sel in ("td{padding:0}", ".tile{padding:0}", ".crow{grid-template-columns"):
        assert f'.style-doodle[data-shrink="1"] {sel}' in css


def test_doodle_underline_only_considers_the_first_three_words():
    """A long word past the window is never picked; the longest *within* it is."""
    assert str(doodle_underline("A B Ccc Supercalifragilistic")) == \
        'A B <span class="ul">Ccc</span> Supercalifragilistic'
    # all early words tie: the earliest wins, the long late word is ignored
    assert str(doodle_underline("A B C Supercalifragilistic")) == \
        '<span class="ul">A</span> B C Supercalifragilistic'


def test_excluding_every_theme_falls_back_to_the_full_list():
    themes = ROOT / "config" / "themes"
    everything = frozenset(available_themes(themes))
    for name in ("random", "rotate", ""):
        got = resolve_theme_path(name, themes, item_id=3, exclude=everything)
        assert got is not None and got.stem in everything, name


def test_doodle_shrink_pins_the_headline_sizes():
    css = _html()
    assert '.style-doodle[data-shrink="1"] h1{font-size:96px}' in css
    assert '.style-doodle[data-shrink="1"] h2{font-size:58px}' in css
    assert '.style-doodle[data-shrink="1"] .sub{font-size:44px}' in css
    assert '.style-doodle[data-shrink="1"] .tval{font-size:56px}' in css
    assert '.style-doodle[data-shrink="1"] .clabel{font-size:34px}' in css
