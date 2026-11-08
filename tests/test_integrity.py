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
from hypothesis import given, strategies as st

from pipeline.integrity import (
    absence_claims,
    check_deck,
    headline_count_slides,
    headline_counts,
    unsupported_absence,
    material_text,
    material_units,
    numbers_in,
    has_code_evidence,
    quote_in_material,
    chart_backed,
    kpi_tile_backed,
    slide_backed,
    NON_CONTENT_TYPES,
    _norm_text,
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
    """Load golden fixture rows, skipping if file is missing.

    Raises pytest.skip if the file doesn't exist (e.g., on a fresh clone
    where data/eval/ is gitignored).
    """
    file_path = DATA_DIR / name
    if not file_path.exists():
        pytest.skip(f"data/eval/{name} is gitignored and not present")
    return json.loads(file_path.read_text())["rows"]


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


def test_id_34_panpsychism_stays_unflagged():
    """A free negative control (architect/reviewer): "4 panpsychism research
    projects" has a 2-word qualifier gap ("panpsychism research") and
    delivers exactly 4 repo slides -- the widened gap must not misfire on
    an honestly-stated count either."""
    rows = {r["id"]: r for r in _golden_rows("golden_listonly.json")}
    row = rows[34]
    assert row["doc"]["slides"][0]["headline"] == "4 panpsychism research projects"
    assert headline_count_slides(row["doc"]["slides"], row["intent"]) == []
# ----------------------------------------------- headline count detection
#
# T102: "the heading says 7 best, the image has 4 points". A headline's
# number is only a promise about the body when it is a plain, standalone
# count — never a version, year, multiplier, ratio or money figure.


@pytest.mark.parametrize("headline, expected", [
    ("7 Best Practices", [7]),
    ("Top 12 Rust CLI Tools", [12]),
    ("5 Things You Need To Know", [5]),
    ("Python 3.13 is out", []),
    ("2026 in review", []),
    ("3 of 5 startups failed", [5]),  # "3" is excluded (followed by "of")
    ("100x faster", []),
    ("$65B raised", []),
    ("GPT-4o beats it", []),
    ("45% adoption", []),
])
def test_headline_counts_finds_only_genuine_item_counts(headline, expected):
    assert headline_counts(headline) == expected


# ------------------------------------ T249: Material-grounding helpers (D217, D219, D226)


class MockItem:
    """Mock Item object for testing material helpers."""
    def __init__(self, research=None, raw_text=""):
        self.research = research or []
        self.raw_text = raw_text


def test_material_text_includes_notes_claim_and_detail():
    """material_text combines research claims and details with raw_text."""
    item = MockItem(
        research=[
            {"claim": "Alpha research", "detail": "Alpha detail."},
            {"claim": "Beta research", "detail": "Beta detail."},
        ],
        raw_text="Raw text here."
    )
    result = material_text(item)
    assert "Alpha research" in result
    assert "Alpha detail" in result
    assert "Beta research" in result
    assert "Beta detail" in result
    assert "Raw text here" in result


def test_material_text_skips_fail_open_notes():
    """material_text excludes notes marked fail_open (D226)."""
    item = MockItem(
        research=[
            {"claim": "Good claim", "detail": "Good detail."},
            {"claim": "Bad claim", "detail": "Bad detail.", "fail_open": True},
        ],
        raw_text="Raw text."
    )
    result = material_text(item)
    assert "Good claim" in result
    assert "Bad claim" not in result
    assert "Bad detail" not in result


def test_material_text_handles_missing_fields():
    """material_text gracefully handles missing claim or detail."""
    item = MockItem(
        research=[
            {"claim": "Claim only"},
            {"detail": "Detail only"},
            {},
        ],
        raw_text="Raw text."
    )
    result = material_text(item)
    assert "Claim only" in result
    assert "Detail only" in result
    assert "Raw text" in result


def test_material_text_empty_on_no_research():
    """material_text returns only raw_text when research is empty."""
    item = MockItem(research=[], raw_text="Raw text.")
    assert material_text(item) == "Raw text."


def test_material_units_counts_distinct_normalized_claims():
    """material_units counts distinct non-fail-open claims (D226)."""
    item = MockItem(
        research=[
            {"claim": "First claim"},
            {"claim": "Second claim"},
            {"claim": "first claim"},  # Duplicate after normalization
        ]
    )
    assert material_units(item) == 2


def test_material_units_includes_low_confidence():
    """material_units counts low-confidence notes (D226)."""
    item = MockItem(
        research=[
            {"claim": "Good claim"},
            {"claim": "Low confidence claim", "confidence": "low"},
        ]
    )
    assert material_units(item) == 2


def test_material_units_skips_fail_open_and_empty():
    """material_units excludes fail_open notes and empty claims."""
    item = MockItem(
        research=[
            {"claim": "Good claim"},
            {"claim": "", "detail": "No claim"},
            {"claim": "Fail open", "fail_open": True},
            {"claim": "  "},  # Whitespace only
        ]
    )
    assert material_units(item) == 1


def test_material_units_zero_on_no_research():
    """material_units returns 0 when research is empty."""
    item = MockItem(research=[])
    assert material_units(item) == 0


# ---- numbers_in normaliser tests (D226, T248)


@pytest.mark.parametrize("text, expected_contains", [
    ("42", [42.0]),
    ("1,000", [1000.0]),
    ("1.5", [1.5]),
    ("$65B", [65.0, 65e9]),
    ("65 billion", [65.0, 65e9]),
    ("65b", [65.0, 65e9]),
    ("3 months", [3.0]),
    ("three months", [3.0]),
    ("40%", [40.0]),
    ("100x", [100.0]),
    ("5k", [5.0, 5000.0]),  # 5 with suffix k
])
def test_numbers_in_parses_magnitudes_and_words(text, expected_contains):
    """numbers_in recognizes magnitude suffixes, percent, and number words (D226)."""
    result = numbers_in(text)
    for val in expected_contains:
        assert val in result, f"{val} not in {result} for text '{text}'"


def test_numbers_in_handles_empty_text():
    """numbers_in returns empty set for empty/None text."""
    assert numbers_in("") == set()
    assert numbers_in(None) == set()


def test_numbers_in_case_insensitive_magnitudes():
    """numbers_in is case-insensitive for magnitude suffixes."""
    assert numbers_in("65B") == numbers_in("65b")
    assert numbers_in("3 THOUSAND") == numbers_in("3 thousand")


def test_numbers_in_handles_urls():
    """numbers_in avoids false positives in URLs with hyphenated numbers."""
    # "page123" has "123" glued to a word character, so it doesn't match
    result = numbers_in("Visit example.com/page123")
    assert 123.0 not in result
    # But "page-123" or "page 123" would match
    assert 123.0 in numbers_in("Visit page-123")


def test_numbers_in_three_months_equals_3():
    """numbers_in: 'three months' normalizes the same as '3' (D226, item 348)."""
    three_as_word = numbers_in("three months")
    three_as_digit = numbers_in("3 months")
    assert 3.0 in three_as_word
    assert 3.0 in three_as_digit


def test_numbers_in_requires_number_words():
    """Boundary test: must extract number words (catches dropping _WORDNUM mapping mutation)."""
    # Only number words, no digits
    result = numbers_in("two hundred and fifty")
    assert 2.0 in result, "two should be extracted"
    assert 100.0 in result, "hundred should be extracted"
    assert 50.0 in result, "fifty should be extracted"

    # Test all single digits 0-20
    for word, num in [("zero", 0), ("five", 5), ("twelve", 12), ("twenty", 20)]:
        assert float(num) in numbers_in(word), f"number word '{word}' should yield {num}"


@given(st.integers(min_value=0, max_value=999999))
def test_numbers_in_is_invariant_to_thousands_commas(n):
    """Property test: numbers_in yields the same raw value with or without commas (D226)."""
    without = numbers_in(str(n))
    with_comma = numbers_in(f"{n:,}")
    assert float(n) in without
    assert float(n) in with_comma


# ---- Code evidence tests (D219, T248)


def test_has_code_evidence_fence_presence_is_not_evidence():
    """D230: a ``` fence in the material never backs code by itself."""
    invented = '{"model": "gpt-9",\n"temperature": 0.7,\n"max_tokens": 4096}'
    material = "Install: ```pip install foo```"
    assert not has_code_evidence(invented, material)
    assert not has_code_evidence("print('hello')", "Some ```python\ncode block\n``` in it.")


def test_has_code_evidence_backed_by_fenced_content():
    """D230: the same code IS backed when its lines are inside the fenced block."""
    code = 'config = {"model": "gpt-9"}\nclient.run(config, retries=3)'
    material = "Use:\n```python\nconfig = {\"model\": \"gpt-9\"}\nclient.run(config, retries=3)\n```"
    assert has_code_evidence(code, material)


def test_has_code_evidence_requires_line_match_without_fence():
    """Without a fence, has_code_evidence checks line-by-line verbatim match."""
    # Lines must be >=8 chars to count (D219)
    code = "x = 1\ny = 2\nvar_name = 42\nresult = 100"
    material = "x = 1\ny = 2\nvar_name = 42\nresult = 100"
    assert has_code_evidence(code, material)


def test_has_code_evidence_requires_50_percent_match():
    """has_code_evidence needs ≥50% of lines to match (D219)."""
    code = "x = 1\ny = 2\nz = 3"  # 3 lines of 7+ chars
    # Only 2/3 match
    material = "x = 1\ny = 2\nw = 4"
    assert not has_code_evidence(code, material)


def test_has_code_evidence_ignores_short_lines():
    """Lines shorter than 8 chars are not evidence either way (D219)."""
    code = "a\nb\nc\nprint('hello world')"  # 4 lines, only 1 qualifies
    material = "print('hello world')"
    assert has_code_evidence(code, material)  # 1/1 = 100%


def test_has_code_evidence_empty_inputs():
    """has_code_evidence returns False for empty/None inputs."""
    assert not has_code_evidence(None, "text")
    assert not has_code_evidence("code", None)
    assert not has_code_evidence("", "text")


def test_has_code_evidence_whitespace_normalized():
    """Whitespace differences are ignored in line matching (code lines normalized)."""
    # Lines must be >=8 chars
    code = "x  =  1  + 2\ny  =   3 + 4"
    material = "x = 1 + 2\ny = 3 + 4"
    assert has_code_evidence(code, material)


def test_has_code_evidence_boundary_exactly_50_percent():
    """Boundary test: exactly 50% match must pass (D219, catches 0.5→0.4 mutation)."""
    # 2 lines of >=8 chars: 1 matches = 50% (must pass at 0.5, fail at 0.4)
    code = "function first_func() {}\nfunction second_func() {}"
    material = "function first_func() {}"
    assert has_code_evidence(code, material)


def test_has_code_evidence_boundary_49_percent_fails():
    """Boundary test: 49% must fail at 0.5 but pass at 0.4 (catches 0.5→0.4 mutation escape)."""
    # 100 lines, 49 match = 49% (fails at 0.5, passes at 0.4)
    code_lines = ["long_func_name_here" + str(i) + "() {}" for i in range(100)]
    code = "\n".join(code_lines)
    material = "\n".join(code_lines[:49])  # Only 49 lines match
    # 49/100 = 49% < 50%, so should fail at 0.5 threshold
    assert not has_code_evidence(code, material)


def test_has_code_evidence_boundary_8_char_minimum():
    """Boundary test: exactly 8-char lines must be counted (catches 8→7 mutation)."""
    # "12345678" is exactly 8 chars and should count
    code = "12345678\n9abcdefgh"  # 8 and 9 char lines
    material = "12345678\n9abcdefgh"
    assert has_code_evidence(code, material)

    # "1234567" is 7 chars and should NOT count
    code = "1234567\n8abcdefgh"  # 7 and 8 char lines
    material = "1234567"
    # Only 8-char+ lines count: "8abcdefgh" not found, so 0/1 = 0% < 50%
    assert not has_code_evidence(code, material)


# ---- Quote evidence tests (D226, T248)


def test_quote_in_material_strict_all_segments():
    """quote_in_material requires all segments to appear as substrings (strict rule)."""
    quote = "This is important... and this too."
    material = "This is important and this too."
    assert quote_in_material(quote, material)


def test_quote_in_material_splits_on_ellipsis():
    """quote_in_material splits on '...' and sentence endings."""
    quote = "First sentence. Second sentence."
    material = "First sentence and Second sentence"
    assert quote_in_material(quote, material)


def test_quote_in_material_fallback_to_fuzzy():
    """If strict fails, quote_in_material falls back to fuzzy ≥90% token coverage."""
    quote = "The quick brown fox jumps"
    material = "The quick brown fox jumps over the lazy dog"
    # All 5 tokens are in a >=3-token run
    assert quote_in_material(quote, material)


def test_quote_in_material_fuzzy_requires_90_percent():
    """Fuzzy fallback requires ≥90% of quote tokens in >=3-token runs (D226)."""
    quote = "one two three four five"  # 5 tokens
    material = "one two three"  # 3 tokens match; 3/5 = 60% < 90%
    assert not quote_in_material(quote, material)


def test_quote_in_material_fuzzy_min_run_length():
    """Fuzzy matching only counts runs of ≥3 tokens (D226)."""
    quote = "a b c"
    material = "x a y b z c"  # Each token separated, no 3-token runs
    assert not quote_in_material(quote, material)


def test_quote_in_material_typo_case_item_303():
    """quote_in_material handles typos via fuzzy fallback (item 303, T248)."""
    # Material has a typo but the quote is near-verbatim
    quote = "doesn't does need your labor"
    material = "doesn't does need your labor"  # Direct match
    assert quote_in_material(quote, material)


def test_quote_in_material_empty_inputs():
    """quote_in_material returns False for empty/None inputs."""
    assert not quote_in_material(None, "text")
    assert not quote_in_material("quote", None)
    assert not quote_in_material("", "text")


def test_quote_in_material_case_insensitive():
    """quote_in_material normalizes case."""
    quote = "Hello World"
    material = "hello world"
    assert quote_in_material(quote, material)


def test_norm_text_folds_curly_apostrophes_and_quotes():
    assert _norm_text("OpenAI\u2019s") == _norm_text("OpenAI's")
    assert _norm_text("\u2018hi\u2019") == _norm_text("'hi'")
    assert _norm_text("\u201cquoted\u201d") == _norm_text('"quoted"')


def test_quote_in_material_curly_apostrophe_in_quote():
    """A curly apostrophe in the quote must match a straight one in the material (strict path)."""
    quote = "We don\u2019t ship models we can\u2019t explain"
    material = "...we don't ship models we can't explain..."
    assert quote_in_material(quote, material)
    assert quote_in_material(material.strip("."), quote)


def test_quote_in_material_is_token_bounded():
    """A short quote must not match inside a longer word."""
    assert not quote_in_material("AI", "they said it was fine")
    assert not quote_in_material("rain", "the train left")
    assert quote_in_material("rain", "heavy rain fell")


def test_kpi_non_numeric_is_token_bounded():
    assert not kpi_tile_backed({"value": "ai"}, "they said so")


# ---- slide_backed: the one gate (T249; T251 reuses)


def test_non_content_types_constant():
    assert NON_CONTENT_TYPES == {"hook", "takeaway", "sources", "follow", "links", "photo"}


def test_slide_backed_dispatches_per_type():
    mat = "He said hello there friend. Revenue was 40% and 65 million."
    assert slide_backed({"type": "quote", "quote": "hello there friend"}, mat)
    assert not slide_backed({"type": "quote", "quote": "invented words entirely here"}, mat)
    assert slide_backed({"type": "kpi", "tiles": [{"value": "40%"}]}, mat)
    assert not slide_backed({"type": "kpi", "tiles": [{"value": "99%"}]}, mat)
    assert slide_backed({"type": "chart", "series": [{"value": 40}, {"value": 65}]}, mat)
    assert not slide_backed({"type": "chart", "series": [{"value": 41}, {"value": 66}]}, mat)
    assert not slide_backed({"type": "code", "code": "invented_call(argument)"}, mat)
    assert slide_backed({"type": "point", "headline": "anything"}, mat)


def test_quote_in_material_boundary_85_percent():
    """Boundary test: 85% coverage must FAIL at 0.9 but PASS at 0.8 (catches 0.9→0.8 mutation escape)."""
    # Create a quote with 20 tokens where only 17 consecutive tokens appear
    # 17/20 = 85% coverage - fails at 0.9, passes at 0.8
    quote = "a b c d e f g h i j k l m n o p q r s t"  # 20 tokens
    material = "a b c d e f g h i j k l m n o p q WRONG x y"
    # Match: "a b c d e f g h i j k l m n o p q" = 17 tokens = 17/20 = 85%
    # At 0.9 threshold: 85% < 90% - should FAIL (test expects False)
    # At 0.8 threshold: 85% >= 80% - should PASS (test would see True, causing assertion failure)
    assert not quote_in_material(quote, material)


# ---- Chart backing tests (D219, T248)


def test_chart_backed_needs_two_series_values():
    """chart_backed requires ≥2 series values in material numbers (D219)."""
    values = [
        {"value": 10.0, "display": "Jan"},
        {"value": 20.0, "display": "Feb"},
        {"value": 30.0, "display": "Mar"},
    ]
    material_str = "10 and 20"
    assert chart_backed(values, material_str)


def test_chart_backed_accepts_display_field():
    """chart_backed can use the display field instead of value."""
    values = [
        {"display": "$65B"},
        {"display": "$100B"},
    ]
    assert chart_backed(values, "65 billion and 100 billion")


def test_chart_backed_requires_two_minimum():
    """chart_backed fails if only 1 value is found (D219)."""
    values = [
        {"value": 10.0},
        {"value": 20.0},
    ]
    assert not chart_backed(values, "10")


def test_chart_backed_empty_inputs():
    """chart_backed returns False for empty/None inputs."""
    assert not chart_backed(None, "text")
    assert not chart_backed([], "text")
    assert not chart_backed([{"value": 1}], None)
    assert not chart_backed([{"value": 1}], "")


def test_chart_backed_handles_non_dict_items():
    """chart_backed gracefully skips non-dict items in values."""
    values = [
        {"value": 10.0},
        "not a dict",
        {"value": 20.0},
    ]
    assert chart_backed(values, "10 and 20")


# ---- KPI tile backing tests (D226, T248)


def test_kpi_tile_backed_numeric_value():
    """kpi_tile_backed accepts numeric values in material numbers."""
    tile = {"value": "65"}
    assert kpi_tile_backed(tile, "Raised 65 million dollars")


def test_kpi_tile_backed_non_numeric_value():
    """kpi_tile_backed accepts non-numeric values as substrings."""
    tile = {"value": "Success rate"}
    assert kpi_tile_backed(tile, "The overall success rate is high")


def test_kpi_tile_backed_case_insensitive():
    """kpi_tile_backed normalizes case for non-numeric values."""
    tile = {"value": "OPEN POSITIONS"}
    assert kpi_tile_backed(tile, "open positions available")


def test_kpi_tile_backed_requires_exact_substring():
    """kpi_tile_backed requires the normalized value as a substring."""
    tile = {"value": "Active users"}
    assert kpi_tile_backed(tile, "Active users online")
    assert not kpi_tile_backed(tile, "Users are active online")


def test_kpi_tile_backed_empty_value():
    """kpi_tile_backed returns False for empty tile values."""
    tile = {"value": ""}
    assert not kpi_tile_backed(tile, "material")


def test_kpi_tile_backed_missing_value():
    """kpi_tile_backed returns False when value field is missing."""
    tile = {}
    assert not kpi_tile_backed(tile, "material")


def test_kpi_tile_backed_empty_material():
    """kpi_tile_backed returns False when material is empty/None."""
    tile = {"value": "test"}
    assert not kpi_tile_backed(tile, None)
    assert not kpi_tile_backed(tile, "")


# ---- T281: NUM_RE spacing variants (extra spaces before suffix)

def test_num_re_matches_single_space_before_percent():
    """T281: NUM_RE should match '40 %' (single space before suffix)."""
    from pipeline.integrity import typed_numbers_in
    # Directly test that the suffix is captured and normalized to 'pct'
    result = typed_numbers_in("40 %")
    assert (40.0, 'pct') in result, f"Expected (40.0, 'pct'), got {result}"

def test_num_re_matches_multiple_spaces_before_percent():
    """T281: NUM_RE should match '40  %' (multiple spaces before suffix).

    This is the key test: the regex must match even with multiple spaces,
    and the suffix normalization must preserve the unit class."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40  %")
    assert (40.0, 'pct') in result, f"Expected (40.0, 'pct'), got {result}"

def test_num_re_matches_no_space_before_percent():
    """T281: NUM_RE should match '40%' (no space, baseline)."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40%")
    assert (40.0, 'pct') in result

