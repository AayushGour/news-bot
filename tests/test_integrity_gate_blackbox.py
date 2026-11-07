"""T277 tester blackbox: slide_backed as T251 will call it, on realistic slides."""
import pytest

from pipeline.integrity import slide_backed

SNIPPET = "def load(path):\n    with open(path) as fh:\n        return json.load(fh)"
MATERIAL = (
    "Usage guide. First: import os\nThen the loader:\n"
    "def load(path):\n    with open(path) as fh:\n        return json.load(fh)\n"
    "Results: a 3.2x gain, 40 per cent fewer errors; 3.2× faster; three months; one of the options."
)


def code(c):
    return {"type": "code", "code": c}


def kpi(*values):
    return {"type": "kpi", "tiles": [{"value": v, "label": "x"} for v in values]}


def chart(*displays):
    return {"type": "chart", "series": [{"label": "s", "value": None, "display": d} for d in displays]}


@pytest.mark.parametrize("slide,material,expected", [
    (code("import os"), MATERIAL, False),
    (code("import os"), "x\nimport os\ny", False),
    (code(SNIPPET), MATERIAL, True),
    (kpi("65%"), "revenue hit $65B", False),
    (kpi("3.2x"), "it is 3.2× faster", True),
    (kpi("Three"), "three months", True),
    (kpi("1"), "one of the options", False),
    (kpi(""), MATERIAL, False),
    (kpi(), MATERIAL, False),
    (chart("3.2×", "40%"), "a 3.2x gain, 40 per cent", True),
    ({"type": "text"}, "", True),
])
def test_slide_backed_realistic(slide, material, expected):
    assert slide_backed(slide, material) is expected
