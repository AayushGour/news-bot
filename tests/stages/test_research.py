"""Research stage tests.

The centrepiece is the regression test for the PoC's worst defect: a
mouse-cursor download site was cited as the source for a statement by Cursor's
leadership. That must never recur.
"""

import pytest

from pipeline.errors import Retryable
from pipeline.models import Item, Status
from pipeline.stages.research import disambiguate, plan_queries, research

CURSOR_NEWS = (
    "OpenAI will block Cursor users from accessing OpenAI models within three "
    "months. Cursor says the models are about five percent of its user traffic."
)

# The actual domains that polluted the PoC run.
POISONED = [
    {"url": "https://custom-cursor.example/anime", "title": "Anime cursors", "content": ""},
    {"url": "https://rw-designer.example/cursor-set", "title": "Cursor sets", "content": ""},
    {"url": "https://stackoverflow.example/q/sql-cursor", "title": "SQL cursor", "content": ""},
]


def _item():
    return Item(id=1, source="channel", status=Status.EXTRACTED, raw_text=CURSOR_NEWS)


def _settings(settings, concurrency=1, docs=3):
    from dataclasses import replace

    return replace(settings, research_concurrency=concurrency, docs_per_query=docs)


# ------------------------------------------------------------ disambiguation


def test_disambiguate_appends_context_to_a_bare_query():
    """A bare ambiguous token is exactly what caused the collision."""
    out = disambiguate(["cursor openai block"], "Cursor", "Anysphere AI coding editor")
    assert out == ["cursor openai block Anysphere AI coding editor"]


def test_disambiguate_leaves_already_qualified_queries_alone():
    out = disambiguate(
        ["Anysphere Cursor funding round"], "Cursor", "Anysphere AI coding editor"
    )
    assert out == ["Anysphere Cursor funding round"]


def test_disambiguate_drops_empty_queries():
    assert disambiguate(["", "  ", "real query"], "E", "ctx") == ["real query ctx"]


def test_disambiguate_without_context_is_a_passthrough():
    assert disambiguate(["a", "b"], "E", "") == ["a", "b"]


async def test_planner_prompt_demands_disambiguation(fake_llm):
    fake_llm.queue({
        "entity": "Cursor",
        "entity_context": "Anysphere AI coding editor",
        "queries": ["cursor openai"],
    })
    queries, subject = await plan_queries(_item(), fake_llm)

    assert queries == ["cursor openai Anysphere AI coding editor"]
    assert subject == "Cursor (Anysphere AI coding editor)"
    assert "disambiguating context" in fake_llm.calls[0].system


# ----------------------------------------------------------- relevance gate


async def test_relevance_gate_rejects_keyword_collision_sources(
    fake_http, fake_llm, settings
):
    """Regression: the PoC cited custom-cursor.com — a mouse-cursor download
    site — as the source for a statement by Cursor's leadership."""
    fake_llm.queue({
        "entity": "Cursor",
        "entity_context": "Anysphere AI coding editor",
        "queries": ["cursor openai block Anysphere AI coding editor"],
    })
    fake_http.respond_for("/search", {"results": POISONED})
    fake_http.respond(200, "<html><body><p>Download free anime mouse cursors "
                           "for your desktop. Custom cursor packs.</p></body></html>")
    # Every fetched page is judged irrelevant.
    fake_llm.queue_each([{"relevant": False, "why": "mouse cursors, not Anysphere"}] * 3)

    with pytest.raises(Retryable):
        await research(_item(), fake_llm, fake_http, _settings(settings))

    # And crucially: no research note was ever produced from a poisoned source.
    note_calls = [c for c in fake_llm.calls if c.schema and "claim" in
                  c.schema.get("properties", {})]
    assert note_calls == []


