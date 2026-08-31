"""Headless-Chromium screenshot worker. Runs as its own process.

Chromium OOMs are routine at this image size. Isolating it here means a browser
crash kills only this subprocess, never the Telethon connection holding the
operator's session.

Invoked as::

    python -m pipeline.render_worker <html_path> <out_dir> <slide_count> <prefix>

Writes a single JSON object to stdout: ``{"paths": [...], "overflows": [...]}``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

VIEWPORT = {"width": 1080, "height": 1350}

# A couple of pixels of slack: sub-pixel line-height rounding otherwise reports
# overflow on slides that look perfectly fine.
OVERFLOW_SLACK_PX = 2

_OVERFLOWS_JS = f"""
el => {{
  const body = el.querySelector('.body');
  return body.scrollHeight > body.clientHeight + {OVERFLOW_SLACK_PX};
}}
"""


async def render(html_path: Path, out_dir: Path, count: int, prefix: str) -> dict:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    overflows: list[int] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
            await page.goto(html_path.as_uri())
            await page.wait_for_timeout(250)  # let webfonts and layout settle

            for index in range(count):
                slide = page.locator(f"#slide-{index}")

                if await slide.evaluate(_OVERFLOWS_JS):
                    # Step one: shrink and re-measure.
                    await slide.evaluate("el => el.setAttribute('data-shrink', '1')")
                    await page.wait_for_timeout(60)
                    if await slide.evaluate(_OVERFLOWS_JS):
                        overflows.append(index)

                target = out_dir / f"{prefix}_slide_{index + 1:02d}.png"
                await slide.screenshot(path=str(target))
                paths.append(str(target))
        finally:
            await browser.close()

    return {"paths": paths, "overflows": overflows}


def main() -> int:
    if len(sys.argv) != 5:
        print(json.dumps({"error": "usage: html_path out_dir count prefix"}))
        return 2
    html_path, out_dir, count, prefix = sys.argv[1:]
    try:
        result = asyncio.run(render(Path(html_path), Path(out_dir), int(count), prefix))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
