"""Every metric must be able to tell a good deck from a generic one.

Two hand-built decks anchor every test: GOOD_DECK is specific, numeric,
sourced, and uses richer slide types; BAD_DECK is the generic-adjective,
all-bullets, same-old-hook deck the user complained about. A metric that
cannot order these two correctly is not measuring what its name claims to.

No LLM calls anywhere in this file — quality.py has none to call.
"""

from __future__ import annotations

import ast
import copy
import inspect
from pathlib import Path

from pipeline import quality
from pipeline.models import Item, Status
from pipeline.quality import (
    COMPOSITE_WEIGHTS,
    FILLER_PHRASES,
    METRIC_SET_VERSION,
    RICH_SLIDE_TYPES,
    STRUCTURAL_SLIDE_TYPES,
    active_component_weights,
    bare_bullet_ratio,
    code_preserved,
    deck_body_text,
    filler_count,
    grounding_ratio,
    hook_headline,
    hook_similarity,
    numeric_density,
    proper_noun_density,
    score_substance,
    source_text_from_item,
    substance_composite,
)

# A specific, numeric, sourced deck reaching for richer slide types.
GOOD_DECK = {
    "slides": [
        {
            "type": "hook",
            "headline": "GPT-5 doubles MMLU accuracy to 92%",
            "sub": "OpenAI's May 2026 benchmark run beat Gemini 2 by 4 points.",
        },
        {
            "type": "kpi",
            "headline": "Benchmark jump",
            "tiles": [
                {"value": "92%", "label": "MMLU score", "delta": "+38%"},
                {"value": "4.2s", "label": "latency", "delta": "-1.1s"},
            ],
        },
        {
            "type": "code",
            "headline": "New API field",
            "lang": "json",
            "code": '{"model": "gpt-5", "temperature": 0.6}',
        },
        {
            "type": "compare",
            "headline": "GPT-5 vs Gemini 2",
            "left_title": "GPT-5",
            "right_title": "Gemini 2",
            "rows": [["92% MMLU", "88% MMLU"], ["$0.02/1k tokens", "$0.015/1k tokens"]],
        },
        {
            "type": "takeaway",
            "headline": "OpenAI regains the benchmark lead",
            "sub": "The 4-point MMLU gap is the widest since GPT-4 launched in 2023.",
        },
        {"type": "sources", "headline": "Sources", "urls": ["https://openai.com/blog/gpt-5"]},
    ],
    "caption": "OpenAI's GPT-5 posts 92% on MMLU, beating Gemini 2 by 4 points.",
    "hashtags": ["openai", "gpt5", "benchmarks"],
    "theme": "signal",
}

# Generic adjectives, no numbers, all bullets, boilerplate hook -- the
# "posts produced by the free models are very bad" complaint in deck form.
BAD_DECK = {
    "slides": [
        {
            "type": "hook",
            "headline": "Exciting update from a major AI company",
            "sub": "Big changes are coming and it's a real game-changer for everyone.",
        },
        {
            "type": "point",
            "headline": "What happened",
            "bullets": [
                "Something big happened.",
                "It is a huge deal.",
                "Experts are impressed.",
            ],
        },
        {
            "type": "point",
            "headline": "Why it matters",
            "bullets": [
                "This could change everything.",
                "The future looks bright.",
                "You won't believe what's next.",
            ],
        },
        {
            "type": "takeaway",
            "headline": "A big step forward",
            "sub": "This is just the beginning of something amazing.",
        },
        {"type": "sources", "headline": "Sources", "urls": ["https://example.com"]},
    ],
    "caption": "Huge news in AI today!",
    "hashtags": ["ai", "news", "tech"],
    "theme": "signal",
}

