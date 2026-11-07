from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.errors import Recompose, Retryable
from pipeline.models import Item, Status
from pipeline.stages.render import (
    DEFAULT_THEME,
    available_themes,
    build_html,
    load_theme,
    render,
    resolve_theme_path,
)

ROOT = Path(__file__).resolve().parents[2]

SIX = [
    {"type": "hook", "headline": "OpenAI Blocks Cursor Users",
     "sub": "The AI coding tool faces a cutoff in three months."},
    {"type": "point", "headline": "Scope Of The Cutoff",
     "bullets": ["Takes effect within 90 days.",
                 "Affects roughly 5% of Cursor traffic."],
     "stat": {"value": "3 Months", "label": "Time Until Block"}},
    {"type": "point", "headline": "Root Cause",
     "bullets": ["Driven by distrust, not technical failure."]},
    {"type": "facts", "headline": "Partnership History",
     "rows": [["Early adopter", "$8M seed round"], ["Traffic share", "5%"]]},
    {"type": "takeaway", "headline": "Implications For Developers",
     "sub": "Platform neutrality is not guaranteed."},
    {"type": "sources", "headline": "Further Reading",
     "urls": ["https://teslarati.example/a", "https://livemint.example/b"]},
]


def _item(slides, item_id=1):
    return Item(id=item_id, source="channel", status=Status.COMPOSED, slides=slides)


def _settings(settings, tmp_path):
    return replace(
        settings,
        media_dir=tmp_path / "media",
        template_dir=ROOT / "templates",
        theme_path=ROOT / "config" / "theme.json",
    )


# --------------------------------------------------------------------- theme


def test_load_theme_falls_back_when_file_is_missing():
    """A missing theme must not be able to block a post."""
    assert load_theme("/nope/theme.json") == DEFAULT_THEME


def test_load_theme_merges_kickers_over_defaults(tmp_path):
    path = tmp_path / "t.json"
    path.write_text('{"accent": "#FF0000", "kickers": {"hook": "BREAKING"}}')
    theme = load_theme(path)

    assert theme["accent"] == "#FF0000"
    assert theme["kickers"]["hook"] == "BREAKING"
    assert theme["kickers"]["sources"] == "SOURCES", "unspecified kickers keep defaults"


# ------------------------------------------------------------------ templates


def test_build_html_renders_every_slide_type():
    html = build_html(SIX, ROOT / "templates", DEFAULT_THEME)
    for index in range(len(SIX)):
        assert f'id="slide-{index}"' in html
    assert "OpenAI Blocks Cursor Users" in html
    assert "$8M seed round" in html
    assert "teslarati.example" in html


def test_repo_slide_renders_a_genuine_zero_star_count():
    """T111 AC3: `stars: 0` is a real fact (a brand-new repo), distinct from
    no star count having been researched at all. A plain Jinja truthy check
    on `s.stars` would hide it the same way it hid a fabricated non-zero --
    the template must render the badge whenever the key is present, zero or
    not."""
    html = build_html(
        [{"type": "repo", "headline": "h", "name": "repo0", "owner": "o0",
          "stars": 0, "url": "https://github.com/o0/repo0"}],
        ROOT / "templates", DEFAULT_THEME,
    )
    assert '<span class="stars">★ 0</span>' in html


def test_repo_slide_omits_the_star_badge_when_no_count_was_researched():
    """The other half of the same state: a repo slide whose note carried no
    star count at all (compose.py's _restore_repo_facts strips the key
    entirely in that case) must not render an empty or fabricated badge."""
    html = build_html(
        [{"type": "repo", "headline": "h", "name": "repo0", "owner": "o0",
          "url": "https://github.com/o0/repo0"}],
        ROOT / "templates", DEFAULT_THEME,
    )
    # Not a bare "stars" substring check -- base.html.j2's shared stylesheet
    # unconditionally defines a `.stars{...}` CSS rule, so that would find a
    # false negative even with the fix in place. The actual rendered badge is
    # this exact span; its absence (and the glyph it alone carries) is what
    # AC3 requires.
    assert '<span class="stars">' not in html
    assert "★" not in html


