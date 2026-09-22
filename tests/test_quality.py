"""Every metric must be able to tell a good deck from a generic one.

Two hand-built decks anchor every test: GOOD_DECK is specific, numeric,
sourced, and uses richer slide types; BAD_DECK is the generic-adjective,
all-bullets, same-old-hook deck the user complained about. A metric that
cannot order these two correctly is not measuring what its name claims to.

No LLM calls anywhere in this file — quality.py has none to call.
"""

from __future__ import annotations

from pipeline.models import Item, Status
from pipeline.quality import (
    COMPOSITE_WEIGHTS,
    FILLER_PHRASES,
    RICH_SLIDE_TYPES,
    STRUCTURAL_SLIDE_TYPES,
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


# --------------------------------------------------------- proper_noun_density


def test_proper_noun_density_orders_good_above_bad():
    assert proper_noun_density(GOOD_DECK) > proper_noun_density(BAD_DECK)


def test_proper_noun_density_ignores_sentence_initial_capitals():
    # Every sentence here starts capitalised; none is a named entity, and
    # none is mid-sentence, so the count must stay at zero.
    doc = {"slides": [{"type": "hook", "headline": "h", "sub": "This is fine. That is fine too."}]}
    assert proper_noun_density(doc) == 0.0


def test_proper_noun_density_counts_mid_sentence_capitals_and_acronyms():
    doc = {"slides": [{"type": "hook", "headline": "h",
                        "sub": "The team at Anthropic shipped an API update."}]}
    assert proper_noun_density(doc) > 0.0


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


# --------------------------------------------------------------- filler_count


def test_filler_count_orders_good_above_bad_with_injected_phrases():
    good = filler_count(GOOD_DECK, _TEST_FILLER_PHRASES)
    bad = filler_count(BAD_DECK, _TEST_FILLER_PHRASES)
    assert bad > good
    assert good == 0


def test_filler_count_defaults_to_the_empty_stub_constant():
    # FILLER_PHRASES is the T74 seam: empty until harvested, so the default
    # call must return 0 for every deck today, not raise or guess.
    assert FILLER_PHRASES == ()
    assert filler_count(GOOD_DECK) == 0
    assert filler_count(BAD_DECK) == 0


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