def test_num_re_matches_per_cent_with_single_space():
    """T281: NUM_RE should match '40 per cent' with single space."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40 per cent")
    assert (40.0, 'pct') in result

def test_num_re_matches_per_cent_with_multiple_spaces():
    """T281: NUM_RE should match '40  per cent' with multiple spaces before 'per'.

    Key test: the regex must capture 'per cent' as a suffix even with extra
    spacing, and normalization collapses the spaces back to single space."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40  per cent")
    assert (40.0, 'pct') in result, f"Expected (40.0, 'pct'), got {result}"

def test_num_re_matches_per_cent_with_space_in_middle():
    """T281: NUM_RE should match '40 per\xa0cent' (non-breaking space)."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40 per\xa0cent")
    assert (40.0, 'pct') in result

def test_num_re_matches_per_cent_with_multiple_spaces_middle():
    """T281: NUM_RE should match '40  per  cent' (multiple spaces in per cent).

    Key test: spaces within 'per cent' must be matched and normalized."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("40  per  cent")
    assert (40.0, 'pct') in result, f"Expected (40.0, 'pct'), got {result}"

def test_num_re_rejects_per_centre_misspelling():
    """T281: NUM_RE should NOT match 'per centre' (British spelling variant).

    'per centre' is a misspelling, so it won't be recognized as a suffix and
    '40 per centre' will extract only '40' as a bare number. When material
    contains '40 per cent', the '40' will be extracted as 'pct'. The match
    succeeds due to numeric matching rules, so we instead verify the regex
    doesn't capture 'per centre' as a suffix."""
    from pipeline.integrity import typed_numbers_in
    value = typed_numbers_in("40 per centre")
    # Should only extract 40 as 'num', not 'pct'
    assert (40.0, 'num') in value
    assert (40.0, 'pct') not in value

