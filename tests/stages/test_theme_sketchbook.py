"""Every theme file has to be loadable and complete.

A theme that parses but omits a token renders with CSS variables resolving to
nothing, which shows up as invisible text on a 1080x1350 image rather than as
an error. The whole set is checked, not just the newest one.
"""

import json
from pathlib import Path

import pytest

from pipeline.stages.render import (
    DEFAULT_THEME,
    available_themes,
    load_theme,
    resolve_theme_path,
)

THEMES = Path(__file__).resolve().parents[2] / "config" / "themes"
REQUIRED = ("name", "font", "style", "bg", "fg", "muted", "accent", "card", "rule")


@pytest.mark.parametrize("name", available_themes(THEMES))
def test_every_theme_is_complete(name):
    theme = json.loads((THEMES / f"{name}.json").read_text())
    missing = [k for k in REQUIRED if not theme.get(k)]
    assert not missing, f"{name} is missing {missing}"


def test_sketchbook_is_available_and_resolves():
    assert "sketchbook" in available_themes(THEMES)
    assert resolve_theme_path("sketchbook", THEMES).name == "sketchbook.json"


def test_sketchbook_palette_stays_paper():
    """The template paints palette[i] as each slide's background. A saturated
    value turns this theme into a flat colour field and loses the sheet it is
    drawn on — the first version shipped an orange palette and every slide
    came out orange."""
    theme = json.loads((THEMES / "sketchbook.json").read_text())
    for colour in theme["palette"]:
        r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        assert min(r, g, b) > 0xDD, f"{colour} is too dark to read as paper"
        assert max(r, g, b) - min(r, g, b) < 0x30, f"{colour} is too saturated"


def test_sketchbook_asks_for_a_handwritten_face_with_fallbacks():
    """If the first face is absent the slide must still look hand-drawn, not
    fall back to the system sans."""
    theme = json.loads((THEMES / "sketchbook.json").read_text())
    faces = theme["font"].lower()
    assert "chalkboard" in faces
    assert faces.count(",") >= 2, "needs fallbacks"
    assert faces.strip().endswith("cursive")


def test_kickers_cover_every_slide_type_the_deck_can_emit():
    from pipeline.stages.compose import SLIDE_TYPES
    theme = load_theme(THEMES / "sketchbook.json")
    for kind in SLIDE_TYPES:
        assert theme["kickers"].get(kind), f"no kicker for {kind}"


def test_a_missing_theme_file_falls_back_rather_than_raising():
    assert resolve_theme_path("no-such-theme", THEMES) is None
    fallback = load_theme(None)
    assert fallback["style"] == DEFAULT_THEME["style"]
    assert all(fallback.get(k) for k in ("font", "bg", "fg", "accent"))