# What the model actually had to work from, phrased so GOOD_DECK's wording
# traces back to it and BAD_DECK's generic prose does not.
SOURCE_TEXT = (
    "OpenAI reported the GPT-5 model reached 92% on the MMLU benchmark, a "
    "4 point lead over Gemini 2, at $0.02 per 1k tokens versus $0.015 for "
    "Gemini 2's $0.015 pricing. OpenAI's May 2026 benchmark run beat Gemini "
    "2 by 4 points on the standard MMLU suite."
)

# The filler-phrase blocklist logic must work correctly even while the real
# FILLER_PHRASES constant is the empty stub T74 will populate -- these are
# the injected phrases used ONLY for testing the counting logic itself.
_TEST_FILLER_PHRASES = ("game-changer", "huge deal", "you won't believe")

FENCED_BRIEF = """Here is the deploy config.

```yaml
service: web
replicas: 3
image: registry.example.com/web:1.4.2
port: 8080
```

Ship it."""

CODE_SLIDE_PRESERVED = {
    "slides": [
        {"type": "hook", "headline": "New deploy config ships", "sub": "Three replicas, pinned image."},
        {
            "type": "code",
            "headline": "Deploy config",
            "lang": "yaml",
            "code": "service: web\nreplicas: 3\nimage: registry.example.com/web:1.4.2\nport: 8080",
        },
        {"type": "sources", "headline": "Sources", "urls": ["https://example.com"]},
    ],
}

CODE_SLIDE_MISSING = {
    "slides": [
        {"type": "hook", "headline": "New deploy config ships", "sub": "Three replicas, pinned image."},
        {"type": "point", "headline": "What changed", "bullets": ["The config was updated."]},
        {"type": "sources", "headline": "Sources", "urls": ["https://example.com"]},
    ],
}


def _item(**kw) -> Item:
    base = dict(id=1, source="channel", status=Status.SYNTHESIZED, brief="", research=[])
    base.update(kw)
    return Item(**base)


# ------------------------------------------------------------- deck_body_text


def test_deck_body_text_excludes_urls_and_code_and_image_fields():
    doc = {
        "slides": [
            {"type": "hook", "headline": "H", "sub": "S"},
            {"type": "code", "headline": "C", "lang": "json", "code": "SECRETCODE123"},
            {"type": "sources", "headline": "Sources", "urls": ["https://verboten.example/x"]},
        ]
    }
    text = deck_body_text(doc)
    assert "SECRETCODE123" not in text
    assert "verboten" not in text
    assert "H" in text and "S" in text


def test_deck_body_text_handles_empty_deck():
    assert deck_body_text({}) == ""
    assert deck_body_text({"slides": []}) == ""


def test_deck_body_text_handles_a_non_dict_doc():
    # `score_substance` gets called on a fresh compose result (T75) with no
    # `_deck_from_row`-style shield -- None, a bare list, or a string doc
    # must degrade to "no slides", not raise.
    assert deck_body_text(None) == ""
    assert deck_body_text([]) == ""
    assert deck_body_text("not a dict") == ""


def test_slide_text_ignores_a_non_list_bullets_field():
    # A malformed completion can hand "bullets" a plain string instead of a
    # list. `if bullets:` alone is truthy for "abc" and used to iterate its
    # *characters* ("a", "b", "c") as if they were bullet fragments -- rows/
    # steps/tiles/series all already guard with isinstance; bullets did not.
    doc = {"slides": [{"type": "point", "headline": "h", "bullets": "abc"}]}
    assert deck_body_text(doc) == "h."


def test_metrics_do_not_crash_on_malformed_slides():
    # --rescore reads rows[].raw for a failed row -- the model's last
    # completion before any validation touched it. A free model can return a
    # garbage shape; nothing here may raise on it.
    malformed_docs = [
        {"slides": ["not a dict", 42, None]},
        {"slides": "not even a list"},
        {"slides": None},
        {},
    ]
    for doc in malformed_docs:
        assert deck_body_text(doc) == ""
        assert numeric_density(doc) == 0.0
        assert proper_noun_density(doc) == 0.0
        assert bare_bullet_ratio(doc) == 0.0
        assert hook_headline(doc) == ""
        assert code_preserved(doc, "```py\nprint(1)\n```") is False


