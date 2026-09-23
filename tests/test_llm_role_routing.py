"""T71: role routing (which LLMClient method — cheap/good/vision — serves a
call) must be config-driven for the research note, and unchanged everywhere
else.

This is the "full role census" acceptance criterion: every hardcoded
``llm.<role>(...)`` call site in ``src/pipeline`` is enumerated with a real
AST walk (not a text/regex match, which would also catch the literal
"llm.cheap(system, user, schema)" that appears in budget.py's own
docstring) and pinned against a frozen expected set. A future call site
silently changing role — or the research note call regressing back to a
hardcoded literal — fails this test instead of drifting unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pipeline"

_ROLE_METHODS = {"cheap", "good", "vision"}


def _hardcoded_role_calls(path: Path) -> list[str]:
    """Every ``llm.<role>(...)`` call actually present in ``path``'s AST —
    never a docstring/comment mention, and never a ``getattr(llm, ...)``
    dispatch (research.py's note call, post-T71)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    roles = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _ROLE_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "llm"
        ):
            roles.append(node.func.attr)
    return roles


def _actual_census() -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        for role in _hardcoded_role_calls(path):
            key = (rel, role)
            counts[key] = counts.get(key, 0) + 1
    return counts


#: The full, frozen census of hardcoded ``llm.<role>(...)`` call sites, as
#: (file relative to src/pipeline, role) -> count — never line numbers,
#: which shift on unrelated edits and would fail this test for the wrong
#: reason (coding-standards.md: "assert the property, not the target").
#:
#: research.py's note-writing call is deliberately ABSENT: T71 made it a
#: config-driven ``getattr(llm, settings.research_note_role)(...)``
#: dispatch, not a hardcoded call, so it cannot appear here by construction.
#: research.py's other two calls (the query planner, the relevance gate)
#: are unchanged and still hardcoded to ``cheap`` per the AC ("the relevance
#: gate stays on cheap").
EXPECTED_CENSUS: dict[tuple[str, str], int] = {
    ("coverage.py", "cheap"): 1,
    ("expand.py", "cheap"): 1,
    ("stages/compose.py", "good"): 3,
    ("stages/enumerate_items.py", "cheap"): 1,
    ("stages/enumerate_items.py", "good"): 1,
    ("stages/extract.py", "vision"): 1,
    ("stages/research.py", "cheap"): 2,  # plan_queries + the relevance gate
    ("stages/synthesize.py", "good"): 2,
    ("stages/triage.py", "cheap"): 1,
}


def test_the_full_hardcoded_role_census_is_unchanged_except_by_design():
    """Pins every hardcoded ``llm.<role>(...)`` call site in
    ``src/pipeline``. A diff here means some call site's role changed —
    which must be this task's note call moving to config, not a new
    hardcoded literal appearing (or disappearing) anywhere else."""
    assert _actual_census() == EXPECTED_CENSUS


def test_research_py_routes_the_note_call_through_config_not_a_literal():
    """research.py must have exactly 2 hardcoded ``llm.cheap(...)`` call
    sites (the query planner and the relevance gate) and ZERO hardcoded
    ``llm.good(...)`` — the note-writing call is a
    ``getattr(llm, settings.research_note_role)(...)`` dispatch, not a
    literal, so the experiment stays a one-line config flip (T71)."""
    calls = _hardcoded_role_calls(SRC / "stages" / "research.py")
    assert calls.count("cheap") == 2
    assert calls.count("good") == 0

    text = (SRC / "stages" / "research.py").read_text()
    assert "settings.research_note_role" in text