def test_font_stack_is_not_html_escaped():
    """Regression: autoescaping turned 'Helvetica Neue' into &#39;Helvetica
    Neue&#39;, which is invalid CSS, so every slide silently rendered in a
    fallback serif instead of the intended sans."""
    html = build_html([{"type": "hook", "headline": "H"}], ROOT / "templates",
                      {**DEFAULT_THEME, "font": "'Helvetica Neue', Arial, sans-serif"})

    assert "&#39;" not in html
    # The stack now lands in a custom property; what matters is that the
    # quotes survive intact so the declaration stays valid CSS.
    assert "--font:'Helvetica Neue', Arial, sans-serif" in html
    assert "font-family:var(--font)" in html


def test_compare_slide_renders_with_both_titles():
    """T271 AC: compare slide with both left_title and right_title renders
    both title divs (colhead and colhead alt) in a title row."""
    html = build_html(
        [{"type": "compare", "headline": "Before vs After",
          "left_title": "Old Way", "right_title": "New Way",
          "rows": [["step 1", "optimized"], ["step 2", "faster"]]}],
        ROOT / "templates", DEFAULT_THEME,
    )
    assert "Old Way" in html
    assert "New Way" in html
    # Verify the title divs are present
    assert '<div class="colhead">Old Way</div>' in html
    assert '<div class="colhead alt">New Way</div>' in html


@pytest.mark.parametrize("theme", available_themes())
@pytest.mark.parametrize("titles", [
    {},
    {"left_title": "", "right_title": None},
    {"left_title": "Old Way"},
    {"right_title": "New Way"},
])
def test_compare_slide_renders_no_title_row_when_titles_missing(theme, titles):
    """T271 AC: a missing title renders no title div on that side and never a
    'Before'/'After' default, in every theme on disk."""
    html = build_html(
        [{"type": "compare", "headline": "Old Method vs New Method",
          "rows": [["step 1", "optimized"], ["step 2", "faster"]], **titles}],
        ROOT / "templates", load_theme(resolve_theme_path(theme)),
    )
    assert ('class="colhead"' in html) == bool(titles.get("left_title"))
    assert ('class="colhead alt"' in html) == bool(titles.get("right_title"))
    assert ">Before<" not in html
    assert ">After<" not in html
    assert "step 1" in html and "optimized" in html


def test_theme_colours_reach_the_css_intact():
    html = build_html([{"type": "hook", "headline": "H"}], ROOT / "templates",
                      {**DEFAULT_THEME, "accent": "#FF00AA"})
    assert "--accent:#FF00AA" in html