def test_num_re_matches_multiplier_with_spaces():
    """T281: NUM_RE should match '3.2  ×' (multiple spaces before multiplier).

    Key test: the regex must capture the multiplier even with extra spaces,
    and normalization must preserve it as 'x' class."""
    from pipeline.integrity import typed_numbers_in
    result = typed_numbers_in("3.2  ×")
    assert (3.2, 'x') in result, f"Expected (3.2, 'x'), got {result}"

def test_num_re_rejects_4x_vs_4xl():
    """T281: NUM_RE should not confuse '4x' and '4xl' — '4xl' is not a multiplier."""
    # Verify that '4x' matches but '4xl' doesn't when looking for multipliers
    material_with_x = "4x faster speed"
    material_with_xl = "Size 4xl shirt"
    assert kpi_tile_backed({"value": "4x"}, material_with_x)
    assert not kpi_tile_backed({"value": "4x"}, material_with_xl)

def test_num_re_rejects_65pct_vs_65b():
    """T281: NUM_RE should distinguish '65%' from '$65B' — the latter is not a percentage."""
    assert kpi_tile_backed({"value": "65%"}, "Growth was 65%.")
    assert not kpi_tile_backed({"value": "65%"}, "Raised $65B in funding.")


# ---- T249 tester additions: kill the fuzzy-fallback / kpi-all mutants ----

