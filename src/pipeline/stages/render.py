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

DEFAULT_THEME = {
    "bg": "#0B0D12", "fg": "#F2F5FA", "muted": "#8A94A6",
    "accent": "#4F8CFF", "accent2": "#8B5CF6", "card": "#141824",
    "rule": "#232838", "handle": "@yourhandle",
    "font": "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif",
    "kickers": {
        "hook": "AI NEWS", "point": "THE DETAIL", "facts": "THE NUMBERS",
        "takeaway": "WHY IT MATTERS", "sources": "SOURCES",
    },
}


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


def build_html(slides: list[dict], template_dir: Path, theme: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    return env.get_template("base.html.j2").render(slides=slides, theme=theme)


async def render(item: Item, settings) -> dict:
    slides = item.slides or []
    if not slides:
        raise Retryable("nothing to render: item has no slides")

    theme = load_theme(getattr(settings, "theme_path", None))
    html = build_html(slides, Path(settings.template_dir), theme)

    out_dir = Path(settings.media_dir) / str(item.id)
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
        detail = (stderr or b"").decode(errors="replace")[-400:]
        raise Retryable(f"renderer exited {process.returncode}: {detail}")

    try:
        result = json.loads((stdout or b"").decode())
    except json.JSONDecodeError:
        raise Retryable(f"renderer produced no parseable result: {stdout[:200]!r}")

    if "error" in result:
        raise Retryable(f"renderer failed: {result['error']}")
    return result
