"""T85/D16 -- the model-only guard must actually run at each call site, not
merely exist as a well-tested helper.

`tests/test_config.py` covers `validate_model_config()`'s own logic (neuter
it and 12 of those tests go red) and its delegation from `validate_for_run()`
(unwire that and 9 more go red, including the superset test). Neither of
those exercises the four *scripts* that are supposed to call it directly --
`eval_compose.py`, `smoke.py`, `build_golden.py`, `dashboard.py`. The
reviewer proved that gap is real: replacing
``settings.validate_model_config()`` with ``pass`` in all four call sites
left 827/827 tests green.

This file drives each script's real entry point -- no argparse, zero
Telegram vars (the exact shape T85 exists for: these tools have no account
wired up yet) -- far enough to prove the guard actually fires there, using
the T69 idiom already in this suite: ``importlib.util.spec_from_file_location``
on the script under test (see ``test_eval_compose_rescore.py:28``).

No LLM calls, no network calls, no real DB/media writes: in the correctly
wired code ``validate_model_config()`` raises before any of the four scripts
touches a database, an HTTP client or a model -- that ordering is exactly
what this test proves.
"""

from __future__ import annotations

import asyncio
import importlib.util
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import MissingConfig

ROOT = Path(__file__).resolve().parents[1]


def _no_telegram_env(tmp_path, **over):
    """Zero Telegram vars, one auto-router slug.

    Zero Telegram vars is the point (D16): ``validate_for_run()``'s
    ``REQUIRED`` check would refuse these before the guard under test ever
    ran, so this env can only raise via ``validate_model_config()``. The
    auto-router slug is the specific, distinctively-worded failure
    (``"reproducib"`` -- see ``config.py``) that lets the assertion below
    tell "the guard fired" apart from any other exception a broken script
    might raise for an unrelated reason.
    """
    env = {
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",
        "OPENROUTER_MODEL_CHEAP": "openrouter/free",
        "DB_PATH": str(tmp_path / "wiring-check.db"),
    }
    env.update(over)
    return env


def _load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_eval_compose() -> None:
    module = _load_module("eval_compose_wiring_check", "scripts/eval_compose.py")
    asyncio.run(module.run(limit=1, tag=None, only=[1], golden=False))


def _invoke_smoke() -> None:
    # smoke.py's own `main()` parses argv itself (unlike eval_compose.py and
    # build_golden.py, which only do that under `__main__`) -- pin it so this
    # doesn't try to parse pytest's own arguments.
    module = _load_module("smoke_wiring_check", "scripts/smoke.py")
    with patch.object(sys, "argv", ["smoke.py"]):
        asyncio.run(module.main())


def _invoke_build_golden() -> None:
    module = _load_module("build_golden_wiring_check", "scripts/build_golden.py")
    asyncio.run(module.main(list_only=True))


def _invoke_dashboard() -> None:
    # dashboard.py's call lives directly under `if __name__ == "__main__":`,
    # not inside a function -- the only one of the four shaped that way --
    # so it needs `run_name="__main__"` to execute at all. argparse there
    # reads real sys.argv unless pinned, and a wired guard would still raise
    # before `web.run_app` is ever reached; both are patched only to keep an
    # UNWIRED (mutated) run from binding a socket or choking on pytest's own
    # argv, not because the wired path needs either.
    with patch.object(sys, "argv", ["dashboard.py"]), patch("aiohttp.web.run_app"):
        runpy.run_path(str(ROOT / "scripts" / "dashboard.py"), run_name="__main__")


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(_invoke_eval_compose, id="eval_compose.py"),
        pytest.param(_invoke_smoke, id="smoke.py"),
        pytest.param(_invoke_build_golden, id="build_golden.py"),
        pytest.param(_invoke_dashboard, id="dashboard.py"),
    ],
)
def test_model_only_scripts_call_the_guard_before_any_real_work(
    monkeypatch, tmp_path, invoke
):
    """Each of these four scripts must call ``validate_model_config()`` at
    its own entry point, not merely inherit the check by importing
    ``pipeline.config``.

    Proof this is load-bearing, not tautological: replace any of the four
    ``settings.validate_model_config()`` call sites with ``pass`` and this
    case goes red -- either "DID NOT RAISE" (the script ran clean past
    where the guard used to be) or a different, later exception (it pushed
    on into real DB/HTTP code with an invalid model config instead of
    refusing up front). Reproduced by hand for all four; red/green pasted
    in devops.md.
    """
    for key, value in _no_telegram_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(MissingConfig, match="reproducib"):
        invoke()
