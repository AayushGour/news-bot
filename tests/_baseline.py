"""The commit the agentic-Regenerate parity tests compare against.

HEAD moves with every commit, so "unchanged vs HEAD" silently stops meaning
"unchanged vs before the feature" once the feature is committed. These tests pin
the last commit before it landed instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PRE_FEATURE_COMMIT = "b1bcae7"

_ROOT = Path(__file__).resolve().parents[1]


def baseline_show(path: str) -> str:
    """``path`` as it was at ``PRE_FEATURE_COMMIT``. Skips the test (with the reason)
    when that commit is not in this clone, e.g. a shallow clone."""
    done = subprocess.run(
        ["git", "show", f"{PRE_FEATURE_COMMIT}:{path}"], cwd=_ROOT, capture_output=True, text=True
    )
    if done.returncode:
        pytest.skip(f"pre-feature commit {PRE_FEATURE_COMMIT} not available (shallow clone?): {done.stderr.strip()}")
    return done.stdout
