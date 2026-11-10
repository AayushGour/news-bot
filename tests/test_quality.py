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
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from pipeline import quality
from pipeline.models import Item, Status
from pipeline.quality import (
    COMPOSITE_WEIGHTS,
    FILLER_PHRASES,
    METRIC_SET_VERSION,
    MODEL_WRITTEN_MANDATED_TYPES,
    PIPELINE_AUTHORED_TYPES,
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
    item_echo_ratio,
    numeric_density,
    proper_noun_density,
    score_substance,
    source_text_from_item,
    substance_composite,
)
from pipeline.stages.compose import ensure_closing_slide, ensure_links_slide

# D25: quality.py itself may import only `re`/`unicodedata`, but a test file
# may import both sides of a contract it is pinning (house rule 3) — this
# file derives compose's CTA literals by calling the real repair functions
# rather than re-typing them (see test_filler_phrases_disjoint_from_... below).
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "eval"


def _harvest_module():
    """Load scripts/harvest_filler_phrases.py the same way
    test_eval_compose_rescore.py loads scripts/eval_compose.py -- scripts/
    is not an importable package, and this reuses the harvest's own scan
    (house rule 3: derive, don't restate) instead of re-implementing it here.
    """
    spec = importlib.util.spec_from_file_location(
        "harvest_filler_phrases_under_test",
        ROOT / "scripts" / "harvest_filler_phrases.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

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


def test_deck_body_text_default_exclude_types_excludes_nothing():
    # B3 (reviewer, T74 bounce 1): `exclude_types` defaults to `frozenset()`
    # -- an UNGUARDED regression surface with no test pinning what that
    # default actually means. Measured: flipping the default to
    # PIPELINE_AUTHORED_TYPES ({"links", "sources", "follow"}) left the
    # entire 754-test suite green while avg_substance_composite moved
    # 39.570 -> 40.415 and 42.542 -> 43.398 on the golden sets -- every
    # caller that calls deck_body_text(doc) with no override (numeric_
    # density, proper_noun_density, score_substance's own word_count) would
    # silently narrow scope with zero test noise. This test calls
    # deck_body_text with NO override and asserts text from all three
    # PIPELINE_AUTHORED_TYPES slides is present by default -- only
    # filler_count opts into narrowing scope, by passing exclude_types
    # explicitly (see test_filler_count_excludes_pipeline_authored_slide_type
    # for THAT behaviour, which is correct and must stay type-scoped to that
    # one caller, not become the default every other caller silently gets).
    doc = {"slides": [
        {"type": "links", "headline": "LINKS-MARKER"},
        {"type": "sources", "headline": "SOURCES-MARKER"},
        {"type": "follow", "headline": "FOLLOW-MARKER"},
        {"type": "hook", "headline": "HOOK-MARKER"},
    ]}
    text = deck_body_text(doc)
    for marker in ("LINKS-MARKER", "SOURCES-MARKER", "FOLLOW-MARKER", "HOOK-MARKER"):
        assert marker in text, (
            f"{marker!r} missing from deck_body_text(doc) with no override -- "
            "the default exclude_types is no longer empty"
        )


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


# ------------------------------------------------------------- item_echo_ratio
# T187: reported-only, never a substance_composite term. See
# quality.item_echo_ratio's own docstring for the full D156 rationale.


def test_item_echo_ratio_verbatim_brief_scores_one():
    # AC5: a brief that is a verbatim copy of raw_text -- every brief 3-gram
    # is then also a raw_text 3-gram by construction.
    text = "OpenAI shipped a new model today with a much longer context window"
    assert item_echo_ratio(text, text) == 1.0


def test_item_echo_ratio_no_shared_trigram_scores_zero():
    # AC5: a brief sharing no 3-gram with raw_text.
    brief = "completely unrelated sentence about something else entirely"
    raw_text = "a totally different topic with zero overlapping phrasing here"
    assert item_echo_ratio(brief, raw_text) == 0.0


def test_item_echo_ratio_denominator_is_brief_grams_not_raw_text_grams():
    # A short, fully-echoed brief inside a much longer raw_text: every one
    # of the brief's 3 distinct 3-grams occurs in raw_text (a superset
    # sequence), so the CORRECT ratio -- numerator over len(brief_grams) --
    # is 3/3 = 1.0. A swapped denominator (numerator over len(raw_grams),
    # the exact "brief-grams for item-grams" mutation this task calls out
    # by name) would instead report 3/8 = 0.375 -- a materially different,
    # still-plausible-looking number that silently changes what the metric
    # means while looking like a normal mid-range score. Any test whose
    # brief and raw_text happen to produce the SAME gram count (like the
    # verbatim-copy or hand-computed-symmetric cases) cannot tell these two
    # denominators apart; this one can, by construction.
    brief = "a b c d e"
    raw_text = "a b c d e f g h i j"
    assert item_echo_ratio(brief, raw_text) == 1.0


def test_item_echo_ratio_hand_computed_mid_range_value():
    # brief 3-grams: (the,quick,brown) (quick,brown,fox) (brown,fox,jumps)
    # (fox,jumps,over) (jumps,over,the) (over,the,lazy) (the,lazy,dog) = 7.
    # raw_text 3-grams: (a,quick,brown) (quick,brown,fox)* (brown,fox,runs)
    # (fox,runs,over) (runs,over,the) (over,the,lazy)* (the,lazy,cat) = 7,
    # of which 2 (marked *) also occur in the brief's set -> 2/7 = 0.2857.
    brief = "the quick brown fox jumps over the lazy dog"
    raw_text = "a quick brown fox runs over the lazy cat"
    assert item_echo_ratio(brief, raw_text) == 0.2857


def test_item_echo_ratio_empty_brief_is_zero_not_a_crash():
    # Empty-denominator case (brief has no 3-grams): 0.0, "no evidence of
    # echo either way" -- the same convention grounding_ratio already uses
    # for its own empty-deck-side case -- never a ZeroDivisionError.
    assert item_echo_ratio("", "a perfectly normal raw text with plenty of words") == 0.0
    assert item_echo_ratio(None, "a perfectly normal raw text with plenty of words") == 0.0


def test_item_echo_ratio_empty_raw_text_is_zero_not_a_crash():
    assert item_echo_ratio("a perfectly normal brief with plenty of words", "") == 0.0
    assert item_echo_ratio("a perfectly normal brief with plenty of words", None) == 0.0


def test_item_echo_ratio_brief_shorter_than_n_grams_is_zero():
    # Two words -> zero 3-grams on the brief side, same empty-denominator
    # branch as an empty string, not a different code path.
    assert item_echo_ratio("two words", "two words and then quite a few more of them") == 0.0


def test_item_echo_ratio_raw_text_shorter_than_n_grams_is_zero():
    assert item_echo_ratio("quite a few words on the brief side here", "two words") == 0.0


def test_item_echo_ratio_pure_punctuation_brief_is_zero():
    # _WORD_RE (via _ngrams) tokenises on [A-Za-z0-9], so a brief that is
    # pure punctuation produces zero word tokens and therefore zero 3-grams
    # -- the same empty-denominator branch, not a crash from indexing an
    # empty token list.
    assert item_echo_ratio("... !!! --- ??? ,,,", "some perfectly normal raw text here") == 0.0


def test_item_echo_ratio_non_ascii_and_emoji_in_raw_text():
    # Real channel posts carry emoji and non-ASCII (item 88's begins with
    # one) -- _WORD_RE simply does not match emoji/CJK as word characters,
    # so they are inert padding around the words that DO match, not a crash
    # or a silent corruption of the surrounding tokenisation.
    raw_text = "🚀 OpenAI ships a new model today with a much longer context window 日本語"
    brief = "OpenAI ships a new model today with a much longer context window"
    assert item_echo_ratio(brief, raw_text) == 1.0


def test_item_echo_ratio_folds_curly_apostrophe_to_ascii():
    # Reuses _ngrams unmodified, so the same apostrophe fold that protects
    # grounding_ratio protects this metric too -- "Apple's" (brief) and
    # "Apple’s" (raw_text) must tokenise identically.
    brief = "Apple's new chip beats last year's model by 30 percent"
    raw_text = "Apple’s new chip beats last year’s model by 30 percent"
    assert item_echo_ratio(brief, raw_text) == 1.0


def test_item_echo_ratio_handles_telegram_text_limit_length_raw_text():
    # bot_intake.py's TELEGRAM_TEXT_LIMIT = 4096 chars is the platform
    # ceiling for a single message this field is built from -- must not
    # crash or slow to a crawl at that size.
    raw_text = ("word " * 819)[:4096]
    assert len(raw_text) <= 4096
    brief = "word " * 20
    assert item_echo_ratio(brief, raw_text) == 1.0


def test_item_echo_ratio_absent_from_substance_composite():
    # AC3: reported only. substance_composite must ignore this key entirely
    # even if a caller's metrics dict happens to carry one -- same proof
    # shape as test_substance_composite_ignores_filler_count_regardless_of_...
    base = {
        "numeric_density": 5.0, "proper_noun_density": 10.0, "grounding_ratio": 0.5,
        "filler_count": 0, "bare_bullet_ratio": 0.2, "word_count": 100,
        "code_preserved": None,
    }
    without = substance_composite(base)
    assert substance_composite({**base, "item_echo_ratio": 1.0}) == without
    assert substance_composite({**base, "item_echo_ratio": 0.0}) == without
    assert "item_echo_ratio" not in COMPOSITE_WEIGHTS
    assert "item_echo_ratio" not in active_component_weights()


def test_item_echo_ratio_house_rule_12_frozen_grounding_inputs_unchanged():
    # AC4: adding item_echo_ratio must not touch any of house rule 12's four
    # frozen grounding_ratio inputs. Pinned here against their EXACT,
    # already-established behaviour -- not merely "did not raise" -- so a
    # future edit that quietly reshapes one of these fails this test, not
    # just a downstream grounding_ratio number nobody is watching.
    assert quality._SCALAR_TEXT_FIELDS == (
        "sub", "quote", "attribution", "caption", "name", "left_title", "right_title",
    )
    assert inspect.signature(quality._ngrams).parameters["n"].default == 3
    assert quality._fold_apostrophes("Apple’s") == "Apple's"
    item = _item(brief="B", research=[{"claim": "c", "detail": "d"}])
    assert source_text_from_item(item) == "B c d"


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


def test_filler_phrases_are_ascii_only():
    # D72: NFKC does NOT fold U+2011 (non-breaking hyphen) or U+2019 (curly
    # apostrophe) to their ASCII equivalents, so a non-ASCII entry could sit
    # in this list forever without ever matching real (ASCII) source or
    # brief text. Nothing enforced this before T74's re-scope.
    for phrase in FILLER_PHRASES:
        assert phrase.isascii(), f"{phrase!r} is not ASCII"


def test_filler_phrases_are_pre_normalised():
    # filler_count lowercases its scan text but never touches the phrases
    # themselves -- an entry that is not already .strip().lower() would
    # silently never match (or match too much, if it carries leading/
    # trailing whitespace `str.count` would then require verbatim).
    for phrase in FILLER_PHRASES:
        assert phrase == phrase.strip().lower(), f"{phrase!r} is not .strip().lower()"


def test_filler_phrases_are_at_least_two_words():
    # A single common word ("ideal", "worth") would false-positive on
    # ordinary specific prose; every entry is a multi-word phrase precisely
    # so it reads as boilerplate rather than vocabulary.
    for phrase in FILLER_PHRASES:
        assert len(phrase.split()) >= 2, f"{phrase!r} is fewer than two words"


def test_no_filler_phrase_is_a_substring_of_another():
    # `filler_count` sums `text.count(p)` per phrase -- if phrase A is a
    # substring of phrase B, a single real occurrence of B double-counts (A
    # fires once for the B occurrence, plus once as itself), inflating the
    # count past the number of actual filler occurrences in the deck.
    for a in FILLER_PHRASES:
        for b in FILLER_PHRASES:
            if a != b:
                assert a not in b, f"{a!r} is a substring of {b!r}"


def test_filler_phrases_have_no_duplicates():
    assert len(set(FILLER_PHRASES)) == len(FILLER_PHRASES)


def _run_files_present(data_dir: Path = DATA_DIR) -> tuple[Path, ...]:
    """Thin wrapper over harvest_filler_phrases.run_files_present.

    T149: the predicate itself now has exactly one owner --
    scripts/harvest_filler_phrases.py's own `main()` gates on it too (the
    IDENTICAL "tracked research_golden.json makes a gitignored corpus look
    present" defect T140 fixed here, found one file over). This test module
    is a reader, not a second implementation (house rule 3: derive, don't
    restate) -- takes `data_dir` as a parameter so the tests below can still
    probe the three corpus states hermetically, against a `tmp_path`,
    without ever touching the real gitignored directory.
    """
    harvest = _harvest_module()
    return harvest.run_files_present(eval_dir=data_dir)


@pytest.mark.skipif(
    not _run_files_present(),
    reason=(
        "none of harvest_filler_phrases.RUN_FILES present under "
        f"{DATA_DIR} -- these are gitignored eval_compose.py run artefacts, "
        "generated locally and never committed. (DATA_DIR itself always "
        "exists because data/eval/research_golden.json is tracked for "
        "test_eval_research.py -- that alone must not count as the corpus "
        "being present.)"
    ),
)
def test_every_filler_phrase_has_a_real_non_closing_citation():
    # Replaces the old `len(FILLER_PHRASES) >= 25` stub-check (which pinned a
    # number somebody hoped for and would pass on 25 invented strings) with
    # the actual property that matters: every entry is a literal substring
    # of some real, doc-validated deck's text on a slide the model authored
    # (i.e. outside PIPELINE_AUTHORED_TYPES). Reuses
    # scripts/harvest_filler_phrases.py's own scan (house rule 3: derive,
    # don't restate) rather than re-implementing the walk here. Skips
    # loudly rather than passing vacuously when the corpus is absent, per
    # coding-standards.md's data-gated-test rule.
    harvest = _harvest_module()
    docs = harvest.load_docs()
    # A RUN_FILES entry exists (the skipif above already gated on that) but
    # contains no doc-validated row -- a real, broken corpus, not a missing
    # one. This must fail loudly, never skip and never pass vacuously.
    assert docs, "a RUN_FILES file exists under data/eval/ but no doc-validated deck was found in it"
    fragments = [
        fragment.lower()
        for _, _, doc in docs
        for _, fragment in harvest.non_closing_fragments(doc)
    ]
    for phrase in FILLER_PHRASES:
        assert any(phrase in f for f in fragments), (
            f"{phrase!r} has no citation in any doc-validated non-closing fragment"
        )


# T140: `_run_files_present` is what the skipif above actually gates on.
# These three hermetic tests pin the exact three corpus states the old
# `DATA_DIR.exists()` guard collapsed -- none of them touch the real
# gitignored data/eval/, so they run on every clone, always.


def test_run_files_present_is_empty_when_the_data_dir_does_not_exist_at_all(tmp_path):
    # State 2a: the very first run, before eval_compose.py has ever been
    # invoked -- data/eval/ itself does not exist yet.
    assert _run_files_present(tmp_path / "does-not-exist-yet") == ()


def test_run_files_present_is_empty_when_only_the_tracked_golden_fixture_exists(tmp_path):
    # State 2b: the actual pristine-clone shape (T140's defect). The
    # directory exists -- data/eval/research_golden.json is git-tracked for
    # test_eval_research.py -- but none of harvest.RUN_FILES do. This is
    # exactly the case `DATA_DIR.exists()` wrongly read as "corpus present".
    (tmp_path / "research_golden.json").write_text("{}")
    assert _run_files_present(tmp_path) == ()


def test_run_files_present_finds_a_real_run_file(tmp_path):
    # State 1/3: any one RUN_FILES entry existing marks the corpus present.
    # Whether it then contains a doc-validated deck is a separate question
    # -- answered by `harvest.load_docs()` plus the test body's `assert docs`,
    # not by this helper.
    harvest = _harvest_module()
    target = tmp_path / f"{harvest.RUN_FILES[0]}.json"
    target.write_text("{}")
    assert _run_files_present(tmp_path) == (target,)


def test_corpus_present_but_no_doc_validated_deck_is_not_mistaken_for_absent(
    tmp_path, monkeypatch
):
    # State 3: a RUN_FILES entry exists (so `_run_files_present` marks the
    # corpus present and the skipif above does NOT fire) but every row is an
    # unvalidated/failed completion -- no "doc" field anywhere. This is a
    # real, broken corpus, and must reach
    # test_every_filler_phrase_has_a_real_non_closing_citation's `assert
    # docs` rather than being confused with state 2 (absent) and skipped.
    harvest = _harvest_module()
    run_file = tmp_path / f"{harvest.RUN_FILES[0]}.json"
    run_file.write_text(json.dumps({"rows": [{"id": 1, "raw": "unvalidated, no doc"}]}))
    monkeypatch.setattr(harvest, "EVAL_DIR", tmp_path)

    assert _run_files_present(tmp_path), "skip guard would wrongly fire on a present corpus"
    assert harvest.load_docs() == [], "a row with no doc field must not synthesize a fake deck"


# T149: the IDENTICAL defect T140 fixed in the skipif above also fooled
# scripts/harvest_filler_phrases.py's own main() -- `not EVAL_DIR.exists() or
# not any(EVAL_DIR.glob("*.json"))` reads the tracked research_golden.json
# fixture as "corpus present" and proceeds to harvest nothing (exit 0,
# "decks scanned: 0", every candidate DROPPED), exactly the silent-empty-table
# failure the module's own docstring (:28-30) says it prevents. These two
# hermetic tests pin main()'s exit code directly -- no re-implementation of
# the guard here (house rule 3: derive, don't restate), just calling the real
# entry point against a monkeypatched EVAL_DIR.


def test_harvest_main_exits_nonzero_naming_run_files_when_dir_absent(
    tmp_path, monkeypatch, capsys
):
    harvest = _harvest_module()
    monkeypatch.setattr(harvest, "EVAL_DIR", tmp_path / "does-not-exist-yet")
    assert harvest.main() == 1
    err = capsys.readouterr().err
    assert "ERROR" in err
    for name in harvest.RUN_FILES:
        assert name in err, f"{name!r} (a RUN_FILES entry) missing from the error message"


def test_harvest_main_exits_nonzero_when_only_the_tracked_golden_fixture_exists(
    tmp_path, monkeypatch, capsys
):
    # The exact pristine-clone shape: the directory exists and holds the
    # git-tracked research_golden.json, but none of RUN_FILES do. This is
    # the case that used to return 0 with an empty table.
    (tmp_path / "research_golden.json").write_text("{}")
    harvest = _harvest_module()
    monkeypatch.setattr(harvest, "EVAL_DIR", tmp_path)
    assert harvest.main() == 1, "a tracked fixture alone must not read as a usable corpus"
    assert "ERROR" in capsys.readouterr().err


def test_filler_count_finds_real_harvested_phrases_in_a_generic_deck():
    # BAD_DECK was hand-built for this suite before T74 harvested anything,
    # yet its "Why it matters" point headline already matches a real T74
    # phrase (golden_fewshot id=10 takeaway) -- a generic-adjective deck
    # written by hand independently converges on the same stock phrasing real
    # free-model output does, which is exactly the correlation FILLER_PHRASES
    # exists to catch. GOOD_DECK, specific throughout, matches none.
    assert filler_count(GOOD_DECK) == 0
    assert filler_count(BAD_DECK) >= 1


def test_filler_count_three_harvested_phrases_score_strictly_worse():
    # AC2 (T74 original): a deck containing three real, harvested
    # FILLER_PHRASES entries must score strictly worse (higher filler_count,
    # lower composite) than the identical deck without them. Placed on the
    # hook's `sub` -- `hook` is a MODEL_WRITTEN_MANDATED_TYPES slide, so it
    # is IN filler_count's post-D73 scan scope (unlike a `follow`/`links`/
    # `sources` slide, which would score 0 regardless of what text sits on
    # it -- see test_filler_count_excludes_pipeline_authored_slide_type).
    clean = copy.deepcopy(GOOD_DECK)
    filler_laden = copy.deepcopy(GOOD_DECK)
    filler_laden["slides"][0]["sub"] += (
        " Why it matters. Worth exploring more. Raises questions still."
    )
    assert filler_count(clean) == 0
    assert filler_count(filler_laden) == 3

    item = _item(brief=SOURCE_TEXT)
    clean_score = substance_composite(score_substance(item, clean))
    filler_score = substance_composite(score_substance(item, filler_laden))
    assert filler_score < clean_score


def test_filler_count_excludes_pipeline_authored_slide_type():
    # AC1: a deck of one follow slide bearing the pipeline's own headline/sub
    # (derived from the real ensure_closing_slide -- house rule 3: derive,
    # don't restate) must score filler_count == 0.
    pipeline_slide = ensure_closing_slide([], is_dm=False, source_urls=[])[0]
    assert filler_count({"slides": [pipeline_slide]}) == 0

    # That alone is not yet a meaningful test of the EXCLUSION -- D77 already
    # removed ensure_closing_slide's own literals from FILLER_PHRASES, so the
    # assertion above would pass trivially even if the type-based exclusion
    # were deleted entirely. Prove the exclusion itself: put a phrase that IS
    # still in FILLER_PHRASES onto that SAME pipeline-authored follow slide
    # -- it must still score 0, because the slide's TYPE is excluded, not
    # because of what text happens to sit on it.
    retained_phrase = FILLER_PHRASES[0]
    on_follow = copy.deepcopy(pipeline_slide)
    on_follow["sub"] = f"{on_follow.get('sub', '')} {retained_phrase}".strip()
    assert filler_count({"slides": [on_follow]}) == 0

    # The SAME retained phrase, moved onto a slide type the model actually
    # writes prose for, must still be caught -- the exclusion is about which
    # slide TYPE wrote the text, not about defanging the blocklist.
    moved = {"slides": [{"type": "point", "headline": "h", "sub": retained_phrase}]}
    assert filler_count(moved) > 0


def test_filler_count_scans_model_written_mandated_types():
    # hook/takeaway/repo are MANDATED slide types but the MODEL writes their
    # prose (D73) -- filler_count must still scan them, unlike the
    # PIPELINE_AUTHORED_TYPES bookends. This is the metric's only real
    # signal (D73's rationale for not excluding these three too).
    # N5 (T74 bounce 1): derive the probe phrase from the live FILLER_PHRASES
    # (same pattern as test_filler_count_excludes_pipeline_authored_slide_type
    # above) rather than hardcoding a literal -- a hardcoded string that
    # happens to currently be in the blocklist goes stale silently if a
    # future re-harvest ever drops it, failing this test for the wrong
    # reason (looks like the exclusion broke; it would really be that the
    # phrase left the list).
    probe_phrase = FILLER_PHRASES[0]
    for slide_type in MODEL_WRITTEN_MANDATED_TYPES:
        doc = {"slides": [
            {"type": slide_type, "headline": "h", "sub": probe_phrase},
        ]}
        assert filler_count(doc) > 0, f"{slide_type} must not be excluded from the scan"


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
    assert RICH_SLIDE_TYPES == {"code", "notes", "flow", "compare", "kpi", "chart", "quote"}


# ----------------------------------------- structural slide type partition


def test_structural_slide_types_is_the_stated_partition():
    # AC2: STRUCTURAL_SLIDE_TYPES is DERIVED as the union of the two named
    # halves (D73), not a third, independently-typed copy of the same six
    # types -- and the two halves are disjoint (a type is pipeline-authored
    # XOR model-written-mandated, never both).
    assert (
        STRUCTURAL_SLIDE_TYPES == PIPELINE_AUTHORED_TYPES | MODEL_WRITTEN_MANDATED_TYPES
    )
    assert PIPELINE_AUTHORED_TYPES.isdisjoint(MODEL_WRITTEN_MANDATED_TYPES)
    assert PIPELINE_AUTHORED_TYPES == {"links", "sources", "follow"}
    assert MODEL_WRITTEN_MANDATED_TYPES == {"hook", "takeaway", "repo"}


def test_filler_phrases_disjoint_from_pipeline_authored_cta_text():
    # D77: the literals ensure_closing_slide/ensure_links_slide write
    # themselves must never appear in FILLER_PHRASES -- after D73 scopes
    # filler_count off PIPELINE_AUTHORED_TYPES slides, an entry that
    # duplicates pipeline text can never fire, and keeping it would misstate
    # what the blocklist measures (D77). DERIVED, not restated (house rule
    # 3): calls the real repair functions on synthetic input rather than
    # re-typing their literals, so this fires if someone edits the CTA text
    # in future. Falsification: re-adding "follow for more" to FILLER_PHRASES
    # turns this red.
    # source_urls must be non-empty for ensure_closing_slide to emit a
    # "sources" slide at all (compose.py:972's `source_urls and not any(...)`
    # guard) -- an empty list takes the falsy branch and only "follow" is
    # ever produced, so PIPELINE_AUTHORED_TYPES' third member never reaches
    # this assertion and a "sources" CTA edit could duplicate a FILLER_PHRASES
    # entry with this test staying green throughout.
    closing = ensure_closing_slide([], is_dm=False, source_urls=["https://a.example"])
    assert any(slide.get("type") == "sources" for slide in closing), (
        "fixture produced no 'sources' slide -- test covers only 2 of 3 "
        "PIPELINE_AUTHORED_TYPES"
    )
    links = ensure_links_slide(
        [], notes=[{"url": "https://a.example"}, {"url": "https://b.example"}]
    )
    fragments = [
        str(fragment)
        for slide in (*closing, *links)
        for fragment in quality._slide_text(slide)
    ]
    assert fragments, "the repair functions produced no text to check against"
    lowered = [f.lower() for f in fragments]
    for phrase in FILLER_PHRASES:
        assert not any(phrase in f for f in lowered), (
            f"{phrase!r} duplicates pipeline-authored CTA text: {fragments}"
        )


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


def test_score_substance_word_count_is_scoped_like_filler_count():
    # N2 (T74 bounce 1): `word_count` feeds `substance_composite`'s
    # `filler_density = filler_count / word_count`, so both sides of that
    # division must count words over the SAME slides. Before this fix,
    # `word_count` counted every slide (including PIPELINE_AUTHORED_TYPES'
    # own CTA text) while filler_count's numerator already excluded it --
    # under-reporting density on every deck with any pipeline-authored CTA
    # text at all. A pipeline-authored slide's words must not appear in the
    # returned word_count.
    item = _item(brief=SOURCE_TEXT)
    doc = {"slides": [
        {"type": "hook", "headline": "one two three four five"},
        {"type": "follow", "headline": "six seven eight nine ten eleven"},
    ]}
    metrics = score_substance(item, doc)
    # Only the hook's 5 words count; the follow slide's 6 do not.
    assert metrics["word_count"] == 5


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


def test_composite_weights_has_no_filler_count_entry_and_sums_to_point_nine():
    # v3 (D74): filler_count left the composite for good -- it is not a term
    # COMPOSITE_WEIGHTS carries at all any more (unlike code_preserved,
    # which stays documented at its weight even though it is N/A on every
    # golden deck). The remaining six terms sum to 0.90, not 1.0.
    assert "filler_count" not in COMPOSITE_WEIGHTS
    assert abs(sum(COMPOSITE_WEIGHTS.values()) - 0.90) < 1e-9


def test_substance_composite_five_active_terms_hand_computed_value():
    # v3: with code_preserved N/A (no fenced block) and filler_count
    # permanently out of the composite, exactly five terms are active
    # (grounding_ratio, numeric_density, bare_bullet_ratio,
    # proper_noun_density, hook_similarity=0.0 default) over their combined
    # weight 0.80 -- the same active set and total weight v1 had, before
    # filler_count ever existed as a candidate term (D22/D69).
    metrics = {
        "numeric_density": 5.0, "proper_noun_density": 10.0, "grounding_ratio": 0.5,
        "filler_count": 0, "bare_bullet_ratio": 0.2, "word_count": 100,
        "code_preserved": None,
    }
    assert substance_composite(metrics) == 66.3


def test_substance_composite_ignores_filler_count_regardless_of_value_or_phrases_state(
    monkeypatch,
):
    # AC10: the EXCLUSION REASON changed (a stated policy via
    # FILLER_COUNT_IN_COMPOSITE, not the incidental "FILLER_PHRASES happens
    # to be empty") but the RENORMALISATION behaviour it feeds must not --
    # the composite is byte-identical no matter what filler_count reports,
    # and no matter what FILLER_PHRASES currently holds.
    base = {
        "numeric_density": 5.0, "proper_noun_density": 10.0, "grounding_ratio": 0.5,
        "bare_bullet_ratio": 0.2, "word_count": 100, "code_preserved": None,
    }
    assert substance_composite({**base, "filler_count": 0}) == 66.3
    assert substance_composite({**base, "filler_count": 1000}) == 66.3

    # N4 (T74 bounce 1): these two monkeypatches are INERT today --
    # substance_composite() takes an already-built `metrics` dict and never
    # reads the module-level FILLER_PHRASES itself (only filler_count(), a
    # different function, does that). Kept as a regression guard anyway: if
    # substance_composite ever grows a direct FILLER_PHRASES read (e.g. to
    # decide the term's applicability itself rather than trusting the caller's
    # `filler_count` value), this proves that read still cannot leak into the
    # composite while FILLER_COUNT_IN_COMPOSITE is False.
    monkeypatch.setattr(quality, "FILLER_PHRASES", ())
    assert substance_composite({**base, "filler_count": 5}) == 66.3

    monkeypatch.setattr(quality, "FILLER_PHRASES", ("a real phrase",))
    assert substance_composite({**base, "filler_count": 5}) == 66.3


# --------------------------------------------------------- metric_set_version


def test_metric_set_version_is_stamped_on_every_scored_deck():
    # D15/D23: score_substance stamps the active metric-set version onto
    # every metrics dict so a saved run (or a reader comparing two numbers)
    # can tell which composite era a figure belongs to without re-deriving it
    # from FILLER_PHRASES' current length.
    item = _item(brief=SOURCE_TEXT)
    metrics = score_substance(item, GOOD_DECK)
    assert metrics["metric_set_version"] == METRIC_SET_VERSION
    # v3: T74's re-scope (D73/D74) permanently removed filler_count from the
    # composite and scoped its scan to model-authored slides only.
    assert METRIC_SET_VERSION == 3


def test_active_component_weights_never_includes_filler_count():
    # v3 (D74): true regardless of FILLER_PHRASES' current state -- filler_count
    # is not a candidate the composite ever renormalises around any more.
    assert "filler_count" not in active_component_weights()
    assert abs(sum(active_component_weights().values()) - 0.90) < 1e-9


def test_active_component_weights_never_includes_filler_count_even_when_phrases_empty(
    monkeypatch,
):
    monkeypatch.setattr(quality, "FILLER_PHRASES", ())
    assert "filler_count" not in active_component_weights()


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
