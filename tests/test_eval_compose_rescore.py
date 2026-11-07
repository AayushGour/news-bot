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


def test_rescore_prints_material_fields_and_keeps_them_out_of_the_composite(
    tmp_path, monkeypatch, eval_compose, capsys
):
    """T249: content_slides / material_units / unsupported_rich are reported, not weighted."""
    item = dict(ITEM_10, research=[{"claim": "Revenue grew 40% last year"},
                                   {"claim": "Fail", "fail_open": True}],
                raw_text="Revenue grew 40% last year.")
    _write_golden(tmp_path, 1, [item])
    deck = {"slides": [
        {"type": "hook", "headline": "Revenue up"},
        {"type": "kpi", "headline": "Growth", "tiles": [{"value": "40%"}]},
        {"type": "quote", "headline": "Said", "quote": "totally invented sentence here"},
        {"type": "sources", "headline": "Sources"},
    ]}
    _write_run(tmp_path, "mat", 1, [{"id": 10, "doc": deck}])
    monkeypatch.setattr(eval_compose, "RESULTS", tmp_path)
    monkeypatch.setattr(eval_compose, "GOLDEN", tmp_path / "golden.json")

    eval_compose.rescore("mat")
    out = capsys.readouterr().out
    assert "content_slides=2" in out
    assert "material_units=1" in out
    assert "unsupported_rich=1" in out
    assert re.search(r"avg_content_slides\s+2", out)
    assert re.search(r"total_unsupported_rich\s+1", out)
    from pipeline import quality
    for key in ("content_slides", "material_units", "unsupported_rich"):
        assert key not in quality.COMPOSITE_WEIGHTS


def test_live_path_summary_carries_material_fields(eval_compose):
    rows = [{"slides": 5, "distinct_types": 3, "rich_slides": 1, "empty_slides": 0,
             "overlong_headlines": 0, "shape_score": 3, "bad_repo_urls": 0,
             "headline_count_mismatch": 0, "hashtags": 1, "theme": "t",
             "content_slides": 3, "material_units": 4, "unsupported_rich": 2}]
    s = eval_compose.summarise(rows)
    assert s["avg_content_slides"] == 3 and s["avg_material_units"] == 4
    assert s["total_unsupported_rich"] == 2


def test_material_fields_unsupported_rich_counts_each_unbacked_slide(eval_compose):
    """T249 tester: 2 unbacked rich slides + 1 backed -> exactly 2."""
    from types import SimpleNamespace
    item = SimpleNamespace(research=[], raw_text="Revenue grew 40% last year.",
                           brief="", source="channel")
    deck = {"slides": [
        {"type": "kpi", "headline": "A", "tiles": [{"value": "40%"}]},          # backed
        {"type": "kpi", "headline": "B", "tiles": [{"value": "77%"}]},          # unbacked
        {"type": "quote", "headline": "C", "quote": "totally invented sentence here"},  # unbacked
    ]}
    assert eval_compose.material_fields(item, deck)["unsupported_rich"] == 2