# ------------------------------------------------------------- numeric_density


def test_numeric_density_orders_good_above_bad():
    assert numeric_density(GOOD_DECK) > numeric_density(BAD_DECK)
    assert numeric_density(BAD_DECK) == 0.0


def test_numeric_density_empty_deck_is_zero():
    assert numeric_density({"slides": []}) == 0.0


def test_numeric_density_hand_computed_value():
    # 10 tokens (Model hits 92 on the test with 4 points gain), 2 carry a
    # digit ("92", "4") -> 2/10 * 100 = 20.0. An ordering-only test cannot
    # tell "counts digits correctly" from "counts something vaguely
    # correlated with digits" -- this pins the actual arithmetic.
    doc = {"slides": [{"type": "hook", "headline": "Model hits 92% on the test with 4 points gain"}]}
    assert numeric_density(doc) == 20.0


# --------------------------------------------------------- proper_noun_density


def test_proper_noun_density_orders_good_above_bad():
    assert proper_noun_density(GOOD_DECK) > proper_noun_density(BAD_DECK)


def test_proper_noun_density_ignores_sentence_initial_capitals():
    # Every sentence here starts capitalised; none is a named entity, and
    # none is mid-sentence, so the count must stay at zero.
    doc = {"slides": [{"type": "hook", "headline": "h", "sub": "This is fine. That is fine too."}]}
    assert proper_noun_density(doc) == 0.0


def test_proper_noun_density_counts_mid_sentence_capitals_and_acronyms():
    # 9 tokens total; "Anthropic" (interior capital) and "API" (acronym) hit
    # -> 2/9 * 100 = 22.22. A bare `> 0.0` check cannot distinguish "counts
    # the right two tokens" from "counts some other, wrong token" -- pin the
    # value, not just the sign.
    doc = {"slides": [{"type": "hook", "headline": "h",
                        "sub": "The team at Anthropic shipped an API update."}]}
    assert proper_noun_density(doc) == 22.22


# ------------------------------------------------------------- grounding_ratio


def test_grounding_ratio_orders_sourced_above_generic():
    good = grounding_ratio(GOOD_DECK, SOURCE_TEXT)
    bad = grounding_ratio(BAD_DECK, SOURCE_TEXT)
    assert good > bad
    assert bad == 0.0
    assert 0.0 < good <= 1.0


def test_grounding_ratio_short_deck_or_source_is_zero_not_a_crash():
    tiny = {"slides": [{"type": "hook", "headline": "Hi", "sub": "ok"}]}
    assert grounding_ratio(tiny, "") == 0.0
    assert grounding_ratio({"slides": []}, SOURCE_TEXT) == 0.0


def test_grounding_ratio_does_not_invent_grams_at_fragment_joins():
    # Two 3-word fragments whose OWN 3-grams appear nowhere in the source.
    # The old joined-blob implementation stitched the last word of fragment 1
    # to the first two of fragment 2 (and vice versa) into two 3-grams that
    # exist in neither the deck's actual prose nor anywhere a reader sees --
    # and this source is built to contain exactly those manufactured joins,
    # so the join-artifact bug scores this deck 0.5 "grounded" when neither
    # fragment the model actually wrote shares one real phrase with the
    # source. Correct answer: 0.0.
    doc = {"slides": [
        {"type": "hook", "headline": "cats run fast"},
        {"type": "takeaway", "headline": "dogs jump high"},
    ]}
    source = "run fast dogs jump"
    assert grounding_ratio(doc, source) == 0.0