async def test_relevant_sources_produce_a_note_with_attribution(
    fake_http, fake_llm, settings
):
    fake_llm.queue({
        "entity": "Cursor", "entity_context": "Anysphere AI coding editor",
        "queries": ["q1 Anysphere AI coding editor", "q2 Anysphere AI coding editor"],
    })
    fake_http.respond_for("/search", {"results": [
        {"url": "https://teslarati.example/a", "title": "t", "content": "c"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("OpenAI cut off Cursor. " * 40)
                           + "</article></body></html>")
    for _ in range(2):
        fake_llm.queue({"relevant": True, "why": "about Anysphere"})
        fake_llm.queue({"claim": "OpenAI blocked Cursor",
                        "detail": "within three months", "confidence": "high"})

    out = await research(_item(), fake_llm, fake_http, _settings(settings))

    assert len(out["research"]) == 2
    assert out["research"][0]["sources"] == ["https://teslarati.example/a"]
    assert out["research"][0]["claim"] == "OpenAI blocked Cursor"


async def test_relevance_gate_error_rejects_rather_than_admits(
    fake_http, fake_llm, settings
):
    """A gate that fails open would defeat its own purpose."""
    fake_llm.queue({"entity": "E", "entity_context": "ctx", "queries": ["q1 ctx"]})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://a.example/1", "title": "t", "content": "some content"},
    ]})
    fake_http.respond(200, "<html><body><p>text</p></body></html>")
    # The gate call raises rather than returning a verdict.
    fake_llm.queue(RuntimeError("gate exploded"))

    with pytest.raises(Retryable):
        await research(_item(), fake_llm, fake_http, _settings(settings))


# ------------------------------------------------------------- fan-out policy


async def test_single_researcher_failure_is_survivable(fake_http, fake_llm, settings):
    """Spec §10: one researcher dying must not fail the item."""
    fake_llm.queue({"entity": "E", "entity_context": "ctx",
                    "queries": ["q1 ctx", "q2 ctx", "q3 ctx"]})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://a.example/1", "title": "t", "content": "body text here"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("Body. " * 60) + "</article></body></html>")

    for i in range(3):
        fake_llm.queue({"relevant": True, "why": "yes"})
        if i == 1:
            fake_llm.queue(RuntimeError("note call blew up"))
        else:
            fake_llm.queue({"claim": f"c{i}", "detail": "d", "confidence": "high"})

    out = await research(_item(), fake_llm, fake_http, _settings(settings))
    assert len(out["research"]) == 2


async def test_fewer_than_two_notes_raises_retryable(fake_http, fake_llm, settings):
    fake_llm.queue({"entity": "E", "entity_context": "ctx", "queries": ["q1 ctx", "q2 ctx"]})
    fake_http.respond_for("/search", {"results": []})

    with pytest.raises(Retryable, match="need at least"):
        await research(_item(), fake_llm, fake_http, _settings(settings))


async def test_no_queries_raises_retryable(fake_http, fake_llm, settings):
    fake_llm.queue({"entity": "E", "entity_context": "ctx", "queries": []})
    with pytest.raises(Retryable, match="no usable queries"):
        await research(_item(), fake_llm, fake_http, _settings(settings))


# ------------------------------------- planner must not invent an expansion


def test_planner_forbidden_from_guessing_acronym_expansions():
    """Regression: 'Explain okf' produced searches for an 'OKF AI research
    lab', the 'Open Knowledge Foundation' and an 'Open Knowledge Framework' —
    three invented entities. The relevance gate then discarded correct pages
    about the real Open Knowledge Format for not matching the guess. Two
    mechanisms compounding into confidently researching the wrong thing."""
    from pipeline.stages.research import PLAN_SYSTEM

    collapsed = " ".join(PLAN_SYSTEM.split())
    assert "must NOT guess an expansion" in collapsed
    assert "Leave entity_context EMPTY" in collapsed
    assert "Never expand an acronym from your own knowledge" in collapsed


