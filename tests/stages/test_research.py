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


def _item(source="channel"):
    return Item(id=1, source=source, status=Status.EXTRACTED, raw_text=CURSOR_NEWS)


def _dm():
    """The clause gate applies to operator requests only — a channel post is a
    story and asks for nothing."""
    return _item(source="dm")


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
        "image_query": "Cursor editor office",
    })
    queries, subject, image_query, _clauses = await plan_queries(_item(), fake_llm)

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
                    "queries": ["okf format specification", "okf markdown agents"],
                    "image_query": "markdown documentation files"})
    queries, subject, _, _clauses = await plan_queries(_item(), fake_llm)

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


# ------------------------------------- image branch for the hook background


async def test_planner_asks_for_a_visual_subject_not_an_abstraction():
    """A picture search for 'artificial intelligence' returns glowing brains."""
    from pipeline.stages.research import PLAN_SYSTEM

    collapsed = " ".join(PLAN_SYSTEM.split())
    assert "image_query" in collapsed
    assert "picture search, not a text search" in collapsed
    assert "glowing brains" in collapsed


async def test_background_image_is_attached_for_compose(
    fake_http, fake_llm, settings, tmp_path, monkeypatch
):
    """The researched photograph must reach compose the same way an attached
    image does, so the existing rating and index guards apply unchanged."""
    from dataclasses import replace as _replace

    import pipeline.stages.research as research_mod

    fake_llm.queue({"entity": "E", "entity_context": "ctx",
                    "queries": ["q1 ctx", "q2 ctx"],
                    "image_query": "a london street"})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://a.example/1", "title": "t", "content": "body text here"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("Body. " * 60) + "</article></body></html>")
    for _ in range(2):
        fake_llm.queue({"relevant": True, "why": "yes"})
        fake_llm.queue({"claim": "c", "detail": "d", "confidence": "high"})

    found = tmp_path / "bg.jpg"
    found.write_bytes(b"x")

    async def fake_find(query, item, http, s):
        assert query == "a london street"
        return {"path": found, "title": "London street", "host": "upload.wikimedia.org"}

    monkeypatch.setattr(research_mod, "_find_background", fake_find)
    out = await research(_item(), fake_llm, fake_http, _settings(settings))

    images = out["extracted"]["image_descriptions"]
    assert len(images) == 1
    assert images[0]["usable"] == "background"
    assert images[0]["path"] == str(found)
    assert images[0]["researched"] is True


async def test_no_background_found_does_not_affect_the_item(
    fake_http, fake_llm, settings, monkeypatch
):
    """A story with no good picture is still a story."""
    import pipeline.stages.research as research_mod

    fake_llm.queue({"entity": "E", "entity_context": "ctx",
                    "queries": ["q1 ctx", "q2 ctx"], "image_query": "nothing"})
    fake_http.respond_for("/search", {"results": [
        {"url": "https://a.example/1", "title": "t", "content": "body"},
    ]})
    fake_http.respond(200, "<html><body><article>" + ("Body. " * 60) + "</article></body></html>")
    for _ in range(2):
        fake_llm.queue({"relevant": True, "why": "yes"})
        fake_llm.queue({"claim": "c", "detail": "d", "confidence": "high"})

    async def none_found(*a, **k):
        return None

    monkeypatch.setattr(research_mod, "_find_background", none_found)
    out = await research(_item(), fake_llm, fake_http, _settings(settings))

    assert len(out["research"]) == 2
    assert "extracted" not in out, "must not touch extraction when nothing was found"


def test_image_search_prefers_predictably_licensed_sources():
    """A news account republishing an arbitrary web photograph is a real risk.
    Reordering this list is a licensing decision, not a tuning knob."""
    from pipeline.search import PREFERRED_IMAGE_DOMAINS

    assert PREFERRED_IMAGE_DOMAINS[0].endswith("wikimedia.org")
    assert any("openverse" in d for d in PREFERRED_IMAGE_DOMAINS)


async def test_tiny_images_are_rejected(fake_http, tmp_path):
    """A thumbnail scaled to 1080x1350 and blurred looks like a mistake."""
    from pipeline.search import download_image

    fake_http.respond(200, "tiny")
    assert await download_image(fake_http, "https://a.example/x.jpg", tmp_path) is None


# --- clause coverage gate ---------------------------------------------------
#
# Item 45 asked two things and got four notes, three of which said "the
# excerpts do not contain information". Counting notes proved nothing about
# whether either clause was answered.

async def test_a_clause_with_no_support_asks_the_operator(
    fake_llm, fake_http, settings, monkeypatch
):
    from pipeline.conversation import NeedsInput
    import pipeline.stages.research as research_mod

    async def one_note(index, query, subject, item, llm, http, s):
        return {"question": query, "claim": "AI eases burnout org-wide",
                "detail": "Workday research", "confidence": "high",
                "sources": ["https://a.example"]}

    monkeypatch.setattr(research_mod, "_research_one", one_note)
    fake_llm.queue({"entity": "burnout", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["the early signs of burnout", "how AI affects it"]})
    fake_llm.queue({"uncovered": ["the early signs of burnout"]})

    with pytest.raises(NeedsInput) as exc:
        await research(_dm(), fake_llm, fake_http, settings)
    assert "the early signs of burnout" in exc.value.question
    assert exc.value.resume_status == Status.TRIAGED