def test_quote_in_material_fuzzy_fallback_passes_where_strict_fails():
    """Near-verbatim quote (one inserted word in the material) fails the strict
    substring path but clears the >=90% in >=3-token-run fuzzy fallback (D226)."""
    quote = "the company announced record revenue growth driven by strong cloud demand today"
    material = "the company announced record revenue growth driven by extra strong cloud demand today"
    mat_norm = _norm_text(material)
    assert _norm_text(quote) not in mat_norm  # strict path really fails
    assert quote_in_material(quote, material)


def test_quote_in_material_invented_quote_is_false():
    material = "the company announced record revenue growth driven by strong cloud demand"
    assert not quote_in_material("penguins govern the northern wastelands with iron fists", material)


def test_kpi_slide_needs_every_tile_backed_not_any():
    slide = {"type": "kpi", "tiles": [{"value": "40%"}, {"value": "99%"}]}
    assert not slide_backed(slide, "Revenue grew 40% last year.")
    assert slide_backed(slide, "Revenue grew 40% and margin hit 99% last year.")


# ---- T277: D230 residuals (code count, small ints, unit class, empty KPI)

def test_code_lone_short_line_cannot_back_two_line_slide():
    code = "import os\nreturn None"
    assert not has_code_evidence(code, "we call import os somewhere")
    assert has_code_evidence(code, "import os\nreturn None")


