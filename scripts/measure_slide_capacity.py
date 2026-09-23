"""Measure how many body items each slide type/field can hold before the
renderer's overflow guard trips — pre-shrink, across every theme on disk.

``normalise_slides`` used to cap every list-shaped body field at a flat,
uncited number (``bullets[:4]``, ``rows[:4]``, ...). Nobody had measured what
the layout actually holds, and the number shipped was roughly half the real
capacity — the user-visible bug this script exists to prevent recurring:
a headline promising 7 things over a body that only ever shows 4.

Capacity is a SURFACE, not a number (house rule 4 / D51):
``(type, field, chars-per-item) -> max count``. This script builds real pages
through the actual render path — ``pipeline.stages.render.build_html`` /
``load_theme`` (no second HTML builder) — and measures with the actual
overflow predicate the renderer enforces at publish time —
``pipeline.render_worker.OVERFLOWS_JS`` (no re-typed predicate). It makes
ZERO model calls, launches Chromium exactly once, takes no screenshots, and
renders every candidate for a given theme on one page load.

Nothing here enters the pytest suite (D44): ``tests/stages/test_render.py``
already drives Chromium and is one of five known flaky-under-load tests.
This script's printed table, pasted into the task log, IS the evidence.

    ./.venv/bin/python scripts/measure_slide_capacity.py

Numbers go stale the moment ``templates/base.html.j2``, a slide template, or
a theme file changes — re-run and re-paste the table in the same task that
touches any of those (house rule 4).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.render_worker import OVERFLOWS_JS  # noqa: E402
from pipeline.stages.render import (  # noqa: E402
    THEMES_DIR,
    available_themes,
    build_html,
    load_theme,
)

TEMPLATE_DIR = ROOT / "templates"
VIEWPORT = {"width": 1080, "height": 1350}

#: A fixed word bank, wrapped to any requested length. Deterministic: the
#: same length always yields the same text, so a capacity measured today is
#: the capacity measured tomorrow. Lowercase English is the fixed fill
#: (D51) — real copy with capitals, digits or wide glyphs (CJK, emoji) may
#: run narrower than what this predicts.
_FILL_WORDS = (
    "the quick brown fox jumps over a lazy dog while calm winds drift across "
    "open fields and slow rivers carry small boats past distant hills under "
    "grey skies near a quiet town where old roads wind through green valleys "
    "and morning light spreads over wide plains beyond the last bend "
)


def fill_text(length: int) -> str:
    """``length`` characters of deterministic lowercase filler prose."""
    if length <= 0:
        return ""
    repeated = _FILL_WORDS * (length // len(_FILL_WORDS) + 2)
    return repeated[:length]


def _text_item(chars: int) -> str:
    return fill_text(chars)


def _row_builder(a_max: int, b_max: int) -> Callable[[int], list]:
    def build(_chars: int) -> list:
        return [fill_text(a_max), fill_text(b_max)]
    return build


def _step_builder(label_max: int, detail_max: int) -> Callable[[int], dict]:
    def build(_chars: int) -> dict:
        return {"label": fill_text(label_max), "detail": fill_text(detail_max)}
    return build


def _tile_builder(value_max: int, label_max: int, delta_max: int) -> Callable[[int], dict]:
    def build(_chars: int) -> dict:
        return {"value": fill_text(value_max), "label": fill_text(label_max),
                "delta": fill_text(delta_max)}
    return build


def _series_builder(label_max: int, display_max: int) -> Callable[[int], dict]:
    def build(_chars: int) -> dict:
        return {"label": fill_text(label_max), "value": 100,
                "display": fill_text(display_max)}
    return build


@dataclass(frozen=True)
class FieldConfig:
    """One ``(type, field, chars-per-item)`` capacity surface to measure.

    ``headline``/``siblings`` are the worst-case companions present on every
    candidate slide — a capacity without its configuration is not
    reproducible (house rule 4). ``max_count`` bounds how many candidates get
    tested; the run prints a warning if a config never overflows within that
    bound, so a hidden higher ceiling does not silently pass as "measured".

    ``bounded=True`` marks a config whose ``max_count`` is a deliberate
    CEILING, not a probe for the real overflow point (see "kpi.tiles"
    below — ``base.html.j2`` has no CSS grid rule past 4 tiles, so testing a
    5th measures a different, unsupported layout, not this one). Such a
    config is EXPECTED to never overflow within ``max_count`` on every theme,
    every run — the warning firing for it is not a finding, and printing it
    anyway (11 times, once per theme, on every single invocation) trains a
    reader to stop reading the warning that matters for the fields whose
    ceiling genuinely is unknown.
    """

    kind: str
    field: str
    label: str
    headline: str
    siblings: dict
    build_item: Callable[[int], object]
    chars: int
    max_count: int = 20
    note: str = ""
    bounded: bool = False


_LOGO_PATH: Path | None = None  # set once per run in main()


#: The RICH row per field — one worst-case, documented (or clearly assumed)
#: per-item length. These are what MAX_BODY_ITEMS is derived from.
RICH: list[FieldConfig] = [
    FieldConfig(
        "point", "bullets", "point.bullets (rich)",
        headline=fill_text(45), siblings={},
        build_item=_text_item, chars=95, max_count=16,
        note="headline<=45, bullet<=95 (SYSTEM prompt). 'stat' is documented "
             "as an ALTERNATIVE to bullets, not a companion of them — "
             "normalise_slides' own required_any=('bullets','stat','sub') "
             "treats point as satisfied by any ONE of the three, and the "
             "prompt never asks for bullets plus a stat callout on the same "
             "slide — so stat is not included as a worst-case sibling here.",
    ),
    FieldConfig(
        "repo", "bullets", "repo.bullets (rich)",
        headline="",
        siblings={
            "owner": fill_text(20), "name": fill_text(35), "language": fill_text(12),
            "stars": "128000", "sub": fill_text(110),
            "url": "https://github.com/" + fill_text(45),
        },
        build_item=_text_item, chars=60, max_count=16,
        note="no headline rendered by repo.html.j2; bullet<=60 (DECK SHAPE); "
             "logo/owner/name/badge/stars/sub(<=110)/url all present — the "
             "worst-case companions a real list-deck repo card carries (D47)",
    ),
    FieldConfig(
        "facts", "rows", "facts.rows (rich)",
        headline=fill_text(45), siblings={},
        build_item=_row_builder(30, 34), chars=0, max_count=16,
        note="headline<=45, row=[label<=30, value<=34] (SYSTEM prompt). No "
             "'sub' sibling: facts' documented spec is headline+rows only.",
    ),
    FieldConfig(
        "compare", "rows", "compare.rows (rich)",
        headline=fill_text(45),
        siblings={"left_title": fill_text(22), "right_title": fill_text(22)},
        build_item=_row_builder(40, 40), chars=0, max_count=16,
        note="headline<=45, left_title/right_title<=22, row=[left<=40, right<=40]",
    ),
    FieldConfig(
        "sources", "urls", "sources.urls (rich)",
        headline=fill_text(45), siblings={},
        build_item=_text_item, chars=70, max_count=24,
        note="headline<=45; no per-url length in the prompt — 70 chars stands "
             "in for a realistic article URL. No 'sub' sibling: sources' "
             "documented spec is headline+urls only.",
    ),
    FieldConfig(
        "flow", "steps", "flow.steps (rich)",
        headline=fill_text(45), siblings={},
        build_item=_step_builder(26, 70), chars=0, max_count=16,
        note="headline<=45, step={label<=26, detail<=70}. No 'sub' sibling: "
             "flow's documented spec is headline+steps only.",
    ),
    FieldConfig(
        "kpi", "tiles", "kpi.tiles (rich)",
        headline=fill_text(45), siblings={},
        build_item=_tile_builder(10, 28, 10), chars=0, max_count=4,
        note="headline<=45, tile={value<=10, label<=28, delta<=10}. Capped at "
             "4 candidates deliberately: base.html.j2 only defines "
             ".kpis-2/.kpis-3/.kpis-4 grid-template-columns — a 5th tile gets "
             "no column rule and silently stacks in one column, which is not "
             "the layout being measured. The SYSTEM prompt already says "
             "'2-4 tiles'; this confirms it rather than re-deriving it.",
        bounded=True,
    ),
    FieldConfig(
        "chart", "series", "chart.series (rich)",
        headline=fill_text(45),
        siblings={"caption": fill_text(80), "unit": fill_text(12)},
        build_item=_series_builder(20, 12), chars=0, max_count=16,
        note="headline<=45, caption<=80, unit<=12, series item={label<=20, "
             "display: no cap stated — 12 chars assumed, matching kpi's delta}",
    ),
    FieldConfig(
        "links", "links", "links.links (rich)",
        headline=fill_text(45), siblings={"sub": fill_text(60)},
        build_item=_text_item, chars=70, max_count=30,
        note="headline: no cap stated for the links type in the main prompt "
             "block (an outright gap this task closes) — 45 chars assumed, "
             "matching the common per-type convention; sub<=60 assumed "
             "(ensure_links_slide's own sub is 23 chars); no per-url length "
             "stated — 70 chars stands in for a realistic URL, as with sources",
    ),
]

#: DENSE rows — extra data points for T103's future densified list cards
#: (D47: several items per slide, "name - summary . language . stars"),
#: NOT used to set MAX_BODY_ITEMS. point.bullets at 95/72/56/40 chars/bullet
#: with no stat/sub sibling (with_sub=False); links.links at 48/64 chars/url.
DENSE: list[FieldConfig] = [
    FieldConfig(
        "point", "bullets", f"point.bullets DENSE ({chars} chars, with_sub=False)",
        headline=fill_text(45), siblings={},
        build_item=_text_item, chars=chars, max_count=20,
        note="no stat, no sub — the densified card is bullets alone",
    )
    for chars in (95, 72, 56, 40)
] + [
    FieldConfig(
        "links", "links", f"links.links DENSE ({chars} chars/url)",
        headline=fill_text(45), siblings={"sub": fill_text(60)},
        build_item=_text_item, chars=chars, max_count=30,
    )
    for chars in (48, 64)
]


def _build_slide(cfg: FieldConfig, count: int) -> dict:
    slide: dict = {"type": cfg.kind, "headline": cfg.headline, **cfg.siblings}
    if cfg.kind == "repo" and _LOGO_PATH is not None:
        slide["logo"] = str(_LOGO_PATH)
    slide[cfg.field] = [cfg.build_item(cfg.chars) for _ in range(count)]
    return slide


async def _measure_theme(
    page, theme: dict, configs: list[FieldConfig],
) -> dict[str, int]:
    """Render every (config, count) candidate for one theme on ONE page load
    and return, for each config label, the largest pre-shrink count that does
    not overflow."""
    slides: list[dict] = []
    spans: dict[str, tuple[int, int]] = {}
    by_label = {cfg.label: cfg for cfg in configs}
    for cfg in configs:
        start = len(slides)
        for n in range(1, cfg.max_count + 1):
            slides.append(_build_slide(cfg, n))
        spans[cfg.label] = (start, cfg.max_count)

    html = build_html(slides, TEMPLATE_DIR, theme)
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "capacity.html"
        html_path.write_text(html, encoding="utf-8")
        await page.goto(html_path.as_uri())
        await page.wait_for_timeout(150)  # let webfonts and layout settle
        overflowed = await page.eval_on_selector_all(
            ".slide", f"els => els.map({OVERFLOWS_JS})"
        )

    results: dict[str, int] = {}
    for label, (start, count) in spans.items():
        best = 0
        for i in range(count):
            if overflowed[start + i]:
                break
            best = i + 1
        # N6: a bounded config (kpi.tiles) is EXPECTED to hit its ceiling
        # every time on every theme — that is what "deliberate ceiling"
        # means, not a hidden higher capacity waiting to be found. Printing
        # the warning anyway made it fire on all 11 themes, every run, which
        # trained a reader to stop reading the one warning that DOES mean
        # something (a field whose real ceiling is genuinely still unknown).
        if best == count and not by_label[label].bounded:
            print(f"  WARNING: {label} never overflowed within {count} "
                  f"candidates — raise max_count to find the real ceiling",
                  file=sys.stderr)
        results[label] = best
    return results


def _make_logo(tmp_dir: Path) -> Path:
    from PIL import Image
    path = tmp_dir / "logo.png"
    Image.new("RGB", (96, 96), color=(80, 90, 120)).save(path)
    return path


async def main() -> None:
    global _LOGO_PATH

    from playwright.async_api import async_playwright

    names = available_themes(THEMES_DIR)
    if not names:
        print("no theme files found", file=sys.stderr)
        raise SystemExit(1)

    configs = RICH + DENSE

    print("=" * 78)
    print("FILL-TEXT GENERATOR (deterministic; same length -> same text)")
    print("=" * 78)
    print(inspect.getsource(fill_text))

    print("=" * 78)
    print("CONFIGURATION — one row per (type, field), worst-case companions")
    print("=" * 78)
    for cfg in configs:
        sib = ", ".join(sorted(cfg.siblings)) or "(none)"
        print(f"- {cfg.label}")
        print(f"    headline={len(cfg.headline)} chars  siblings=[{sib}]  "
              f"item_chars={cfg.chars}  candidates_tested=1..{cfg.max_count}")
        if cfg.note:
            print(f"    {cfg.note}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        _LOGO_PATH = _make_logo(Path(tmp))

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
                per_theme: dict[str, dict[str, int]] = {}
                for name in names:
                    theme = load_theme(THEMES_DIR / f"{name}.json")
                    per_theme[name] = await _measure_theme(page, theme, configs)
            finally:
                await browser.close()

    print("=" * 78)
    print("CAPACITY TABLE — pre-shrink max count that fits, per theme")
    print("=" * 78)
    col = max(len(c.label) for c in configs) + 2
    header = "field".ljust(col) + "".join(n[:10].ljust(11) for n in names) + "MIN"
    print(header)
    minimums: dict[str, int] = {}
    for cfg in configs:
        row = [per_theme[n][cfg.label] for n in names]
        minimum = min(row)
        minimums[cfg.label] = minimum
        print(cfg.label.ljust(col) + "".join(str(v).ljust(11) for v in row)
              + str(minimum))

    print()
    print("=" * 78)
    print("MAX_BODY_ITEMS — minimum across all themes, RICH rows only")
    print("=" * 78)
    print("MAX_BODY_ITEMS: dict[tuple[str, str], int] = {")
    for cfg in RICH:
        print(f'    ("{cfg.kind}", "{cfg.field}"): {minimums[cfg.label]},  '
              f'# {cfg.label}')
    print("}")


if __name__ == "__main__":
    asyncio.run(main())
