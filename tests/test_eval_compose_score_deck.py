"""`score_deck` must agree with production on what an empty slide is, and
must see a headline promising a count the deck does not deliver.

T100 -- `BODY_FIELDS` was a third, flat, type-agnostic copy of the body
contract `REQUIRED_BODY_FIELDS` (T78) already made the single per-type
source of truth. The consequence was not cosmetic: a `links` slide carrying
only `sub` (B4) has `"sub"` in the flat tuple, so it scored as non-empty
here while `normalise_slides` drops that exact slide in production for
carrying none of `links`'s own required field -- the harness rated a broken
deck `0 empty_slides`.

T104 -- the user's report was "the heading says 7 best but the rest of the
image contains only max of 4 points". No metric anywhere compared a numeral
in a headline to what the deck actually delivered.

`scripts/eval_compose.py` has no package `__init__.py`, so it is loaded the
same way `tests/test_eval_compose_rescore.py` already does: via
`importlib`, not a normal import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pipeline.models import Item, Status

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_compose():
    spec = importlib.util.spec_from_file_location(
        "eval_compose_under_test_score_deck", ROOT / "scripts" / "eval_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_compose = _load_eval_compose()


def _item(intent: str = "news", research: list[dict] | None = None) -> Item:
    return Item(id=1, source="channel", status=Status.SYNTHESIZED,
                intent=intent, research=research or [])


# --------------------------------------------------------------- T100: BODY_FIELDS


def test_body_fields_constant_is_gone():
    assert not hasattr(eval_compose, "BODY_FIELDS")


def test_score_deck_consumes_required_body_fields_not_a_third_copy():
    """Importing the SAME object, not a re-typed copy, is what T100 requires --
    a copy that merely happened to agree today would drift the moment T78's
    contract next changes."""
    from pipeline.stages.compose import REQUIRED_BODY_FIELDS as compose_contract
    assert eval_compose.REQUIRED_BODY_FIELDS is compose_contract


def test_b4_links_slide_carrying_only_sub_now_scores_as_empty():
    """The exact B4 slide: a `links` slide with `sub` but no `links`.

    Under the old flat BODY_FIELDS tuple, "sub" alone satisfied the
    type-agnostic check and this slide scored as non-empty -- while
    normalise_slides drops it in production for having none of "links"'s own
    required field. This is the regression T100 exists to close.
    """
    doc = {"slides": [
        {"type": "links", "headline": "All the links", "sub": "Screenshot this slide."},
    ]}
    row = eval_compose.score_deck(_item(), doc)
    assert row["empty_slides"] == 1


def test_a_links_slide_carrying_links_is_not_empty():
    doc = {"slides": [
        {"type": "links", "headline": "All the links", "links": ["https://a.example"]},
    ]}
    row = eval_compose.score_deck(_item(), doc)
    assert row["empty_slides"] == 0


def test_a_slide_of_unrecognised_type_scores_as_empty():
    doc = {"slides": [{"type": "bogus", "headline": "Whatever", "sub": "text"}]}
    row = eval_compose.score_deck(_item(), doc)
    assert row["empty_slides"] == 1


def test_an_empty_bullets_list_still_scores_as_empty():
    """A present-but-empty container is falsy, same as absent -- the boundary
    a flat `any()` over field NAMES (not values) could get wrong."""
    doc = {"slides": [{"type": "point", "headline": "Fine", "bullets": []}]}
    row = eval_compose.score_deck(_item(), doc)
    assert row["empty_slides"] == 1


def test_a_normal_populated_deck_scores_zero_empty_slides():
    doc = {"slides": [
        {"type": "hook", "headline": "Fine", "sub": "A real sub."},
        {"type": "point", "headline": "Fine", "bullets": ["a", "b"]},
        {"type": "follow", "headline": "Follow", "sub": "Daily tech, explained."},
    ]}
    row = eval_compose.score_deck(_item(), doc)
    assert row["empty_slides"] == 0


# ------------------------------------------------------ T104: headline_count_mismatch


def test_score_deck_returns_headline_count_mismatch_key():
    doc = {"slides": [{"type": "hook", "headline": "Fine", "sub": "s"}]}
    row = eval_compose.score_deck(_item(), doc)
    assert "headline_count_mismatch" in row
    assert row["headline_count_mismatch"] == 0


def test_a_hook_promising_8_with_7_repo_slides_scores_1():
    slides = [{"type": "hook", "headline": "8 Best Repos to Explore This Week",
               "sub": "A roundup."}]
    slides += [{"type": "repo", "headline": f"repo{i}", "name": f"repo{i}"}
               for i in range(7)]
    slides.append({"type": "links", "headline": "All the links",
                    "links": [f"https://x.example/{i}" for i in range(7)]})
    doc = {"slides": slides}
    row = eval_compose.score_deck(_item(intent="list"), doc)
    assert row["headline_count_mismatch"] == 1


def test_a_consistent_deck_of_the_same_shape_scores_0():
    slides = [{"type": "hook", "headline": "7 Best Repos to Explore This Week",
               "sub": "A roundup."}]
    slides += [{"type": "repo", "headline": f"repo{i}", "name": f"repo{i}"}
               for i in range(7)]
    slides.append({"type": "links", "headline": "All the links",
                    "links": [f"https://x.example/{i}" for i in range(7)]})
    doc = {"slides": slides}
    row = eval_compose.score_deck(_item(intent="list"), doc)
    assert row["headline_count_mismatch"] == 0


def test_mismatch_and_match_must_not_score_the_same():
    """The exact blindness T104 removes: these two decks must not agree."""
    mismatch_slides = [{"type": "hook", "headline": "8 Best Repos", "sub": "s"}]
    mismatch_slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]
    match_slides = [{"type": "hook", "headline": "7 Best Repos", "sub": "s"}]
    match_slides += [{"type": "repo", "headline": "r", "name": "r"} for _ in range(7)]

    mismatch_row = eval_compose.score_deck(_item(intent="list"), {"slides": mismatch_slides})
    match_row = eval_compose.score_deck(_item(intent="list"), {"slides": match_slides})
    assert mismatch_row["headline_count_mismatch"] != match_row["headline_count_mismatch"]
    assert mismatch_row["headline_count_mismatch"] == 1
    assert match_row["headline_count_mismatch"] == 0


def test_summarise_aggregates_headline_count_mismatch():
    rows = [
        {"slides": 3, "distinct_types": 2, "rich_slides": 1,
         "empty_slides": 0, "overlong_headlines": 0, "shape_score": 3,
         "bad_repo_urls": 0, "headline_count_mismatch": 1, "hashtags": 0,
         "theme": "signal", "content_slides": 2, "material_units": 3,
         "unsupported_rich": 0},
        {"slides": 3, "distinct_types": 2, "rich_slides": 1,
         "empty_slides": 0, "overlong_headlines": 0, "shape_score": 3,
         "bad_repo_urls": 0, "headline_count_mismatch": 0, "hashtags": 0,
         "theme": "signal", "content_slides": 2, "material_units": 3,
         "unsupported_rich": 0},
    ]
    summary = eval_compose.summarise(rows)
    assert summary["total_headline_count_mismatch"] == 1


def test_score_deck_carries_material_fields_on_the_live_path():
    """T249: the live run() row (score_deck) has the reported-only fields."""
    item = Item(id=1, source="channel", status=Status.SYNTHESIZED, intent="news",
                research=[{"claim": "Revenue grew 40% last year"}],
                raw_text="Revenue grew 40% last year.")
    doc = {"slides": [
        {"type": "hook", "headline": "Revenue"},
        {"type": "kpi", "headline": "Growth", "tiles": [{"value": "99%"}]},
        {"type": "point", "headline": "Plain", "bullets": ["x"]},
        {"type": "sources", "headline": "Sources"},
    ]}
    row = eval_compose.score_deck(item, doc)
    assert (row["content_slides"], row["material_units"], row["unsupported_rich"]) == (2, 1, 1)