def test_grounding_ratio_verbatim_source_fragments_score_one():
    # Every fragment in this deck is a literal, contiguous substring of the
    # source -- the "copied verbatim from the brief" case. Every 3-gram the
    # deck side produces therefore exists in the source side too, so the
    # ratio must be a full 1.0, not deflated by how the fragments happen to
    # be split across slide fields.
    source = (
        "OpenAI reported the GPT-5 model reached 92 percent on the MMLU "
        "benchmark. The company said this result beat Gemini 2 by 4 points "
        "running the same suite."
    )
    doc = {"slides": [
        {"type": "hook",
         "headline": "OpenAI reported the GPT-5 model reached 92 percent on the MMLU benchmark."},
        {"type": "takeaway",
         "sub": "The company said this result beat Gemini 2 by 4 points running the same suite."},
    ]}
    assert grounding_ratio(doc, source) == 1.0


def test_grounding_ratio_hand_computed_mid_range_value():
    # source 3-grams: (the,quick,brown) (quick,brown,fox) (brown,fox,jumps)
    # (fox,jumps,over) (jumps,over,the) (over,the,lazy) (the,lazy,dog) = 7.
    # deck 3-grams: (a,quick,brown) (quick,brown,fox)* (brown,fox,runs)
    # (fox,runs,over) (runs,over,the) (over,the,lazy)* (the,lazy,cat) = 7,
    # of which 2 (marked *) also occur in source -> 2/7 = 0.2857.
    source = "the quick brown fox jumps over the lazy dog"
    doc = {"slides": [{"type": "hook", "headline": "a quick brown fox runs over the lazy cat"}]}
    assert grounding_ratio(doc, source) == 0.2857


def test_grounding_ratio_folds_curly_apostrophe_to_ascii():
    # "Apple's" (ASCII, typical of a brief) and "Apple’s" (curly, typical
    # of LLM output) must tokenise identically. Without folding, the curly
    # apostrophe splits into two tokens ("apple", "s") that misalign every
    # downstream 3-gram against the ASCII-apostrophe source, deflating an
    # otherwise word-for-word-identical sentence to 0.4 instead of 1.0.
    source = "Apple's new chip beats last year's model by 30 percent"
    doc_ascii = {"slides": [
        {"type": "hook", "headline": "Apple's new chip beats last year's model by 30 percent"},
    ]}
    doc_curly = {"slides": [
        {"type": "hook", "headline": "Apple’s new chip beats last year’s model by 30 percent"},
    ]}
    assert grounding_ratio(doc_ascii, source) == 1.0
    assert grounding_ratio(doc_curly, source) == 1.0


# ------------------------------------------------------------- hook_similarity


def test_hook_similarity_orders_repetitive_run_above_varied_run():
    good_hooks = [
        "GPT-5 doubles MMLU accuracy to 92%",
        "Anthropic ships Claude Opus 5 with 500K context",
        "Google's Gemini 3 adds native video reasoning",
    ]
    bad_hooks = [
        "Big news in AI today",
        "Big news in tech today",
        "Big news for developers today",
    ]
    assert hook_similarity(bad_hooks) > hook_similarity(good_hooks)


def test_hook_similarity_needs_at_least_two_hooks():
    assert hook_similarity([]) == 0.0
    assert hook_similarity(["only one hook"]) == 0.0


def test_hook_similarity_identical_hooks_is_one():
    assert hook_similarity(["same headline exactly", "same headline exactly"]) == 1.0


def test_hook_similarity_hand_computed_mid_range_value():
    # Jaccard(h1,h2) = |{alpha,beta}| / |{alpha,beta,gamma,delta}| = 2/4 = 0.5
    # Jaccard(h1,h3) = Jaccard(h2,h3) = 0/6 = 0.0
    # best-per-hook: h1=0.5, h2=0.5, h3=0.0 -> mean = 1.0/3 = 0.3333.
    # An ordering-only check cannot tell this from any other mid-range
    # number a broken Jaccard implementation might happen to produce.
    hooks = ["alpha beta gamma", "alpha beta delta", "epsilon zeta eta"]
    assert hook_similarity(hooks) == 0.3333


# --------------------------------------------------------------- filler_count


