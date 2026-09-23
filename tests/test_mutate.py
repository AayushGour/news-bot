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


# --- crash-recovery safety net: _dirty_tree, _refuse_reason, check, main ---
# (B4, bounce 1, reviewer): code-brain risk 0.60, blast radius 0 files --
# nothing imports mutate.py, so a direct test here is the ONLY possible
# guard on this logic. No subprocess/filesystem call against the real repo
# state: subprocess.run and INFLIGHT are both faked/redirected per test so
# none of this depends on -- or can dirty -- this worktree's actual git
# status.

class _FakeCompleted:
    """Stands in for subprocess.CompletedProcess -- only `.stdout` is read."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_dirty_tree_is_none_on_a_clean_tree(mutate, monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *a, **k: _FakeCompleted("")
    )
    assert mutate._dirty_tree() is None


def test_dirty_tree_returns_the_porcelain_output_when_dirty(mutate, monkeypatch):
    porcelain = " M src/pipeline/stages/compose.py\n"
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *a, **k: _FakeCompleted(porcelain)
    )
    assert mutate._dirty_tree() == porcelain


def test_refuse_reason_is_none_when_clean_and_no_lock(mutate, monkeypatch, tmp_path):
    monkeypatch.setattr(mutate, "INFLIGHT", tmp_path / ".mutate-inflight")
    monkeypatch.setattr(mutate, "_dirty_tree", lambda: None)
    assert mutate._refuse_reason() is None


def test_refuse_reason_names_dirty_tree_when_only_the_tree_is_dirty(
    mutate, monkeypatch, tmp_path
):
    monkeypatch.setattr(mutate, "INFLIGHT", tmp_path / ".mutate-inflight")
    monkeypatch.setattr(mutate, "_dirty_tree", lambda: " M some/file.py\n")
    reason = mutate._refuse_reason()
    assert reason is not None
    assert "dirty" in reason


def test_refuse_reason_names_the_recovery_record_when_only_the_lock_exists(
    mutate, monkeypatch, tmp_path
):
    inflight = tmp_path / ".mutate-inflight"
    inflight.write_text('{"path": "x", "original": "y"}')
    monkeypatch.setattr(mutate, "INFLIGHT", inflight)
    monkeypatch.setattr(mutate, "_dirty_tree", lambda: None)
    reason = mutate._refuse_reason()
    assert reason is not None
    assert "already exists" in reason
    assert "restore" in reason


def test_refuse_reason_simulated_crash_prints_the_recovery_message(
    mutate, monkeypatch, tmp_path
):
    """B2 (bounce 1, reviewer): a crash leaves BOTH signals true at once --
    the lock file AND a dirty tree (the mutation it describes, still
    applied). Before this fix, `_refuse_reason` checked dirty-tree first, so
    exactly the case the recovery message exists for produced the OTHER
    message instead -- the one that never mentions the record. This is that
    simulated crash: both conditions are made true together, and the
    recovery message (not the plain dirty-tree message) must be what a human
    sees.

    Would fail red if `_refuse_reason`'s dirty-tree check were moved back
    ahead of the `INFLIGHT.exists()` check (the exact bug this bounce
    fixes) -- proven below by literally doing that and re-running.
    """
    inflight = tmp_path / ".mutate-inflight"
    inflight.write_text('{"path": "src/x.py", "original": "..."}')
    monkeypatch.setattr(mutate, "INFLIGHT", inflight)
    monkeypatch.setattr(mutate, "_dirty_tree", lambda: " M src/x.py\n")

    reason = mutate._refuse_reason()

    assert reason is not None
    assert "already exists" in reason, (
        "a simulated crash (lock + dirty tree together) did not surface the "
        "recovery message -- the dirty-tree check is shadowing it again"
    )
    assert "restore" in reason and "Read the file" in reason


def test_check_refuses_to_run_when_a_lock_already_exists(mutate, monkeypatch, tmp_path):
    """The second safety net (D92/D98): even if something bypasses main()'s
    `_refuse_reason` gate and calls `check()` directly while a lock is
    already on disk, the exclusive `open(INFLIGHT, "x")` must still refuse
    -- and must refuse BEFORE the target file is touched."""
    fixture_dir = tmp_path / "fixture_src"
    fixture_dir.mkdir()
    target = fixture_dir / "sample.py"
    target.write_text("old_anchor_text\n")

    inflight = tmp_path / ".mutate-inflight"
    inflight.write_text('{"path": "already-running", "original": "..."}')
    monkeypatch.setattr(mutate, "INFLIGHT", inflight)
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    # T151: check() now checks the baseline before claiming the lock. That
    # baseline check doesn't touch `target` or the lock at all -- it is a
    # read-only pytest run against `mutation.tests` -- so faking it green
    # here keeps this test isolated and fast without changing what it
    # proves (the lock still refuses before the target file is touched).
    monkeypatch.setattr(mutate, "_baseline", lambda paths: (True, "passed"))

    fake_mutation = mutate.Mutation(
        "fixture-lock", "fixture_src/sample.py", "old_anchor_text", "new_anchor_text",
        "tests/test_mutate.py", "proves check() refuses when INFLIGHT already exists",
    )

    with pytest.raises(SystemExit, match="already exists"):
        mutate.check(fake_mutation)

    assert target.read_text() == "old_anchor_text\n", (
        "check() touched the target file before honouring the existing lock"
    )


def test_main_refuses_to_start_and_never_runs_a_mutation(mutate, monkeypatch, capsys):
    monkeypatch.setattr(mutate, "_refuse_reason", lambda: "simulated refusal reason")

    def _must_not_run(*_a, **_k):
        raise AssertionError("main() ran a mutation despite a refusal reason")

    monkeypatch.setattr(mutate, "check", _must_not_run)

    code = mutate.main(None, False)

    assert code == mutate.EXIT_REFUSED_TO_START
    assert code != mutate.EXIT_BAD_ARGS, (
        "a start-time safety refusal must not share an exit code with a bad "
        "--only argument (nit, bounce 1, reviewer)"
    )
    out = capsys.readouterr().out
    assert "refusing to start" in out
    assert "simulated refusal reason" in out


# --- T151: run_tests()/_baseline()/check() must tell "caught" apart from
# "inconclusive", not collapse pytest's whole exit-code space into a bool.
# `run_tests` classification is unit-tested against a faked subprocess
# result (same `_FakeCompleted` stand-in used above); `check()`'s baseline
# short-circuit (AC1/AC2) is proven against REAL pytest subprocesses --
# those two are the exact false "caught" a reviewer reproduced and the
# exact T140 family, so a mock would only prove the mock, not the fix.

def test_run_tests_classifies_exit_0_as_passed(mutate, monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0})(),
    )
    assert mutate.run_tests("tests/x.py") == (True, "passed")


def test_run_tests_classifies_exit_1_as_failed(mutate, monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1})(),
    )
    assert mutate.run_tests("tests/x.py") == (False, "failed")


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_run_tests_classifies_2_through_5_as_inconclusive_not_caught(
    mutate, monkeypatch, code
):
    """The bug itself: `result.returncode == 0` used to make every one of
    these read as "not 0" -> caught in `check()`'s old `not run_tests(...)`.
    None of them are a verdict on the mutated code -- pytest never finished
    evaluating it."""
    monkeypatch.setattr(
        mutate.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": code})(),
    )
    passed, detail = mutate.run_tests("tests/x.py")
    assert passed is None, (
        f"pytest exit {code} must be inconclusive (None), not a bool -- "
        "treating it as caught is T151"
    )
    assert detail  # a human-readable reason, never blank


def test_run_tests_classifies_an_unknown_exit_code_as_inconclusive(mutate, monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 99})(),
    )
    passed, detail = mutate.run_tests("tests/x.py")
    assert passed is None
    assert "99" in detail


def test_run_tests_reports_inconclusive_when_pytest_cannot_be_launched(
    mutate, monkeypatch
):
    """AC1: the exact false 'caught' a reviewer reproduced by removing
    `.venv`. Point PYTHON at a path that does not exist -- the same
    failure shape as a missing `.venv/bin/python` -- and prove `run_tests`
    comes back `(None, ...)`, never `(True, ...)` and never a silent
    `(False, "failed")` either; either of those, inverted by the old
    `not run_tests(...)`, is exactly how a broken environment used to read
    as caught."""
    monkeypatch.setattr(mutate, "PYTHON", Path("/nonexistent/does-not-exist/python"))
    passed, detail = mutate.run_tests("tests/test_mutate.py")
    assert passed is None, (
        f"a missing pytest interpreter must be inconclusive (None), got {passed!r}"
    )
    assert "could not be launched" in detail


def test_baseline_is_memoized_per_unique_test_path(mutate, monkeypatch):
    calls = []

    def _fake_run_tests(paths):
        calls.append(paths)
        return True, "passed"

    monkeypatch.setattr(mutate, "run_tests", _fake_run_tests)
    monkeypatch.setattr(mutate, "_BASELINE_CACHE", {})

    assert mutate._baseline("tests/a.py") == (True, "passed")
    assert mutate._baseline("tests/a.py") == (True, "passed")
    assert mutate._baseline("tests/b.py") == (True, "passed")

    assert calls == ["tests/a.py", "tests/b.py"], (
        "a repeated tests path must hit the cache, not re-run pytest -- "
        "T151 AC: a baseline run per sweep, not per mutation"
    )


def test_main_clears_the_baseline_cache_before_each_sweep(mutate, monkeypatch):
    monkeypatch.setattr(mutate, "_refuse_reason", lambda: None)
    monkeypatch.setattr(mutate, "check", lambda m: ("caught", "caught"))
    mutate._BASELINE_CACHE["stale"] = (False, "leftover from a previous main() call")

    mutate.main(None, False)

    assert "stale" not in mutate._BASELINE_CACHE, (
        "a cache left over from a previous main() invocation could answer "
        "a baseline question with a stale verdict"
    )


def test_check_reports_inconclusive_when_the_baseline_is_already_red(
    mutate, monkeypatch, tmp_path
):
    """AC2 (T140's family): a mutation whose target tests are ALREADY red
    before any mutation runs must report "inconclusive", never "caught" --
    a red baseline makes every mutation touching it look caught for free,
    exactly what voided 16 of 68 verdicts in T140. Deliberately red test,
    real pytest, `_baseline`/`run_tests` left unmocked: this proves the
    actual mechanism end to end, not a stand-in for it.
    """
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "INFLIGHT", tmp_path / ".mutate-inflight")
    monkeypatch.setattr(mutate, "_BASELINE_CACHE", {})

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture_always_red.py").write_text(
        "def test_always_red():\n    assert False\n"
    )

    target_dir = tmp_path / "fixture_src"
    target_dir.mkdir()
    target = target_dir / "sample.py"
    target.write_text("old_anchor_text\n")

    fake_mutation = mutate.Mutation(
        "fixture-red-baseline", "fixture_src/sample.py",
        "old_anchor_text", "new_anchor_text",
        "tests/test_fixture_always_red.py",
        "proves a pre-existing red test voids the verdict, not passes it",
    )

    verdict, detail = mutate.check(fake_mutation)

    assert verdict == "inconclusive", (
        f"a pre-existing red baseline must report inconclusive, not "
        f"{verdict!r} -- this is the exact T140 false-caught failure"
    )
    assert "BASELINE NOT GREEN" in detail
    # And the mutation must never even have been applied once the baseline
    # is known to be red -- check() short-circuits before touching disk.
    assert target.read_text() == "old_anchor_text\n"


def test_check_reports_caught_for_a_genuine_regression(mutate, monkeypatch, tmp_path):
    """AC3: the three-way split must not disturb a genuine catch. Real
    pytest, a real green baseline, a real broken mutation.

    Deliberately uses `src/` (not an invented dir name) as the fixture's
    top-level dir: `clear_bytecode()` derives which dirs it clears from the
    real, global `MUTATIONS`, not from this fake one, so a made-up dir name
    would never get its `__pycache__` cleared between the baseline run and
    the mutated run -- caught this exact way once (an unrelated invented
    name let a stale bytecode cache from the baseline pass survive into the
    mutated pytest subprocess and reported survived instead of caught,
    a T127-shaped mutant-not-running failure one level up, in the test
    fixture rather than the tool). `src` and `tests` are both real
    `MUTATIONS` top-level dirs, so `clear_bytecode()` covers them exactly
    as it would in production.
    """
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "INFLIGHT", tmp_path / ".mutate-inflight")
    monkeypatch.setattr(mutate, "_BASELINE_CACHE", {})

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    target = src_dir / "sample.py"
    target.write_text("def add(a, b):\n    return a + b\n")

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture_caught.py").write_text(
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from sample import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )

    fake_mutation = mutate.Mutation(
        "fixture-genuine-catch", "src/sample.py",
        "return a + b", "return a - b",
        "tests/test_fixture_caught.py",
        "proves a real regression still reports caught",
    )

    verdict, detail = mutate.check(fake_mutation)

    assert verdict == "caught"
    assert detail == "caught"
    assert target.read_text() == "def add(a, b):\n    return a + b\n", (
        "check() must revert the mutation regardless of verdict"
    )


def test_check_reports_survived_when_nothing_covers_the_mutation(
    mutate, monkeypatch, tmp_path
):
    """AC3, the other half: an uncovered change must still report
    survived, not caught and not inconclusive. Uses `src/`, not an invented
    dir name -- see the docstring on the caught-case test above for why."""
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "INFLIGHT", tmp_path / ".mutate-inflight")
    monkeypatch.setattr(mutate, "_BASELINE_CACHE", {})

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    target = src_dir / "sample.py"
    target.write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def unused(a, b):\n    return a + b\n"
    )

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture_survivor.py").write_text(
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from sample import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )

    fake_mutation = mutate.Mutation(
        "fixture-genuine-survivor", "src/sample.py",
        "def unused(a, b):\n    return a + b",
        "def unused(a, b):\n    return a - b",
        "tests/test_fixture_survivor.py",
        "proves an uncovered change still reports survived, not caught",
    )

    verdict, detail = mutate.check(fake_mutation)

    assert verdict == "survived"
    assert "SURVIVED" in detail
    assert target.read_text() == (
        "def add(a, b):\n    return a + b\n\n"
        "def unused(a, b):\n    return a + b\n"
    )


def test_main_returns_exit_inconclusive_and_it_outranks_survived(
    mutate, monkeypatch, capsys
):
    """AC4: the three outcomes are distinct in the summary and the exit
    code distinguishes them. A sweep with BOTH a survived mutation and an
    inconclusive one must exit EXIT_INCONCLUSIVE, not EXIT_SURVIVED -- an
    inconclusive verdict means the survived count itself is not fully
    trustworthy either."""
    monkeypatch.setattr(mutate, "_refuse_reason", lambda: None)

    verdicts = {
        "m-caught": ("caught", "caught"),
        "m-survived": ("survived", "SURVIVED — nothing tests this"),
        "m-inconclusive": ("inconclusive", "BASELINE NOT GREEN (...)"),
    }
    fakes = [
        mutate.Mutation(name, "x.py", "a", "b", "tests/x.py", "why")
        for name in verdicts
    ]
    monkeypatch.setattr(mutate, "MUTATIONS", fakes)
    monkeypatch.setattr(mutate, "check", lambda m: verdicts[m.name])

    code = mutate.main(None, False)

    assert code == mutate.EXIT_INCONCLUSIVE
    assert code not in (
        0, mutate.EXIT_SURVIVED, mutate.EXIT_BAD_ARGS, mutate.EXIT_REFUSED_TO_START,
    ), "EXIT_INCONCLUSIVE must not collide with any existing exit code (D98's contract)"
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "survived" in out


def test_main_still_returns_exit_survived_when_nothing_is_inconclusive(
    mutate, monkeypatch
):
    """Regression: a plain survivor with no inconclusive mutations in the
    sweep must keep returning EXIT_SURVIVED exactly as before T151."""
    monkeypatch.setattr(mutate, "_refuse_reason", lambda: None)
    fake = mutate.Mutation("m", "x.py", "a", "b", "tests/x.py", "why")
    monkeypatch.setattr(mutate, "MUTATIONS", [fake])
    monkeypatch.setattr(
        mutate, "check", lambda m: ("survived", "SURVIVED — nothing tests this")
    )

    assert mutate.main(None, False) == mutate.EXIT_SURVIVED


def test_main_returns_zero_when_every_mutation_is_caught(mutate, monkeypatch):
    """AC5 at the unit level (the real 71-mutation sweep is run and pasted
    separately): a clean sweep is still exit 0."""
    monkeypatch.setattr(mutate, "_refuse_reason", lambda: None)
    fake = mutate.Mutation("m", "x.py", "a", "b", "tests/x.py", "why")
    monkeypatch.setattr(mutate, "MUTATIONS", [fake])
    monkeypatch.setattr(mutate, "check", lambda m: ("caught", "caught"))

    assert mutate.main(None, False) == 0
