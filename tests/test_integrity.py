"""A deck may not present our own failure to find something as a finding.

Item 116 asked for "the psychology of narcissistic people and how they affect
kids in their childhood which turns into illness as adults". Research returned
eight general "what is NPD" articles and nothing about developmental outcomes,
which the coverage gate caught correctly. /post then built a deck anyway,
compose found a hole it had not been told about, and filled it: a hook reading
"What the research doesn't show", a takeaway reading "The gap is the finding",
and a caption asserting "That research simply hasn't been done yet."

That claim is false, and it is not a claim this pipeline is in any position to
make — it read a handful of pages, which says something about the search and
nothing about the literature.
"""

import pytest

from pipeline.integrity import absence_claims, check_deck, unsupported_absence

NPD_NOTES = [
    {"claim": "What types of narcissism are there?",
     "detail": "Subtypes of NPD may include grandiose and vulnerable NPD."},
    {"claim": "9 Common Signs of NPD",
     "detail": "Many people use the word narcissist to describe someone vain."},
]

ITEM_116_DECK = [
    {"type": "hook", "headline": "What the research doesn't show",
     "sub": "Narcissistic parenting → adult illness: the missing longitudinal data"},
    {"type": "takeaway", "headline": "The gap is the finding",
     "sub": "Sources cover NPD as a disorder, not narcissistic parenting."},
]
ITEM_116_CAPTION = ("The specific developmental pathway from narcissistic parent "
                    "to sick adult? That research simply hasn't been done yet.")


def test_the_exact_deck_that_shipped_is_rejected():
    problems = check_deck(ITEM_116_DECK, NPD_NOTES, ITEM_116_CAPTION)
    assert problems, "this is the deck that went out; it must not pass"


@pytest.mark.parametrize("text", [
    "That research simply hasn't been done yet.",
    "No longitudinal studies exist on this.",
    "The gap is the finding.",
    "What the research doesn't show",
    "The data has not been published.",
])
def test_absence_assertions_are_detected(text):
    assert absence_claims(text)


@pytest.mark.parametrize("text", [
    "Experts disagree about the mechanism.",
    "The study found no effect on adult outcomes.",
    "Researchers have not agreed on a single definition.",
    "Adoption has not slowed since the spec landed.",
    "Prevalence estimates range from 0.5% to 5%.",
])
def test_ordinary_findings_are_not_flagged(text):
    """A source reporting a null result is a finding. Flagging it would push
    the composer away from reporting what the research actually says."""
    assert not absence_claims(text), text


def test_a_deck_may_repeat_an_absence_a_SOURCE_reports():
    """The rule is about who is making the claim, not the words used."""
    notes = [{"claim": "Review finds no longitudinal data exists",
              "detail": "The authors note that no longitudinal studies exist "
                        "on this pathway."}]
    assert not unsupported_absence("No longitudinal studies exist on this.", notes)


def test_the_same_claim_is_rejected_when_no_source_says_it():
    assert unsupported_absence("No longitudinal studies exist on this.", NPD_NOTES)


def test_a_grounded_deck_passes():
    slides = [
        {"type": "hook", "headline": "What narcissism actually is",
         "sub": "Grandiose and vulnerable subtypes, and why the label spread."},
        {"type": "point", "headline": "The subtypes",
         "bullets": ["Grandiose NPD", "Vulnerable NPD"]},
    ]
    assert check_deck(slides, NPD_NOTES, "Prevalence estimates vary.") == []


def test_an_empty_deck_is_a_problem():
    assert check_deck([], NPD_NOTES, "") == ["deck has no slides"]


def test_bullets_and_quotes_are_searched_too():
    """The claim can hide anywhere the reader will see it."""
    slides = [{"type": "point", "headline": "Fine",
               "bullets": ["That research simply hasn't been done yet"]}]
    assert check_deck(slides, NPD_NOTES, "")


def test_a_claim_that_appears_ONLY_in_the_caption_is_caught():
    """The caption is what most readers actually read, and item 116's worst
    sentence lived there: "That research simply hasn't been done yet.\""""
    clean_slides = [
        {"type": "hook", "headline": "What narcissism actually is",
         "sub": "Grandiose and vulnerable subtypes."},
    ]
    assert check_deck(clean_slides, NPD_NOTES, ITEM_116_CAPTION)


def test_a_claim_that_appears_ONLY_in_bullets_is_caught():
    clean_caption = "Prevalence estimates vary."
    slides = [{"type": "point", "headline": "Subtypes",
               "bullets": ["Grandiose NPD", "No longitudinal studies exist on this"]}]
    assert check_deck(slides, NPD_NOTES, clean_caption)


def test_a_claim_that_appears_ONLY_in_a_quote_slide_is_caught():
    slides = [{"type": "quote", "headline": "On the evidence",
               "quote": "That research simply hasn't been done yet."}]
    assert check_deck(slides, NPD_NOTES, "Prevalence estimates vary.")


# ------------------------------------------- how much of a deck may be a gap


def test_one_slide_naming_a_gap_is_fine():
    slides = [
        {"type": "hook", "headline": "What NPD is", "sub": "Clinical definition."},
        {"type": "point", "headline": "The subtypes", "bullets": ["Grandiose", "Vulnerable"]},
        {"type": "takeaway", "headline": "Not covered here",
         "sub": "These sources do not address childhood outcomes."},
    ]
    assert check_deck(slides, NPD_NOTES, "Prevalence varies.") == []


def test_a_deck_built_out_of_its_own_shortfall_is_rejected():
    """Item 116's rebuild spent two of seven slides on what it could not find,
    one of them a single sentence broken across bullets."""
    slides = [
        {"type": "hook", "headline": "What NPD is", "sub": "Clinical definition."},
        {"type": "point", "headline": "Childhood exposure effects",
         "bullets": ["The provided sources do not address",
                     "how narcissistic caregivers affect", "children"]},
        {"type": "point", "headline": "Adult illness links",
         "bullets": ["No provided source connects", "childhood exposure to",
                     "adult health outcomes"]},
    ]
    problems = check_deck(slides, NPD_NOTES, "")
    assert problems and "missing material" in problems[0]


def test_the_positions_are_named_so_the_rebuild_knows_which():
    slides = [
        {"type": "hook", "headline": "Fine", "sub": "Fine."},
        {"type": "point", "headline": "A", "sub": "These sources do not address X."},
        {"type": "point", "headline": "B", "sub": "No provided source covers Y."},
    ]
    assert "[2, 3]" in check_deck(slides, NPD_NOTES, "")[0]


def test_ordinary_slides_are_not_counted_as_gaps():
    from pipeline.integrity import gap_slides
    slides = [
        {"type": "point", "headline": "Sources agree on the definition",
         "bullets": ["Mayo Clinic", "Nature"]},
        {"type": "point", "headline": "Prevalence", "sub": "About 7.7% of males."},
    ]
    assert gap_slides(slides) == []