def test_relevance_gate_judges_on_the_name_not_the_guess():
    """The gate must not enforce the planner's hallucination."""
    from pipeline.stages.research import RELEVANCE_SYSTEM

    collapsed = " ".join(RELEVANCE_SYSTEM.split())
    assert "Judge against the SUBJECT NAME first" in collapsed
    assert "corrects an assumption in it" in collapsed


async def test_empty_context_leaves_queries_untouched(fake_llm):
    """With no context, queries must go out as written rather than being
    padded with an invented expansion."""
    fake_llm.queue({"entity": "OKF", "entity_context": "",
                    "queries": ["okf format specification", "okf markdown agents"]})
    queries, subject = await plan_queries(_item(), fake_llm)

    assert queries == ["okf format specification", "okf markdown agents"]
    assert subject == "OKF", "no parenthetical when there is nothing to add"


async def test_gate_does_not_treat_ollama_outage_as_irrelevance(
    fake_http, fake_llm, settings
):
    """Regression: Ollama dropped mid-research and the gate's except-all
    returned False, so every source was discarded and logged as 'every source
    rejected as irrelevant'. The sources were modelcontextprotocol.io — exactly
    right. A transient outage became a permanent failure with a message that
    pointed at the wrong subsystem.
    """
    from pipeline.errors import Retryforever

    fake_llm.queue({"entity": "MCP", "entity_context": "", "queries": ["mcp apps spec"]})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://modelcontextprotocol.io/extensions/apps/overview",
         "title": "MCP Apps", "content": "real content about MCP apps"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("MCP apps. " * 60)
                           + "</article></body></html>")
    fake_llm.queue(Retryforever("ollama unreachable"))

    with pytest.raises(Retryforever):
        await research(_item(), fake_llm, fake_http, _settings(settings))


async def test_gate_still_fails_closed_on_a_malformed_verdict(
    fake_http, fake_llm, settings
):
    """A broken gate must not start admitting anything."""
    fake_llm.queue({"entity": "E", "entity_context": "", "queries": ["q1", "q2"]})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://a.example/1", "title": "t", "content": "body"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("Body. " * 60) + "</article></body></html>")
    fake_llm.queue_each([ValueError("garbage verdict")] * 2)

    with pytest.raises(Retryable, match="need at least"):
        await research(_item(), fake_llm, fake_http, _settings(settings))


def test_gate_rejects_a_different_thing_not_partial_coverage():
    """Regression: the gate demanded a page match every element of the subject.
    It rejected an Anthropic/OpenAI ARR analysis for not naming Greg Brockman,
    and an MCP apps explainer for a story about MCP apps. On-topic sources were
    discarded until items failed the two-note minimum.

    The mouse-cursor guard must survive: that is a different thing sharing a
    word, which is what the gate is actually for.
    """
    from pipeline.stages.research import RELEVANCE_SYSTEM

    collapsed = " ".join(RELEVANCE_SYSTEM.split())
    assert "about the same THING as the subject" in collapsed
    assert "EVEN IF it is partial" in collapsed
    assert "does not name every person" in collapsed
    assert "When genuinely uncertain whether it is the same thing, accept" in collapsed
    # the original purpose is not lost
    assert "mouse cursors is not about the company Cursor" in collapsed
    assert "keep out pages about a different subject entirely" in collapsed


def test_planner_targets_primary_sources_for_technical_subjects():
    """Regression: an MCP explainer researched msn.com, forbes.com and
    geeky-gadgets — journalism about the protocol, none of which contains a
    line of protocol JSON. The extraction fix that preserves code blocks could
    not help because no source had any. Documentation shows a format; news
    describes it."""
    from pipeline.stages.research import PLAN_SYSTEM

    collapsed = " ".join(PLAN_SYSTEM.split())
    assert "at least one query MUST target the primary source" in collapsed
    assert "documentation" in collapsed and "specification" in collapsed
    assert "News articles describe a format in prose; documentation shows it" in collapsed
