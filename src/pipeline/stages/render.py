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
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..errors import Recompose, Retryable
from ..models import Item

log = logging.getLogger(__name__)

RENDER_TIMEOUT_S = 180

THEMES_DIR = Path(__file__).resolve().parents[3] / "config" / "themes"

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
                       item_id: int = 0) -> Path | None:
    """Map a theme setting to a file.

    ``rotate`` cycles by item id rather than at random: a given item always
    renders the same way, so a regenerate does not silently change the look
    while the operator is comparing two previews.
    """
    directory = Path(themes_dir or THEMES_DIR)
    names = available_themes(directory)
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


def build_html(slides: list[dict], template_dir: Path, theme: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    return env.get_template("base.html.j2").render(
        slides=with_image_uris(slides), theme=theme
    )


async def render(item: Item, settings) -> dict:
    slides = item.slides or []
    if not slides:
        raise Retryable("nothing to render: item has no slides")

    # The composer picks a theme to match the story, but left to itself it
    # picks the same one almost every time — 6 of 8 decks came out identical.
    # An explicit rotate/random setting therefore overrides it; a named theme
    # in the setting is a hard pin; only an empty setting defers to the model.
    configured = (getattr(settings, "theme", "") or "").strip()
    if configured in ("rotate", "random"):
        wanted = configured
    else:
        wanted = configured or item.theme or ""
    chosen = resolve_theme_path(wanted, item_id=item.id)
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