def test_filler_count_orders_good_above_bad_with_injected_phrases():
    good = filler_count(GOOD_DECK, _TEST_FILLER_PHRASES)
    bad = filler_count(BAD_DECK, _TEST_FILLER_PHRASES)
    assert bad > good
    assert good == 0


def test_filler_count_explicit_empty_override_is_not_replaced_by_the_global():
    # An explicit `()` is a real, distinct request ("count against nothing"),
    # not "no argument was given" -- only the latter should fall back to the
    # live FILLER_PHRASES. `if not phrases: phrases = FILLER_PHRASES` looks
    # equivalent to `if phrases is None:` but silently collapses these two
    # cases: BAD_DECK contains a real harvested phrase ("why it matters"), so
    # an explicit `()` override wrongly falling back to the global would
    # report a nonzero count here instead of the requested zero.
    assert filler_count(BAD_DECK, ()) == 0


def test_filler_phrases_are_harvested_not_a_stub():
    # T74: no longer the empty stub. >= 25 real, cited, non-empty, unique
    # phrases -- see the FILLER_PHRASES docstring and the T74 task handoff
    # for the full phrase -> deck -> field citation table.
    assert len(FILLER_PHRASES) >= 25
    assert len(set(FILLER_PHRASES)) == len(FILLER_PHRASES)
    assert all(isinstance(p, str) and p for p in FILLER_PHRASES)


def test_filler_count_finds_real_harvested_phrases_in_a_generic_deck():
    # BAD_DECK was hand-built for this suite before T74 harvested anything,
    # yet its "Why it matters" point headline already matches a real T74
    # phrase (golden_fewshot id=10/5/20 takeaway) -- a generic-adjective deck
    # written by hand independently converges on the same stock phrasing real
    # free-model output does, which is exactly the correlation FILLER_PHRASES
    # exists to catch. GOOD_DECK, specific throughout, matches none.
    assert filler_count(GOOD_DECK) == 0
    assert filler_count(BAD_DECK) >= 1


def test_filler_count_three_harvested_phrases_score_strictly_worse():
    # AC2: a deck containing three real, harvested FILLER_PHRASES entries
    # must score strictly worse (higher filler_count, lower composite) than
    # the identical deck without them.
    clean = copy.deepcopy(GOOD_DECK)
    filler_laden = copy.deepcopy(GOOD_DECK)
    filler_laden["slides"][0]["sub"] += (
        " Follow for more. Daily tech, explained. Why it matters."
    )
    assert filler_count(clean) == 0
    assert filler_count(filler_laden) == 3

    item = _item(brief=SOURCE_TEXT)
    clean_score = substance_composite(score_substance(item, clean))
    filler_score = substance_composite(score_substance(item, filler_laden))
    assert filler_score < clean_score


def test_filler_count_reads_the_live_module_global_not_an_import_time_default(
    monkeypatch,
):
    # T93/D26: the old `phrases: tuple = FILLER_PHRASES` default bound the
    # (then-empty) tuple AT IMPORT TIME, so a runtime reassignment of
    # quality.FILLER_PHRASES was invisible to every call relying on the
    # default -- score_substance kept reporting filler_count=0 from the stale
    # binding while substance_composite (which already re-reads the global
    # directly) started crediting the term a flat 1.0: measured 12.5 -> 22.22
    # on one deck. This is exactly the reassignment scenario, reproduced:
    # RED under the old default-binding implementation, GREEN under the fix.
    monkeypatch.setattr(quality, "FILLER_PHRASES", ())
    assert filler_count(BAD_DECK) == 0

    monkeypatch.setattr(
        quality, "FILLER_PHRASES", ("game-changer", "huge deal", "you won't believe")
    )
    assert filler_count(BAD_DECK) == 3


# ---------------------------------------------------------- bare_bullet_ratio


def test_bare_bullet_ratio_orders_good_below_bad():
    assert bare_bullet_ratio(GOOD_DECK) < bare_bullet_ratio(BAD_DECK)
    assert bare_bullet_ratio(BAD_DECK) == 1.0
    assert bare_bullet_ratio(GOOD_DECK) == 0.0


