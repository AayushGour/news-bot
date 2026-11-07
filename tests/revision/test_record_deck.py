"""T302: a run records the deck it left behind, so a later preview of a DIFFERENT deck
(a legacy regen, a requeue) never presents it as that deck's changes."""

from __future__ import annotations

from pipeline.revision.record import append_run, deck_fingerprint, latest_run, new_run

REQ = {"origin": "operator", "surface": "telegram", "note": "x"}
A = [{"type": "hook", "headline": "A"}, {"type": "follow"}]
B = [{"type": "hook", "headline": "B"}, {"type": "follow"}]


def rev(*decks):
    out = None
    for d in decks:
        out = append_run(out, new_run(REQ, at="t", deck=d))
    return out


def test_fingerprint_is_stable_and_sensitive():
    assert deck_fingerprint(A) == deck_fingerprint([dict(s) for s in A])
    assert deck_fingerprint(A) != deck_fingerprint(B)
    assert deck_fingerprint(A) != deck_fingerprint(A[:1])
    assert deck_fingerprint(None) == deck_fingerprint([])


def test_new_run_carries_the_deck_key():
    assert new_run(REQ, at="t")["deck"] is None
    assert new_run(REQ, at="t", deck="abc")["deck"] == "abc"


def test_latest_run_is_the_last_one_for_the_current_deck():
    r = rev(deck_fingerprint(B), deck_fingerprint(A))
    assert latest_run(r, A) is r["runs"][-1]
    assert latest_run(r, B) is None, "the run before the last is never 'latest'; the last is for A"


def test_a_run_for_another_deck_is_stale_unless_asked_otherwise():
    r = rev(deck_fingerprint(B))
    assert latest_run(r, A) is None
    assert latest_run(r, A, current_deck=False) is r["runs"][-1]


def test_a_run_with_no_recorded_deck_is_trusted():
    r = rev(None)
    assert latest_run(r, A) is r["runs"][-1]


def test_unreadable_revision_values_are_none():
    for junk in (None, {}, [], "x", {"runs": "x"}, {"runs": []}, {"runs": ["s"]}, {"v": 1, "runs": [None]}):
        assert latest_run(junk, A) is None
