"""Render: slide JSON -> HTML -> PNG.

This stage holds the *actual* enforcement of slide length. The composer is told
character limits and does not reliably respect them — the PoC produced a
243-character field against a stated limit of 110. So the guard here measures
real layout and sends the item back for shorter copy when text will not fit.
It costs no tokens and cannot be argued with.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from ..errors import Recompose, Retryable
from ..models import Item

log = logging.getLogger(__name__)

RENDER_TIMEOUT_S = 180

THEMES_DIR = Path(__file__).resolve().parents[3] / "config" / "themes"
# Webfonts bundled for themes that need them (doodle). Served over file:// so a
# render never needs the network.
FONTS_DIR = THEMES_DIR.parent / "fonts"
# The hook headline underlines one word; look only at the opening words so the
# mark lands on the subject, not on something near the end.
UNDERLINE_WINDOW = 3
# Themes that never draw images. THEME=random/rotate must not hand one of these
# a deck with a photo slide: that slide would become a headline about a picture
# that is not there. An explicitly pinned theme is still honoured.
TEXT_ONLY_THEMES = frozenset({"doodle"})
PHOTO_SLIDE_TYPE = "photo"

DEFAULT_THEME = {
    "bg": "#0B0D12", "fg": "#F2F5FA", "muted": "#8A94A6",
    "accent": "#4F8CFF", "accent2": "#8B5CF6", "card": "#141824",
    "rule": "#232838", "handle": "@yourhandle",
    "font": "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif",
    "kickers": {
        "hook": "AI NEWS", "point": "THE DETAIL", "facts": "THE NUMBERS",
        "takeaway": "WHY IT MATTERS", "sources": "SOURCES",
    },
    "style": "signal",
}


def available_themes(themes_dir: Path | None = None) -> list[str]:
    """Theme names on disk, sorted, so rotation is stable across restarts."""
    directory = Path(themes_dir or THEMES_DIR)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def resolve_theme_path(name: str, themes_dir: Path | None = None,
                       item_id: int = 0, exclude: frozenset[str] = frozenset()) -> Path | None:
    """Map a theme setting to a file.

    ``rotate`` cycles by item id rather than at random: a given item always
    renders the same way, so a regenerate does not silently change the look
    while the operator is comparing two previews.
    """
    directory = Path(themes_dir or THEMES_DIR)
    names = available_themes(directory)
    if name in ("random", "rotate", ""):
        # ``exclude`` only narrows the automatic pick; a named theme is a pin.
        names = [n for n in names if n not in exclude] or names
    if not names:
        return None
    if name == "random":
        # Deterministic per item so a re-render is not a moving target, but
        # spread across the set rather than cycling in file order.
        index = (item_id * 2654435761) % len(names)
        return directory / f"{names[index]}.json"
    if name in ("rotate", ""):
        return directory / f"{names[item_id % len(names)]}.json"
    candidate = directory / f"{name}.json"
    return candidate if candidate.exists() else None


def load_theme(path: Path | str | None) -> dict:
    """Theme tokens, falling back to defaults so a missing file cannot block a post."""
    if not path:
        return dict(DEFAULT_THEME)
    try:
        theme = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("theme %s unreadable (%s); using defaults", path, exc)
        return dict(DEFAULT_THEME)
    merged = dict(DEFAULT_THEME)
    merged.update(theme)
    merged["kickers"] = {**DEFAULT_THEME["kickers"], **theme.get("kickers", {})}
    return merged


def with_image_uris(slides: list[dict]) -> list[dict]:
    """Turn image paths into file:// URIs Chromium will actually load.

    The page is served from a file:// URL, and a bare absolute path in src is
    not a valid URL there. A missing file drops the image rather than
    rendering a broken-image icon into a published slide.
    """
    out = []
    for slide in slides:
        entry = dict(slide)
        logo = entry.pop("logo", None)
        if logo and Path(logo).exists():
            entry["logo_uri"] = Path(logo).resolve().as_uri()

        path = entry.pop("image", None)
        if path:
            resolved = Path(path)
            if resolved.exists():
                entry["image_uri"] = resolved.resolve().as_uri()
            else:
                log.warning("image missing, dropping from slide: %s", path)
                entry.pop("image_mode", None)
        out.append(entry)
    return out


def doodle_underline(headline: str) -> Markup:
    """Wrap the longest of the first few words in ``<span class="ul">``.

    The doodle theme draws one red hand-drawn stroke under a single word of the
    hook, as the reference does; the longest early word is the most likely
    subject ("Bullsh*t", not "The"). Everything is escaped. Runs of spaces are
    preserved and never count as words.
    """
    tokens = re.split(r"( +)", str(headline or ""))
    word_idx = [i for i, tok in enumerate(tokens) if tok and not tok.isspace()]
    if not word_idx:
        return Markup(escape(str(headline or "")))
    window = word_idx[:UNDERLINE_WINDOW]
    pick = max(window, key=lambda i: (len(tokens[i]), -i))
    return Markup("").join(
        Markup('<span class="ul">{}</span>').format(tok) if i == pick else escape(tok)
        for i, tok in enumerate(tokens)
    )


def build_html(slides: list[dict], template_dir: Path, theme: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["doodle_underline"] = doodle_underline
    return env.get_template("base.html.j2").render(
        slides=with_image_uris(slides), theme=theme, fonts_uri=FONTS_DIR.resolve().as_uri()
    )


def wanted_theme(item: Item, configured: str | None) -> str:
    """The theme NAME (or rotate/random) this item renders with.

    Precedence, highest first (D256):
      1. ``item.theme_pin`` -- the operator asked for this theme on this item.
         An unknown name (theme file removed since) is ignored, not honoured.
      2. the configured ``THEME`` setting: a name, ``rotate`` or ``random``.
      3. ``item.theme`` -- the composer's pick for the story. It is only a
         suggestion: ``items.theme`` stores it and is NOT the theme that was
         rendered whenever 1 or 2 applies (T283: documented, not rewritten).

    The composer picks the same theme almost every time (6 of 8 decks came out
    identical), so an explicit setting overrides it; an empty setting defers
    to the model. Items with no pin behave exactly as before.
    """
    pin = (getattr(item, "theme_pin", None) or "").strip()
    if pin:
        if pin in available_themes():
            return pin
        log.warning("item %s: theme_pin %r is not an available theme; ignoring", item.id, pin)
    return (configured or "").strip() or item.theme or ""


async def render(item: Item, settings) -> dict:
    slides = item.slides or []
    if not slides:
        raise Retryable("nothing to render: item has no slides")

    wanted = wanted_theme(item, getattr(settings, "theme", ""))
    has_photo = any(s.get("type") == PHOTO_SLIDE_TYPE for s in slides)
    if has_photo and wanted in TEXT_ONLY_THEMES:
        log.warning("item %s: text-only theme %r pinned on a deck with a photo slide", item.id, wanted)
    chosen = resolve_theme_path(
        wanted, item_id=item.id,
        exclude=TEXT_ONLY_THEMES if has_photo else frozenset(),
    )
    theme = load_theme(chosen or getattr(settings, "theme_path", None))
    # One handle, set once, wins over whatever each theme file carries.
    if getattr(settings, "handle", ""):
        theme["handle"] = settings.handle
    log.info("item %s rendering with theme %r", item.id, theme.get("name", "default"))
    html = build_html(slides, Path(settings.template_dir), theme)

    # as_uri() below requires an absolute path; a relative media_dir would
    # otherwise fail inside the subprocess with an opaque ValueError.
    out_dir = (Path(settings.media_dir).resolve() / str(item.id))
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "slides.html"
    html_path.write_text(html, encoding="utf-8")

    result = await _run_worker(html_path, out_dir, len(slides), prefix=f"item{item.id}")

    overflows = result.get("overflows") or []
    if overflows:
        first = overflows[0]
        slide = slides[first]
        raise Recompose(
            first,
            f"the {slide.get('type', 'slide')} content is too long for the layout "
            f"even after shrinking",
        )

    paths = result.get("paths") or []
    if len(paths) != len(slides):
        raise Retryable(f"expected {len(slides)} images, renderer produced {len(paths)}")
    return {"rendered_paths": paths}


async def _run_worker(html_path: Path, out_dir: Path, count: int, prefix: str) -> dict:
    """Drive the renderer in a separate process and parse its JSON result."""
    # The child does not inherit an editable install or pytest's pythonpath, so
    # point it at the source root explicitly.
    src_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(src_root)]
    )

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pipeline.render_worker",
        str(html_path), str(out_dir), str(count), prefix,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=RENDER_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise Retryable(f"renderer timed out after {RENDER_TIMEOUT_S}s")

    if process.returncode != 0:
        # The worker reports structured failures on stdout; stderr alone is
        # often empty, which makes the error message useless.
        out = (stdout or b"").decode(errors="replace")[-400:]
        err = (stderr or b"").decode(errors="replace")[-400:]
        raise Retryable(
            f"renderer exited {process.returncode}: {out or err or '(no output)'}"
        )

    try:
        result = json.loads((stdout or b"").decode())
    except json.JSONDecodeError:
        raise Retryable(f"renderer produced no parseable result: {stdout[:200]!r}")

    if "error" in result:
        raise Retryable(f"renderer failed: {result['error']}")
    return result