def test_bare_bullet_ratio_ignores_structural_bookends():
    # hook/takeaway/sources/links/follow/repo carry no bare-vs-rich verdict.
    doc = {"slides": [{"type": t, "headline": "h"} for t in STRUCTURAL_SLIDE_TYPES]}
    assert bare_bullet_ratio(doc) == 0.0


def test_bare_bullet_ratio_all_rich_content_is_zero():
    doc = {"slides": [{"type": "hook", "headline": "h"}, {"type": "flow", "headline": "f"},
                       {"type": "compare", "headline": "c"}]}
    assert bare_bullet_ratio(doc) == 0.0
    assert RICH_SLIDE_TYPES == {"code", "flow", "compare", "kpi", "chart", "quote"}


# ------------------------------------------------------------- code_preserved


def test_code_preserved_true_when_fenced_block_survives():
    assert code_preserved(CODE_SLIDE_PRESERVED, FENCED_BRIEF) is True


def test_code_preserved_false_when_no_code_slide():
    assert code_preserved(CODE_SLIDE_MISSING, FENCED_BRIEF) is False


def test_code_preserved_is_none_when_brief_has_no_fence():
    assert code_preserved(CODE_SLIDE_MISSING, "no fenced block here, just prose.") is None
    assert code_preserved(CODE_SLIDE_MISSING, None) is None


# ------------------------------------------------------- source_text_from_item


def test_source_text_from_item_combines_brief_and_notes():
    item = _item(brief="THE BRIEF", research=[
        {"claim": "claim one", "detail": "detail one"},
        {"claim": "claim two", "detail": ""},
    ])
    text = source_text_from_item(item)
    assert "THE BRIEF" in text
    assert "claim one" in text and "detail one" in text
    assert "claim two" in text


def test_source_text_from_item_handles_empty_item():
    item = _item(brief=None, research=[])
    assert source_text_from_item(item) == ""


# ----------------------------------------------------------- hook_headline


def test_hook_headline_finds_the_hook_slide():
    assert hook_headline(GOOD_DECK) == "GPT-5 doubles MMLU accuracy to 92%"
    assert hook_headline({"slides": []}) == ""


# -------------------------------------------------- score_substance / composite


def test_score_substance_returns_all_seven_keys_minus_run_level_hook():
    item = _item(brief=SOURCE_TEXT)
    metrics = score_substance(item, GOOD_DECK)
    for key in ("numeric_density", "proper_noun_density", "grounding_ratio",
                "filler_count", "bare_bullet_ratio", "code_preserved", "word_count"):
        assert key in metrics


def test_substance_composite_orders_good_deck_above_bad_deck():
    item = _item(brief=SOURCE_TEXT)
    good_metrics = score_substance(item, GOOD_DECK)
    bad_metrics = score_substance(item, BAD_DECK)
    good_score = substance_composite(good_metrics, hook_similarity=0.0)
    bad_score = substance_composite(bad_metrics, hook_similarity=0.0)
    assert good_score > bad_score
    assert 0.0 <= bad_score <= 100.0
    assert 0.0 <= good_score <= 100.0


def test_substance_composite_penalises_high_hook_similarity():
    item = _item(brief=SOURCE_TEXT)
    metrics = score_substance(item, GOOD_DECK)
    low_repeat = substance_composite(metrics, hook_similarity=0.0)
    high_repeat = substance_composite(metrics, hook_similarity=1.0)
    assert high_repeat < low_repeat


def test_substance_composite_treats_na_code_as_excluded_not_zero():
    # Identical metrics except code_preserved: True / False / None. None must
    # drop the term out of the weighted average (renormalised), landing
    # strictly between True and False -- not collapse to False's score,
    # which is what a "missing key defaults to 0" bug would produce.
    base = {
        "numeric_density": 5.0, "proper_noun_density": 10.0, "grounding_ratio": 0.5,
        "filler_count": 0, "bare_bullet_ratio": 0.2, "word_count": 100,
    }
    score_true = substance_composite({**base, "code_preserved": True})
    score_false = substance_composite({**base, "code_preserved": False})
    score_na = substance_composite({**base, "code_preserved": None})
    assert score_true > score_na > score_false


