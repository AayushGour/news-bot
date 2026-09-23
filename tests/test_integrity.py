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

import json
from pathlib import Path

import pytest

from pipeline.integrity import (
    absence_claims,
    check_deck,
    headline_count_slides,
    unsupported_absence,
)

# D25/house rule 3: data/eval/ is gitignored fixture data, absent in a fresh
# worktree -- tests that depend on it skip loudly (see below) rather than
# passing vacuously, matching tests/test_quality.py's own DATA_DIR pattern.
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "eval"

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


# ------------------------------- a headline promising a count it doesn't deliver


def test_a_hook_promising_8_repos_with_only_7_delivered_is_flagged():
    """The user's report: "the heading says 7 best but the rest of the image
    contains only max of 4 points." -- a hook stating one count, a list deck
    delivering another."""
    slides = [{"type": "hook", "headline": "8 Best Repos to Explore This Week"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]
    assert headline_count_slides(slides, "list") == [1]


def test_a_hook_whose_count_matches_the_delivered_repos_is_not_flagged():
    slides = [{"type": "hook", "headline": "7 Best Repos to Explore This Week"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]
    assert headline_count_slides(slides, "list") == []


def test_a_hook_count_is_only_checked_against_repos_on_a_list_intent():
    """A "5 things" hook on a NEWS deck has no repo-count claim behind it --
    news decks are not enumerations, so this is rule 3, not rule 2."""
    slides = [{"type": "hook", "headline": "5 Key Facts About the Outage"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(2)]
    assert headline_count_slides(slides, "news") == []


@pytest.mark.parametrize("field,count,headline", [
    ("bullets", 6, "6 Tips for Faster Builds"),
    ("rows", 5, "5 Facts About the Merger"),
    ("tiles", 4, "4 Key Metrics to Watch"),
    ("links", 10, "10 Links to Save"),
])
def test_a_slide_with_its_own_countable_field_is_checked_against_it(field, count, headline):
    slide = {"type": "point", "headline": headline, field: list(range(count))}
    assert headline_count_slides([slide], "news") == []

    short = {"type": "point", "headline": headline, field: list(range(count - 1))}
    assert headline_count_slides([short], "news") == [1]


@pytest.mark.parametrize("headline", [
    "Python 3.13 is out",
    "2026 in review",
    "3 of 5 startups failed",
    "100x faster",
    "$65B raised",
    "GPT-4o beats it",
])
def test_headlines_with_no_real_enumeration_claim_are_left_alone(headline):
    """These all carry a leading digit and NO countable claim behind it. Run
    through the most permissive path (a list-intent hook, which needs no
    field on the slide itself) so a false positive would be as visible as
    possible."""
    slides = [{"type": "hook", "headline": headline}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(3)]
    assert headline_count_slides(slides, "list") == []


def test_a_slide_with_no_countable_field_at_all_has_no_checkable_claim():
    """A `quote` slide's body ("quote") is a string, not a list -- there is
    nothing here to compare the number against, so it is left alone rather
    than guessed at."""
    slides = [{"type": "quote", "headline": "7 Words That Changed Everything",
               "quote": "Something short."}]
    assert headline_count_slides(slides, "news") == []


def test_positions_are_1_indexed_and_only_the_offending_slide_is_named():
    slides = [
        {"type": "hook", "headline": "Fine intro", "sub": "s"},
        {"type": "point", "headline": "6 Tips for Faster Builds", "bullets": ["a", "b"]},
        {"type": "follow", "headline": "Follow", "sub": "Daily tech, explained."},
    ]
    assert headline_count_slides(slides, "news") == [2]


def test_an_empty_deck_has_no_mismatches():
    assert headline_count_slides([], "list") == []


def test_a_slide_of_unrecognised_type_has_no_checkable_claim():
    assert headline_count_slides([{"type": None, "headline": "7 Best Tools"}], "news") == []


def test_an_empty_body_list_is_a_mismatch_against_any_nonzero_stated_count():
    slides = [{"type": "point", "headline": "5 Tips", "bullets": []}]
    assert headline_count_slides(slides, "news") == [1]


def test_a_stated_zero_against_an_empty_list_is_consistent():
    slides = [{"type": "point", "headline": "0 Tips For This", "bullets": []}]
    assert headline_count_slides(slides, "news") == []


def test_a_non_string_headline_does_not_crash():
    assert headline_count_slides([{"type": "hook", "headline": 123, "sub": "x"}], "news") == []


def test_a_missing_headline_field_does_not_crash():
    assert headline_count_slides([{"type": "hook", "sub": "x"}], "news") == []


def test_multiple_mismatches_are_all_reported_in_order():
    slides = [
        {"type": "point", "headline": "3 Tips", "bullets": ["a"]},
        {"type": "point", "headline": "Fine", "bullets": ["a", "b"]},
        {"type": "point", "headline": "9 Tips", "bullets": ["a", "b"]},
    ]
    assert headline_count_slides(slides, "news") == [1, 3]


def test_the_full_max_items_ceiling_is_not_flagged_at_the_boundary():
    """D53: MAX_ITEMS = 15 is the editorial ceiling -- the boundary itself
    must not misfire."""
    slides = [{"type": "hook", "headline": "15 Best Repos"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(15)]
    assert headline_count_slides(slides, "list") == []


def test_one_short_of_the_stated_count_is_flagged():
    slides = [{"type": "hook", "headline": "15 Best Repos"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(14)]
    assert headline_count_slides(slides, "list") == [1]


# ------------------------------------ bounce 1: a bounded qualifier gap


@pytest.mark.parametrize("headline,gap_words", [
    ("8 GitHub repos for technical interview prep", 1),   # id 26's real hook
    ("8 GitHub repos worth studying", 1),                  # id 29's real hook
    ("8 GitHub repos for interview prep", 1),              # id 30's real hook
    ("8 repos to explore", 0),
    ("8 great repos to explore", 1),
    ("8 really great repos to explore", 2),
    ("8 quite genuinely excellent repos", 3),   # boundary -- the widest gap that still matches
])
def test_a_qualifier_between_the_digit_and_the_listicle_noun_is_still_caught(headline, gap_words):
    """Bounce 1 (reviewer, real data): the original version required the
    listicle noun immediately after the digits, so it returned [] on the
    three decks in golden_current.json that are the user's exact complaint
    shape -- a qualifier word ("GitHub", ...) sits between the digit and
    "repos". A 0-3 word gap is now allowed; this locks in that every gap
    width from 0 through the 3-word boundary still resolves to a real
    mismatch when the deck under-delivers."""
    del gap_words  # documents the case; the assertion is what matters
    slides = [{"type": "hook", "headline": headline}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]
    assert headline_count_slides(slides, "list") == [1]


def test_a_qualifier_gap_wider_than_three_words_is_not_matched():
    """The gap is bounded on purpose (reviewer's fix, not an unbounded scan
    forward for any listicle noun anywhere in the headline) -- a fourth
    qualifier word between the digit and the count word must not match, or
    this detector would start flagging headlines on a coincidental later
    occurrence of a count word rather than a real enumeration claim."""
    slides = [{"type": "hook", "headline": "8 really quite genuinely excellent repos"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]
    assert headline_count_slides(slides, "list") == []


def test_a_hyphen_glued_token_right_after_the_digit_still_never_matches():
    """The mandatory `\\s+` right after the digit, not the word-boundary
    alone, is what excludes a hyphen-glued token -- these are real headlines
    in this repo's own corpus ("06-CSS3", "30-Days-Of-Python", and the
    "...8-hour plan into 16-hour ordeal" hook), reproduced here as
    self-contained cases so this test does not depend on the gitignored
    corpus existing."""
    slides = [{"type": "hook", "headline": "06-CSS3"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(3)]
    assert headline_count_slides(slides, "list") == []

    slides = [{"type": "hook", "headline": "30-Days-Of-Python"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(3)]
    assert headline_count_slides(slides, "list") == []

    slides = [{"type": "hook",
               "headline": "AI-guided climb turned 8-hour plan into 16-hour ordeal"}]
    slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(3)]
    assert headline_count_slides(slides, "list") == []


# --------------------------- bounce 1: real-data positive control (AC1-3)


def _golden_rows(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text())["rows"]


@pytest.mark.skipif(
    not DATA_DIR.exists(), reason="data/eval corpus is gitignored and not present"
)
def test_ids_26_29_30_are_flagged_on_golden_current():
    """The blocking finding this bounce exists to close: this detector could
    not see the three decks in this repo's own corpus that are exactly the
    user's complaint -- a hook promising N repos with only N-1 delivered,
    the qualifier word ("GitHub") sitting between the digit and "repos". A
    real-data positive control so the next reader can watch it fire, rather
    than trusting a zero that reads as "clean" while guarding nothing
    (D78/D92)."""
    rows = {r["id"]: r for r in _golden_rows("golden_current.json")}
    for expect_id in (26, 29, 30):
        row = rows[expect_id]
        assert row["doc"]["slides"][0]["type"] == "hook"
        assert headline_count_slides(row["doc"]["slides"], row["intent"]) == [1], (
            f"id {expect_id} should flag its hook slide"
        )


@pytest.mark.skipif(
    not DATA_DIR.exists(), reason="data/eval corpus is gitignored and not present"
)
def test_golden_current_flags_exactly_these_ids_and_positions():
    """Pin the full corpus result, by id AND position -- not just "26/29/30
    are somewhere in the flagged set". Every non-hook digit-bearing headline
    in this deck's own corpus lives inside these three already-flagged
    decks (id 26's and id 30's own "06-CSS3" repo slide, both real data) --
    an id-only assertion would not notice a second, wrong position flagged
    inside an id that is already expected to be flagged for its hook."""
    rows = _golden_rows("golden_current.json")
    flagged = {
        r["id"]: headline_count_slides(r["doc"]["slides"], r["intent"])
        for r in rows if r.get("doc")
    }
    flagged = {k: v for k, v in flagged.items() if v}
    assert flagged == {26: [1], 29: [1], 30: [1]}


@pytest.mark.skipif(
    not DATA_DIR.exists(), reason="data/eval corpus is gitignored and not present"
)
def test_golden_listonly_flags_nothing():
    """AC2: the same hooks in golden_listonly.json deliver the count they
    state (8/8, 8/8, 8/8) -- the fix must not turn a bounded qualifier gap
    into a false positive on a deck that is actually correct."""
    rows = _golden_rows("golden_listonly.json")
    for r in rows:
        if not r.get("doc"):
            continue
        assert headline_count_slides(r["doc"]["slides"], r["intent"]) == [], (
            f"id {r['id']} should not be flagged"
        )


@pytest.mark.skipif(
    not DATA_DIR.exists(), reason="data/eval corpus is gitignored and not present"
)
def test_id_34_panpsychism_stays_unflagged():
    """A free negative control (architect/reviewer): "4 panpsychism research
    projects" has a 2-word qualifier gap ("panpsychism research") and
    delivers exactly 4 repo slides -- the widened gap must not misfire on
    an honestly-stated count either."""
    rows = {r["id"]: r for r in _golden_rows("golden_listonly.json")}
    row = rows[34]
    assert row["doc"]["slides"][0]["headline"] == "4 panpsychism research projects"
    assert headline_count_slides(row["doc"]["slides"], row["intent"]) == []
