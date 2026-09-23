"""A repo card must never render as a name floating on an empty slide.

The compose prompt already asked for a one-line `sub` on every repo slide.
The model sometimes did not write one, and the result was a 1080x1350 card
holding an owner, a star count and a URL with two thirds of it blank. The
research note for that same repo carries a written description, so the fix is
to stop asking and start guaranteeing.
"""

from pipeline.stages.compose import _restore_repo_facts, normalise_slides

NOTE = {
    "name": "HElib",
    "owner": "homenc",
    "url": "https://github.com/homenc/HElib",
    "stars": 3100,
    "language": "C++",
    "detail": "An open-source library implementing homomorphic encryption, "
              "with an emphasis on effective use of the Smart-Vercauteren "
              "ciphertext packing techniques.",
}


def test_a_repo_slide_with_no_sub_gets_the_researched_description():
    slides = _restore_repo_facts([{"type": "repo", "name": "HElib"}], [NOTE])
    assert slides[0]["sub"].startswith("An open-source library")


def test_the_models_own_summary_is_preferred_over_the_note():
    """The fallback is a floor, not an override — a written summary is better
    targeted than a scraped description."""
    slides = _restore_repo_facts(
        [{"type": "repo", "name": "HElib", "sub": "Homomorphic encryption in C++."}],
        [NOTE])
    assert slides[0]["sub"] == "Homomorphic encryption in C++."


def test_a_blank_sub_counts_as_missing():
    slides = _restore_repo_facts([{"type": "repo", "name": "HElib", "sub": "   "}], [NOTE])
    assert slides[0]["sub"].startswith("An open-source library")


def test_the_description_is_not_truncated():
    """Capping it would cut mid-sentence; the renderer shrinks instead."""
    slides = _restore_repo_facts([{"type": "repo", "name": "HElib"}], [NOTE])
    assert slides[0]["sub"] == NOTE["detail"]
    assert "…" not in slides[0]["sub"] and "..." not in slides[0]["sub"]


def test_a_note_without_a_detail_leaves_the_slide_alone():
    note = {k: v for k, v in NOTE.items() if k != "detail"}
    slides = _restore_repo_facts([{"type": "repo", "name": "HElib"}], [note])
    assert "sub" not in slides[0] or not slides[0]["sub"]


def test_non_repo_slides_are_untouched():
    slides = _restore_repo_facts([{"type": "point", "name": "HElib"}], [NOTE])
    assert "sub" not in slides[0]


def test_bullets_survive_normalisation_on_a_repo_slide():
    """The template renders up to four; normalise must not drop them."""
    out = normalise_slides([{
        "type": "repo", "headline": "HElib", "name": "HElib",
        "sub": "Homomorphic encryption.",
        "bullets": ["Packs ciphertexts", "BGV and CKKS", "IBM-backed", "C++ API", "fifth"],
    }])
    assert out[0]["bullets"] == ["Packs ciphertexts", "BGV and CKKS", "IBM-backed", "C++ API"]


# ------------------------------------------------ T111: absent facts, not invented
#
# `_restore_repo_facts` used `if note.get(field): slide[field] = note[field]` --
# truthy, not presence-aware. That can only ever SKIP a field, never STRIP one,
# so a model-fabricated value survived untouched whenever the note genuinely
# lacked the fact (a repo note can come back with no detected language, or a
# candidate with no popularity signal at all). And a real `stars: 0` -- a
# brand-new repo -- is falsy too, so it could never overwrite a fabricated
# non-zero either. Live-reproduced 2/2 against the real hosted model: once for
# language, once for stars.


def test_restore_repo_facts_strips_a_field_the_note_never_carried():
    """T111 AC1: the note has no "language" key at all -- the shape a
    non-repo catalogue note (or a hand-built fixture) has. A model-fabricated
    language must not survive."""
    note = {"name": "HElib", "owner": "homenc",
            "url": "https://github.com/homenc/HElib", "detail": "d"}
    slides = _restore_repo_facts(
        [{"type": "repo", "name": "HElib", "language": "Cobol"}], [note])
    assert "language" not in slides[0]


def test_restore_repo_facts_strips_a_field_the_note_carries_as_none_or_empty():
    """The production shape (enumerate_items.py's `_note`) always sets the
    stars/language keys on a repo note, but the VALUE is the "nothing here"
    sentinel when nothing was found: `None` for stars (no popularity signal),
    `""` for language (no `_LANG` match). Same bug, the other note shape --
    both must still strip the model's invention."""
    note = {"name": "HElib", "owner": "homenc",
            "url": "https://github.com/homenc/HElib",
            "stars": None, "language": "", "detail": "d"}
    slides = _restore_repo_facts(
        [{"type": "repo", "name": "HElib", "stars": 4200, "language": "Cobol"}],
        [note])
    assert "stars" not in slides[0]
    assert "language" not in slides[0]


def test_restore_repo_facts_overwrites_a_fabricated_star_count_with_a_genuine_zero():
    """T111 AC2 -- the truthiness bug itself. `if note.get(field):` treats a
    real `stars: 0` the same as "nothing to write", so a model-fabricated
    `stars: 4200` used to survive a note that in fact said zero. This must go
    red against the old `if note.get(field):` check and green after."""
    note = {"name": "HElib", "owner": "homenc",
            "url": "https://github.com/homenc/HElib", "stars": 0,
            "language": "C++", "detail": "d"}
    slides = _restore_repo_facts(
        [{"type": "repo", "name": "HElib", "stars": 4200}], [note])
    assert slides[0]["stars"] == 0


def test_restore_repo_facts_still_writes_every_real_fact():
    """Guard against an overcorrection: presence-aware must not become
    "strip everything" -- every fact the note actually has still lands on
    the slide, fabricated or not."""
    slides = _restore_repo_facts([{"type": "repo", "name": "HElib"}], [NOTE])
    assert slides[0]["owner"] == "homenc"
    assert slides[0]["url"] == "https://github.com/homenc/HElib"
    assert slides[0]["stars"] == 3100
    assert slides[0]["language"] == "C++"