def test_composite_weights_sum_to_one():
    assert abs(sum(COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9


def test_substance_composite_excludes_filler_term_when_phrases_empty(monkeypatch):
    # T74 populated FILLER_PHRASES for real, but the exclude-and-renormalise
    # treatment (same as code_preserved=None) must keep working for the
    # pre-T74 empty-blocklist state -- reassigning the module global back to
    # `()` is exactly the scenario T93 exists to make safe, and is the only
    # way left to exercise this branch now that the real default is populated.
    monkeypatch.setattr(quality, "FILLER_PHRASES", ())
    metrics = {
        "numeric_density": 5.0, "proper_noun_density": 10.0, "grounding_ratio": 0.5,
        "filler_count": 0, "bare_bullet_ratio": 0.2, "word_count": 100,
        "code_preserved": None,
    }
    # Hand-computed: five applicable terms (grounding, numeric, bare_bullet,
    # proper_noun, hook_similarity=0.0 default) over their combined weight
    # 0.80 (code_preserved AND filler_count both excluded here) -- see the
    # COMPOSITE_WEIGHTS "effective weights" docstring note.
    assert substance_composite(metrics) == 66.3


# --------------------------------------------------------- metric_set_version


def test_metric_set_version_is_stamped_on_every_scored_deck():
    # D15/D23: score_substance stamps the active metric-set version onto
    # every metrics dict so a saved run (or a reader comparing two numbers)
    # can tell which composite era a figure belongs to without re-deriving it
    # from FILLER_PHRASES' current length.
    item = _item(brief=SOURCE_TEXT)
    metrics = score_substance(item, GOOD_DECK)
    assert metrics["metric_set_version"] == METRIC_SET_VERSION
    # v2: T74 populated FILLER_PHRASES, crossing the D23 boundary from v1.
    assert METRIC_SET_VERSION == 2


def test_active_component_weights_includes_filler_now_the_blocklist_is_real():
    # code_preserved's applicability is per-deck, so active_component_weights
    # always carries it at its documented weight (see its own docstring) --
    # only filler_count's global applicability is filtered here. All 7 terms
    # present -> full 1.0 (COMPOSITE_WEIGHTS already sums to 1.0).
    weights = active_component_weights()
    assert "filler_count" in weights
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_active_component_weights_excludes_filler_when_phrases_empty(monkeypatch):
    monkeypatch.setattr(quality, "FILLER_PHRASES", ())
    weights = active_component_weights()
    assert "filler_count" not in weights
    assert abs(sum(weights.values()) - 0.90) < 1e-9  # 6 remaining terms


def test_active_component_weights_does_not_mutate_the_module_table():
    # A caller must not be able to corrupt COMPOSITE_WEIGHTS by mutating the
    # dict active_component_weights() handed back.
    weights = active_component_weights()
    weights["grounding_ratio"] = 999.0
    assert COMPOSITE_WEIGHTS["grounding_ratio"] == 0.30


# ------------------------------------------------- no-model-judge invariant


def test_quality_module_imports_only_re_and_unicodedata():
    # D25 / coding-standards rule 6: the no-model-judge invariant is enforced
    # STRUCTURALLY, not by convention -- this module may import `re` and
    # `unicodedata` (plus `from __future__ import annotations`) and nothing
    # else. Parses the actual source file's import statements rather than
    # trusting the docstring's promise.
    source = Path(inspect.getfile(quality)).read_text()
    tree = ast.parse(source)
    allowed_modules = {"re", "unicodedata", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed_modules, f"unexpected import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed_modules, f"unexpected import: {node.module}"
