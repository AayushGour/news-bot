"""Browserless guards for T240: code-slide lines must wrap, and the overflow
predicate must see horizontal / clipped-child loss. Layout truth itself is
measured by scripts/measure_slide_capacity.py (D44), not asserted here."""

import re
from pathlib import Path

from pipeline.render_worker import OVERFLOWS_JS

BASE = Path(__file__).resolve().parents[2] / "templates" / "base.html.j2"


def _rule(selector: str) -> str:
    css = BASE.read_text()
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def test_code_pre_wraps_instead_of_clipping():
    rule = _rule(".codeblock pre")
    assert "white-space:pre-wrap" in rule.replace(" ", "")
    assert "overflow-wrap:anywhere" in rule.replace(" ", "")


def test_predicate_checks_width_and_clipped_children():
    assert "scrollWidth > n.clientWidth" in OVERFLOWS_JS
    assert "scrollHeight > n.clientHeight" in OVERFLOWS_JS
    assert "querySelectorAll" in OVERFLOWS_JS
    assert "getComputedStyle" in OVERFLOWS_JS