async def test_full_clause_coverage_proceeds(
    fake_llm, fake_http, settings, monkeypatch
):
    import pipeline.stages.research as research_mod

    async def one_note(index, query, subject, item, llm, http, s):
        return {"question": query, "claim": "a real finding", "detail": "d",
                "confidence": "high", "sources": ["https://a.example"]}

    monkeypatch.setattr(research_mod, "_research_one", one_note)
    fake_llm.queue({"entity": "x", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["one ask"]})
    fake_llm.queue({"uncovered": []})

    out = await research(_dm(), fake_llm, fake_http, settings)
    assert len(out["research"]) == 3
    assert out["clauses"] == ["one ask"]


async def test_null_result_notes_do_not_count_as_support(
    fake_llm, fake_http, settings, monkeypatch
):
    """Three low-confidence "I found nothing" notes must not look like three
    findings to the coverage judge."""
    import pipeline.stages.research as research_mod

    async def null_note(index, query, subject, item, llm, http, s):
        return {"question": query,
                "claim": "The provided web excerpts do not contain information.",
                "detail": "", "confidence": "low", "sources": []}

    monkeypatch.setattr(research_mod, "_research_one", null_note)
    fake_llm.queue({"entity": "x", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["the ask"]})
    fake_llm.queue({"uncovered": []})

    await research(_dm(), fake_llm, fake_http, settings)
    judged = fake_llm.calls[-1].user
    assert "do not contain information" in judged, \
        "with no high-confidence notes the judge still sees what there was"


def test_planner_prompt_demands_a_query_per_clause():
    from pipeline.stages.research import PLAN_SYSTEM

    collapsed = " ".join(PLAN_SYSTEM.split())
    assert "EVERY clause needs at least one query of its own" in collapsed
    assert "the intersection of two topics is far thinner" in collapsed


async def test_low_confidence_notes_are_excluded_when_real_findings_exist(
    fake_llm, fake_http, settings, monkeypatch
):
    """A note reporting "the excerpts do not contain information" is a record
    of failure. Handing it to the judge as evidence is how a clause with no
    sources looks covered."""
    import pipeline.stages.research as research_mod

    async def mixed(index, query, subject, item, llm, http, s):
        if index == 0:
            return {"question": query, "claim": "AI eases burnout org-wide",
                    "detail": "Workday", "confidence": "high",
                    "sources": ["https://a.example"]}
        return {"question": query,
                "claim": "The excerpts do not contain information about signs.",
                "detail": "", "confidence": "low", "sources": []}

    monkeypatch.setattr(research_mod, "_research_one", mixed)
    fake_llm.queue({"entity": "x", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["the ask"]})
    fake_llm.queue({"uncovered": []})

    await research(_dm(), fake_llm, fake_http, settings)
    judged = fake_llm.calls[-1].user
    assert "AI eases burnout" in judged
    assert "do not contain information" not in judged


async def test_a_channel_post_is_never_parked_on_clauses(
    fake_llm, fake_http, settings, monkeypatch
):
    """A news story asks for nothing, so splitting it yields fragments of its
    own prose. One item was parked demanding a source for "r considerably
    harder to dismiss as pure speculation" — a mid-word slice of the article's
    last sentence. Nineteen channel items wedged that way."""
    import pipeline.stages.research as research_mod

    async def one_note(index, query, subject, item, llm, http, s):
        return {"question": query, "claim": "a finding", "detail": "d",
                "confidence": "high", "sources": ["https://a.example"]}

    monkeypatch.setattr(research_mod, "_research_one", one_note)
    fake_llm.queue({"entity": "x", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["r considerably harder to dismiss as pure speculation"]})

    out = await research(_item(source="channel"), fake_llm, fake_http, settings)

    assert out["research"], "the item proceeds"
    assert len(fake_llm.calls) == 1, "no coverage judge call for a channel post"


async def test_parked_work_is_carried_so_an_answer_does_not_re_research(
    fake_llm, fake_http, settings, monkeypatch
):
    """Raising discarded the searches already paid for, and left the parked
    item with an empty clauses column that could not be inspected."""
    from pipeline.conversation import NeedsInput
    import pipeline.stages.research as research_mod

    async def one_note(index, query, subject, item, llm, http, s):
        return {"question": query, "claim": "a finding", "detail": "d",
                "confidence": "high", "sources": ["https://a.example"]}

    monkeypatch.setattr(research_mod, "_research_one", one_note)
    fake_llm.queue({"entity": "x", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["the first ask", "the second ask"]})
    fake_llm.queue({"uncovered": ["the second ask"]})

    with pytest.raises(NeedsInput) as exc:
        await research(_dm(), fake_llm, fake_http, settings)

    assert exc.value.fields["research"], "the notes come with it"
    assert exc.value.fields["clauses"] == ["the first ask", "the second ask"]


async def test_proceed_anyway_skips_the_clause_gate(
    fake_llm, fake_http, settings, monkeypatch
):
    """The operator already saw this question and answered "post what you
    have". Asking again about material that has not changed is the loop this
    flag exists to break."""
    from dataclasses import replace

    import pipeline.stages.research as research_mod

    async def one_note(index, query, subject, item, llm, http, s):
        return {"question": query, "claim": "AI eases burnout org-wide",
                "detail": "Workday research", "confidence": "high",
                "sources": ["https://a.example"]}

    monkeypatch.setattr(research_mod, "_research_one", one_note)
    fake_llm.queue({"entity": "burnout", "entity_context": "",
                    "queries": ["q1", "q2", "q3"], "image_query": "img",
                    "clauses": ["the early signs of burnout", "how AI affects it"]})
    fake_llm.queue({"uncovered": ["the early signs of burnout"]})

    item = replace(_dm(), proceed_anyway=True)
    fields = await research(item, fake_llm, fake_http, settings)
    assert fields["research"], "the notes already gathered must still be used"
