"""`--rescore` must refuse to pair a run against the wrong golden set.

A saved run's row ids are meaningful only relative to whatever it was scored
against: a `--golden` run's ids are golden-set ids (stable, versioned); a
live-db run's ids are database ids. Golden-set ids and database ids overlap
heavily (see the T68 review), so pairing a live-db run's rows against the
golden set by id alone silently scores each row against a different item's
brief and prints a plausible-looking number for it -- an off-by-one pairing
measured grounding at 0.0009 against a correct 0.0992 on the same data, and
nothing in the old output told the two apart. `rescore()` must refuse instead
of guessing, using `summary.set_version` as the only source of truth.

No LLM calls, no network calls anywhere in this file -- `rescore()` has none.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def eval_compose():
    spec = importlib.util.spec_from_file_location(
        "eval_compose_under_test", ROOT / "scripts" / "eval_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_golden(results_dir: Path, version, items: list[dict]) -> None:
    cases = [
        {"failure_mode": "none", "why": "", "item": item}
        for item in items
    ]
    (results_dir / "golden.json").write_text(
        json.dumps({"set_version": version, "cases": cases})
    )


def _write_run(results_dir: Path, tag: str, set_version, rows: list[dict]) -> None:
    (results_dir / f"{tag}.json").write_text(
        json.dumps({"summary": {"set_version": set_version}, "rows": rows})
    )


ITEM_10 = {"id": 10, "source": "channel", "brief": "Item ten brief."}
ITEM_20 = {"id": 20, "source": "channel", "brief": "Item twenty brief."}


def test_rescore_refuses_a_run_with_no_set_version(tmp_path, monkeypatch, eval_compose):
    # A live-db run (set_version None) must never be paired against the
    # golden set by id -- even though these row ids happen to also exist in
    # the golden set here, refusal must fire before any pairing is attempted.
    _write_golden(tmp_path, 1, [ITEM_10, ITEM_20])
    _write_run(tmp_path, "livedb", None, [
        {"id": 10, "doc": {"slides": [{"type": "hook", "headline": "whatever happened"}]}},
    ])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    with pytest.raises(SystemExit, match="live db selection"):
        eval_compose.rescore("livedb")


def test_rescore_refuses_a_golden_set_version_mismatch(tmp_path, monkeypatch, eval_compose):
    _write_golden(tmp_path, 2, [ITEM_10, ITEM_20])
    _write_run(tmp_path, "stale", 1, [
        {"id": 10, "doc": {"slides": [{"type": "hook", "headline": "whatever happened"}]}},
    ])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    with pytest.raises(SystemExit, match=r"v1.*v2|v2.*v1"):
        eval_compose.rescore("stale")


def test_rescore_missing_tag_file_gives_a_friendly_error_not_a_traceback(
    tmp_path, monkeypatch, eval_compose
):
    _write_golden(tmp_path, 1, [ITEM_10])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    # "golden_current" typo'd as "golden" used to traceback on a bare
    # path.read_text() FileNotFoundError; must now fail cleanly instead.
    with pytest.raises(SystemExit, match="does not exist"):
        eval_compose.rescore("golden_current")


def test_rescore_missing_rows_key_gives_a_friendly_error(tmp_path, monkeypatch, eval_compose):
    _write_golden(tmp_path, 1, [ITEM_10])
    (tmp_path / "notarun.json").write_text(json.dumps({"not": "a run"}))
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    with pytest.raises(SystemExit, match="no \"rows\" key"):
        eval_compose.rescore("notarun")


def test_rescore_scores_a_matching_version_run_and_prints_the_runs_own_version(
    tmp_path, monkeypatch, eval_compose, capsys
):
    _write_golden(tmp_path, 1, [ITEM_10, ITEM_20])
    _write_run(tmp_path, "matching", 1, [
        {"id": 10, "doc": {"slides": [{"type": "hook", "headline": "Item ten brief."}]}},
        {"id": 20, "raw": {"slides": [{"type": "hook", "headline": "fallback completion"}]}},
    ])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    eval_compose.rescore("matching")
    out = capsys.readouterr().out
    assert "golden set v1" in out
    assert "2/2 rows scored" in out


def test_rescore_reports_doc_and_raw_rows_separately(tmp_path, monkeypatch, eval_compose, capsys):
    # golden_current mixed 19 validated `doc` decks with 5 unvalidated `raw`
    # completions; averaging them into one number blurs "how many of each
    # landed" into what reads like a quality signal. The two must be
    # reported apart.
    _write_golden(tmp_path, 1, [ITEM_10, ITEM_20])
    _write_run(tmp_path, "mixed", 1, [
        {"id": 10, "doc": {"slides": [{"type": "hook", "headline": "a validated deck"}]}},
        {"id": 20, "raw": {"slides": [{"type": "hook", "headline": "an unvalidated completion"}]}},
    ])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    eval_compose.rescore("mixed")
    out = capsys.readouterr().out
    doc_section = out.split("doc only")[1].split("raw only")[0]
    raw_section = out.split("raw only")[1]
    assert re.search(r"decks_scored\s+1\b", doc_section)
    assert re.search(r"decks_scored\s+1\b", raw_section)


def test_deck_from_row_prefers_doc_over_raw_and_reports_the_source(eval_compose):
    doc_row = {"doc": {"slides": [{"type": "hook", "headline": "h"}]}, "raw": {"slides": []}}
    deck, source = eval_compose._deck_from_row(doc_row)
    assert source == "doc"
    assert deck is doc_row["doc"]

    raw_only_row = {"raw": {"slides": [{"type": "hook", "headline": "h"}]}}
    deck, source = eval_compose._deck_from_row(raw_only_row)
    assert source == "raw"
    assert deck is raw_only_row["raw"]

    empty_row = {"error": "BadCompletion: no content"}
    deck, source = eval_compose._deck_from_row(empty_row)
    assert deck is None and source is None
