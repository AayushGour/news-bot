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