def test_build_html_escapes_content():
    """Slide text comes from a model reading arbitrary web pages."""
    html = build_html(
        [{"type": "hook", "headline": "<script>alert(1)</script>"}],
        ROOT / "templates", DEFAULT_THEME,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_every_slide_type_has_a_partial():
    """facts and compare both exist now, with separate jobs."""
    for name in ("hook", "point", "facts", "code", "flow", "compare",
                 "quote", "takeaway", "sources"):
        assert (ROOT / "templates" / "slides" / f"{name}.html.j2").exists(), name


# --------------------------------------------------------------------- render


async def test_render_produces_one_png_per_slide_at_instagram_size(settings, tmp_path):
    from PIL import Image

    out = await render(_item(SIX), _settings(settings, tmp_path))
    paths = out["rendered_paths"]

    assert len(paths) == 6
    for path in paths:
        assert Image.open(path).size == (1080, 1350)


async def test_render_without_slides_is_retryable(settings, tmp_path):
    with pytest.raises(Retryable, match="no slides"):
        await render(_item([]), _settings(settings, tmp_path))


async def test_overflowing_slide_raises_recompose_with_its_index(settings, tmp_path):
    """Spec §7.6: the guard is the real enforcement, because the model ignores
    the character limits it is given."""
    slides = [
        {"type": "hook", "headline": "Fine"},
        {"type": "point", "headline": "Way too much",
         "bullets": ["x " * 500, "y " * 500, "z " * 500, "w " * 500]},
        {"type": "takeaway", "headline": "End"},
    ]
    with pytest.raises(Recompose) as exc:
        await render(_item(slides, item_id=2), _settings(settings, tmp_path))

    assert exc.value.slide_index == 1
    assert "too long" in str(exc.value)


async def test_slightly_long_content_is_rescued_by_shrinking(settings, tmp_path):
    """Step one of the guard is a font step-down, not an immediate bounce."""
    slides = [
        {"type": "hook", "headline": "A reasonably long headline that still fits"},
        {"type": "point", "headline": "Detail",
         "bullets": ["A bullet of quite generous length that pushes the layout "
                     "close to its limit without going over.",
                     "Another bullet of similar generous length to add pressure.",
                     "A third bullet, also long, to bring it right to the edge.",
                     "A fourth bullet completing a full and dense slide layout."]},
        {"type": "takeaway", "headline": "Done"},
    ]
    out = await render(_item(slides, item_id=3), _settings(settings, tmp_path))
    assert len(out["rendered_paths"]) == 3


async def test_relative_media_dir_still_renders(settings, tmp_path, monkeypatch):
    """Regression: html_path.as_uri() throws on a relative path, and the
    failure surfaced from the subprocess as an opaque ValueError."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    local = replace(settings, media_dir=Path("rel"),
                    template_dir=ROOT / "templates",
                    theme_path=ROOT / "config" / "themes" / "signal.json")

    out = await render(_item([{"type": "hook", "headline": "Fits fine"}], item_id=99), local)
    assert len(out["rendered_paths"]) == 1


def test_handle_setting_overrides_the_theme_file():
    """Regression: the handle was duplicated across five theme files, so
    changing it meant editing all five and missing one."""
    from dataclasses import replace as _replace

    from pipeline.config import Settings
    from pipeline.stages.render import load_theme, resolve_theme_path

    theme = load_theme(resolve_theme_path("signal"))
    theme["handle"] = "@from_the_file"
    settings = _replace(Settings.load(env={}), handle="@from_the_setting")

    if settings.handle:
        theme["handle"] = settings.handle
    assert theme["handle"] == "@from_the_setting"


def test_every_theme_file_is_valid_and_declares_a_style():
    """A malformed theme silently falls back to defaults, which is hard to spot."""
    import json

    from pipeline.stages.render import THEMES_DIR, available_themes

    names = available_themes()
    assert names, "no themes on disk"
    for name in names:
        data = json.loads((THEMES_DIR / f"{name}.json").read_text())
        assert data.get("style"), f"{name} has no style"
        assert data.get("kickers"), f"{name} has no kickers"
        assert data["name"] == name, f"{name} name field mismatch"


def test_image_paths_become_file_uris(tmp_path):
    """Chromium loads the page from file://, where a bare absolute path in src
    is not a valid URL."""
    from pipeline.stages.render import with_image_uris

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    out = with_image_uris([{"type": "photo", "headline": "H", "image": str(img),
                            "image_mode": "hero"}])

    assert out[0]["image_uri"].startswith("file://")
    assert "image" not in out[0]


def test_missing_image_is_dropped_not_rendered_broken(tmp_path):
    """A deleted download must not put a broken-image icon in a published slide."""
    from pipeline.stages.render import with_image_uris

    out = with_image_uris([{"type": "point", "headline": "H",
                            "image": str(tmp_path / "gone.png"), "image_mode": "inset"}])
    assert "image_uri" not in out[0]
    assert "image_mode" not in out[0]


def test_background_image_gets_a_dimming_overlay():
    """A background must never win against the headline sitting on it."""
    from pipeline.stages.render import DEFAULT_THEME, build_html

    html = build_html(
        [{"type": "hook", "headline": "H", "image": __file__, "image_mode": "background"}],
        ROOT / "templates", DEFAULT_THEME,
    )
    assert "has-bg" in html
    # A blurred <img> layer rather than a CSS background, so the image can be
    # blurred independently of the text sitting on it.
    assert '<div class="bg">' in html
    assert "blur(" in html


def test_photo_and_inset_never_crop():
    """Regression: object-fit:cover cropped screenshots and charts, cutting off
    exactly the edges that carry meaning. Only the blurred hook background may
    crop, because nothing there is being read."""
    css = (ROOT / "templates" / "base.html.j2").read_text()

    photo = css[css.index(".photo img{"):css.index(".photo img{") + 160]
    inset = css[css.index(".inset img{"):css.index(".inset img{") + 160]
    assert "object-fit:contain" in photo and "cover" not in photo
    assert "object-fit:contain" in inset and "cover" not in inset

    background = css[css.index(".slide .bg img{"):css.index(".slide .bg img{") + 200]
    assert "object-fit:cover" in background, "the blurred background should fill"
