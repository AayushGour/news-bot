"""The log page must survive whatever is sitting next to the log file.

`_log_files` globs `pipeline.log.*` to pick up RotatingFileHandler's backups.
That glob is not selective: an archived copy parked beside the log matched it
too, and parsing its suffix as an integer took the entire /logs page down with
a 500. The page is what you read when something is wrong, so it has to be the
one thing that does not break.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    """Load dashboard.py as a module with LOG_PATH pointed at a temp dir."""
    spec = importlib.util.spec_from_file_location(
        "dashboard_under_test", ROOT / "scripts" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "pipeline.log")
    return module


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_a_non_numeric_sibling_is_ignored_not_fatal(dashboard, tmp_path):
    """The exact file that caused the 500."""
    write(tmp_path / "pipeline.log", "live\n")
    write(tmp_path / "pipeline.log.archive-20260913", "archived\n")
    files = dashboard._log_files()
    assert [p.name for p in files] == ["pipeline.log"]


def test_numeric_backups_are_ordered_oldest_first(dashboard, tmp_path):
    """.1 is the most recent backup, so it must come last among the backups."""
    for name in ("pipeline.log", "pipeline.log.1", "pipeline.log.2",
                 "pipeline.log.10"):
        write(tmp_path / name, name)
    assert [p.name for p in dashboard._log_files()] == [
        "pipeline.log.10", "pipeline.log.2", "pipeline.log.1", "pipeline.log",
    ]


def test_backups_and_junk_together(dashboard, tmp_path):
    write(tmp_path / "pipeline.log", "live")
    write(tmp_path / "pipeline.log.1", "backup")
    write(tmp_path / "pipeline.log.archive-20260913", "junk")
    write(tmp_path / "pipeline.log.bak", "junk")
    assert [p.name for p in dashboard._log_files()] == [
        "pipeline.log.1", "pipeline.log"]


def test_no_log_file_at_all_is_empty_not_an_error(dashboard):
    assert dashboard._log_files() == []


def test_newest_first_reverses_line_order(dashboard):
    text = "first\nsecond\nthird"
    assert dashboard._newest_first(text) == "third\nsecond\nfirst"


def test_newest_first_on_a_single_line_and_on_empty(dashboard):
    assert dashboard._newest_first("only") == "only"
    assert dashboard._newest_first("") == ""


# --------------------------------------------------------------- timeline


class FakeItem:
    def __init__(self, status, publish_log=None, item_id=7):
        self.id = item_id
        self.status = status
        self.publish_log = publish_log or []


def states(html: str) -> list[str]:
    """The state class of each node, in pipeline order."""
    import re
    return re.findall(r"tl-node (done|now|todo|off)", html)


def test_stages_before_the_current_one_are_done_and_later_ones_are_todo(dashboard):
    html = dashboard.timeline_control(FakeItem("composed"))
    order = dashboard.PIPELINE
    here = order.index("composed")
    assert states(html) == ["done"] * here + ["now"] + ["todo"] * (len(order) - here - 1)


def test_only_requeueable_stages_are_clickable(dashboard):
    """awaiting_approval and published are shown but must not be buttons —
    requeue would raise on them."""
    from pipeline.requeue import TARGETS
    html = dashboard.timeline_control(FakeItem("rendered"))
    for stage in dashboard.PIPELINE:
        posts_to_it = f"value='{stage}'" in html
        assert posts_to_it is (stage in TARGETS), stage


def test_every_clickable_stage_posts_to_the_requeue_endpoint(dashboard):
    html = dashboard.timeline_control(FakeItem("triaged", item_id=42))
    from pipeline.requeue import TARGETS
    assert html.count("action='/item/42/requeue'") == len(TARGETS)


def test_an_off_pipeline_status_marks_no_stage_current(dashboard):
    """dropped/failed/needs_input are not points on the line; claiming one was
    'now' would misreport where the item actually is."""
    html = dashboard.timeline_control(FakeItem("failed"))
    assert set(states(html)) == {"off"}
    assert "tl-node now" not in html


def test_published_item_marks_the_whole_line_done(dashboard):
    html = dashboard.timeline_control(FakeItem("published"))
    assert states(html)[-1] == "now"
    assert "todo" not in states(html)


def test_warning_appears_only_when_the_item_has_been_published(dashboard):
    assert dashboard.published_warning(FakeItem("ingested")) == ""
    warn = dashboard.published_warning(FakeItem("ingested", [
        {"ig_post_id": "18092835962538597", "at": "2026-09-13T10:30:49+00:00"}]))
    assert "already published once" in warn
    assert "18092835962538597" in warn
    assert "another carousel" in warn


def test_warning_counts_repeat_publications(dashboard):
    warn = dashboard.published_warning(FakeItem("ingested", [
        {"ig_post_id": "a", "at": "x"}, {"ig_post_id": "b", "at": "y"}]))
    assert "already published 2 times" in warn
    assert "<code>b</code>" in warn, "must show the most recent, not the first"


def test_warning_survives_the_requeue_that_hid_it(dashboard):
    """The whole point: a requeued item still says it is on the account."""
    requeued = FakeItem("ingested", [{"ig_post_id": "18092835962538597", "at": "t"}])
    assert "already published" in dashboard.published_warning(requeued)
