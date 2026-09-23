"""`clear_bytecode` must clear every dir a Mutation can target, not a
hardcoded guess about which dirs those are (B4, T74 bounce 1).

D78 already named this failure mode once, one file down: an anchor
(`FILLER_PHRASES`) that duplicates a fact owned elsewhere drifts silently.
`clear_bytecode`'s old `for root_dir in ("src", "scripts")` was the identical
mistake one level up -- a second, independently-typed copy of the set of top-
level dirs `MUTATIONS` already targets. The fix derives that set from
`MUTATIONS` itself; this test proves the derivation is real by adding a
`Mutation` against a top-level dir the hardcoded tuple never listed and
checking its `__pycache__` gets cleared anyway.

No LLM calls, no subprocess calls -- `clear_bytecode()` itself is pure
filesystem cleanup.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: A throwaway top-level dir name that is not any real project directory and
#: is not any existing `Mutation.path`'s top-level component -- guarantees
#: the hardcoded ("src", "scripts") tuple `clear_bytecode` used to have would
#: NOT have cleared it, while a derivation from `MUTATIONS` would.
_FAKE_ROOT_DIR = "zz_mutate_test_fixture_only"


@pytest.fixture
def mutate():
    spec = importlib.util.spec_from_file_location(
        "mutate_under_test", ROOT / "scripts" / "mutate.py")
    module = importlib.util.module_from_spec(spec)
    # mutate.py defines a @dataclass; the dataclass machinery resolves
    # forward-referenced annotations via sys.modules[cls.__module__], which
    # only exists once the module is registered -- unlike eval_compose.py /
    # harvest_filler_phrases.py (no dataclasses), this module must be
    # registered in sys.modules BEFORE exec_module runs the class body.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_pycache(root_dir: str) -> Path:
    cache_dir = ROOT / root_dir / "sub" / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "fake_module.cpython-313.pyc").write_bytes(b"not real bytecode")
    return cache_dir


def test_clear_bytecode_derives_its_dirs_from_mutations(mutate, monkeypatch):
    # A Mutation targeting a dir NOT in the old hardcoded ("src", "scripts")
    # tuple. If clear_bytecode ever regresses to that hardcoded tuple, this
    # fixture's __pycache__ survives the call and the assertion below fails.
    fake_mutation = mutate.Mutation(
        "fixture-only", f"{_FAKE_ROOT_DIR}/module.py", "old", "new",
        "tests/test_mutate.py", "proves clear_bytecode derives from MUTATIONS",
    )
    monkeypatch.setattr(mutate, "MUTATIONS", [*mutate.MUTATIONS, fake_mutation])

    fake_cache = _make_pycache(_FAKE_ROOT_DIR)
    try:
        assert fake_cache.exists()
        mutate.clear_bytecode()
        assert not fake_cache.exists(), (
            "clear_bytecode did not clear a dir a live Mutation targets -- "
            "it is not deriving its root dirs from MUTATIONS"
        )
    finally:
        shutil.rmtree(ROOT / _FAKE_ROOT_DIR, ignore_errors=True)


def test_clear_bytecode_still_clears_every_real_mutation_dir(mutate):
    # Regression: the real (non-fixture) dirs every shipped Mutation targets
    # today must still be cleared -- the derivation must not narrow coverage
    # while fixing it, only stop hardcoding it.
    real_dirs = sorted({m.path.split("/")[0] for m in mutate.MUTATIONS})
    assert real_dirs, "MUTATIONS is empty -- nothing for this test to prove"

    caches = [_make_pycache(d) for d in real_dirs]
    try:
        for cache in caches:
            assert cache.exists()
        mutate.clear_bytecode()
        for cache in caches:
            assert not cache.exists()
    finally:
        for d in real_dirs:
            shutil.rmtree(ROOT / d / "sub", ignore_errors=True)


def test_clear_bytecode_root_dirs_match_derivation_expression(mutate):
    # Pins the exact derivation (sorted set of MUTATIONS' top-level path
    # components) against the source, so a future edit to the expression
    # itself is a deliberate, visible change rather than a silent one.
    import ast
    import inspect

    source = inspect.getsource(mutate.clear_bytecode)
    tree = ast.parse(source)
    assigns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "root_dirs" for t in node.targets)
    ]
    assert assigns, "clear_bytecode no longer assigns root_dirs from MUTATIONS"
    # The RHS must be built from `MUTATIONS` (a Name reference), not a
    # literal tuple/list of hardcoded strings.
    rhs_names = {
        node.id for node in ast.walk(assigns[0].value) if isinstance(node, ast.Name)
    }
    assert "MUTATIONS" in rhs_names, (
        "root_dirs is not derived from MUTATIONS -- looks like a reverted "
        "hardcoded literal"
    )
