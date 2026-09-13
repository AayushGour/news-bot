"""Requeue clears what the next stage regenerates — no more, no less.

The table is easy to get subtly wrong: a status names finished work, but the
worker keys the *next* stage off it. Clearing the status's own output instead
of the next stage's runs that stage on wiped inputs, which surfaces later as a
model fault rather than a requeue fault.
"""

import pytest

from pipeline.models import Status
from pipeline.requeue import STAGE_OUTPUTS, TARGETS, fields_to_clear


def test_requeue_to_ingested_clears_vision_output_because_extract_reruns():
    assert fields_to_clear(Status.INGESTED)["extracted"] == {}


def test_requeue_to_extracted_keeps_vision_output():
    """Triage runs from EXTRACTED and reads the descriptions — wiping them
    would triage an image-only post blind, which is the exact failure the
    stage map orders extraction before triage to avoid."""
    cleared = fields_to_clear(Status.EXTRACTED)
    assert "extracted" not in cleared
    assert cleared["triage_score"] is None


def test_requeue_to_triaged_clears_research_but_keeps_triage_verdict():
    cleared = fields_to_clear(Status.TRIAGED)
    assert cleared["research"] == []
    assert "triage_score" not in cleared


def test_requeue_to_synthesized_keeps_the_brief_it_will_compose_from():
    cleared = fields_to_clear(Status.SYNTHESIZED)
    assert "brief" not in cleared
    assert cleared["slides"] == []


def test_later_stages_are_always_cleared_too():
    """A requeue must not leave rendered images from a deck that no longer
    exists — the preview would show the old carousel."""
    cleared = fields_to_clear(Status.TRIAGED)
    for key in ("brief", "slides", "rendered_paths", "media_urls"):
        assert key in cleared


def test_every_requeue_resets_the_attempt_counter():
    """A requeue is a fresh attempt; inheriting a spent budget would fail the
    item almost immediately."""
    cleared = fields_to_clear(Status.COMPOSED)
    assert cleared["attempts"] == 0
    assert cleared["last_error"] is None


def test_stage_order_matches_the_pipeline():
    assert [s.value for s, _ in STAGE_OUTPUTS] == [
        "ingested", "extracted", "triaged", "researched",
        "synthesized", "composed", "rendered", "approved", "publishing",
    ]
    assert set(TARGETS) == {s.value for s, _ in STAGE_OUTPUTS}


def test_a_terminal_status_is_not_a_requeue_target():
    with pytest.raises(ValueError):
        fields_to_clear(Status.PUBLISHED)


def test_requeue_clears_instagram_containers():
    """A container has its caption and image URLs baked in at creation.
    Reusing one after a recompose publishes the previous deck."""
    cleared = fields_to_clear(Status.SYNTHESIZED)
    assert cleared["ig_carousel_id"] is None
    assert cleared["ig_child_ids"] == []
    assert cleared["ig_post_id"] is None


def test_requeuing_to_publishing_keeps_the_uploaded_urls():
    """Republishing the same slides should not re-upload them."""
    cleared = fields_to_clear(Status.PUBLISHING)
    assert "media_urls" not in cleared
    assert cleared["ig_carousel_id"] is None