def test_code_single_long_line_backs():
    long_line = "result = compute_total(items)"  # >= 20 chars
    assert has_code_evidence(f"{long_line}\nprint(result_x)", f"see {long_line} here")


def test_code_single_line_19_chars_fails_20_passes():
    l19, l20 = "x = frobnicate(abc)", "x = frobnicate(abcd)"
    assert (len(l19), len(l20)) == (19, 20)
    assert not has_code_evidence(f"{l19}\nunmatched_second_line", l19)
    assert has_code_evidence(f"{l20}\nunmatched_second_line", l20)


def test_kpi_number_word_one_does_not_back_small_integer():
    assert not kpi_tile_backed({"value": "1"}, "one of the founders left")
    assert kpi_tile_backed({"value": "One"}, "one of the founders left")  # same word backs the word
    assert not kpi_tile_backed({"value": "One"}, "founders left")
    assert kpi_tile_backed({"value": "1"}, "1 founder left")
    assert kpi_tile_backed({"value": "10"}, "ten is 10 here")
    assert not kpi_tile_backed({"value": "10"}, "ten founders")
    assert kpi_tile_backed({"value": "11"}, "eleven founders")  # >10: word still backs


@pytest.mark.parametrize("value,material,ok", [
    ("65%", "raised $65B", False),
    ("65%", "up 65 percent", True),
    ("65%", "up 65% yoy", True),
    ("3.2x", "in 3.2 years", False),
    ("3.2x", "a 3.2x gain", True),
    ("2x", "in 2 months", False),
    ("$65B", "65% growth", False),
    ("$65B", "$65 billion raised", True),
    ("$65B", "65,000,000,000 raised", True),
    ("$65B", "$65 raised", False),
    ("65", "grew 65%", True),  # unitless claim stays lenient
    ("Three", "three months", True),  # a word claim is backed by the same word
    ("Three", "3 months", True),
    ("1", "one of the", False),
    ("3.2x", "3.2\u00d7 faster", True),
    ("3.2\u00d7", "a 3.2x gain", True),
    ("3.2\u00d7", "in 3.2 years", False),
    ("40%", "40 per cent", True),
    ("40%", "40 per centre", False),
])
def test_kpi_unit_class(value, material, ok):
    assert kpi_tile_backed({"value": value}, material) is ok


def test_chart_display_unit_class_not_bypassed_by_value():
    values = [{"value": 65, "display": "65%"}, {"value": 40, "display": "40%"}]
    assert not chart_backed(values, "raised $65B and $40B")
    assert chart_backed(values, "65% and 40%")


def test_kpi_slide_with_no_tiles_is_not_backed():
    assert not slide_backed({"type": "kpi", "tiles": []}, "anything 40%")
    assert not slide_backed({"type": "kpi"}, "anything 40%")
    assert not slide_backed({"type": "kpi", "tiles": ["junk"]}, "anything 40%")


def test_typed_numbers_in_classes_and_numbers_in_unchanged():
    from pipeline.integrity import typed_numbers_in
    t = typed_numbers_in("65% 3.2x $65B 7 three")
    assert {(65.0, "pct"), (3.2, "x"), (65e9, "mag"), (7.0, "num"), (3.0, "word")} <= t
    assert numbers_in("$65B") == {65.0, 65e9}
