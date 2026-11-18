"""T366: an explainer question or a single link is not a list; research then reads the
right pages and parks (never fails) when it finds too little.

The DMs are the real ones that failed (items 612, 615, 616). The classifier and note
writer are faked (they are models); everything deterministic around them is real.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import pipeline.__main__ as main_mod
from pipeline import search
from pipeline.conversation import NeedsInput, ask, handle_answer
from pipeline.errors import Retryable, Retryforever
from pipeline.models import Item, Status
from pipeline.revision import text_edit as te
from pipeline.revision.model import OutcomeCode
from pipeline.stages import research as R
from pipeline.stages.compose import REQUIRED_BODY_FIELDS
from pipeline.stages.enumerate_items import (
    INTENT_SYSTEM,
    LINKED_PAGE_PREVIEW_CHARS,
    classify,
    count_stated,
    enumerate_items,
    is_link_share,
)

DM_612 = ("What is ncm next concept modeling, how does it work, how's it diffrent "
          "from an llm what are its uses")
DM_615 = "https://github.com/HazyResearch/ncm"
DM_616 = "What is ncm? How does it work? How is it different from llm"
README = ("Next-Concept Modeling (NCM) is a two-stage framework for parallel text "
          "generation. Stage one predicts concepts; stage two decodes tokens.")
CLAUSES = ["what NCM is", "how it works", "how it differs from an LLM", "what it is used for"]
PAGE = "<html><body><article>" + ("NCM predicts concepts first. " * 30) + "</article></body></html>"


def _dm(text=DM_616, **kw):
    base = dict(id=1, source="dm", status=Status.TRIAGED, raw_text=text)
    base.update(kw)
    return Item(**base)


def _verdict(intent, **kw):
    base = {"intent": intent, "count": 8, "subject": "", "queries": [], "why": "w",
            "catalogue": "web", "clauses": CLAUSES}
    base.update(kw)
    return base


def _cfg(settings, **kw):
    return replace(settings, research_concurrency=1, docs_per_query=1, **kw)


def _hit(url):
    return {"url": url, "title": "t", "content": "c"}


# ------------------------------------------------------------- classification


@pytest.mark.parametrize("text", [DM_612, DM_616])
async def test_an_explainer_question_is_explain_and_keeps_its_clauses(fake_llm, text):
    fake_llm.queue(_verdict("explain", subject="Next-Concept Modeling (NCM)"))
    plan = await classify(_dm(text), fake_llm)
    assert (plan["intent"], plan["clauses"]) == ("explain", CLAUSES)


async def test_a_bare_link_can_never_be_a_list_even_if_the_model_says_so(fake_llm):
    fake_llm.queue(_verdict("list", subject="HazyResearch ncm", catalogue="repos"))
    assert (await classify(_dm(DM_615), fake_llm))["intent"] == "explain"


async def test_a_list_request_that_contains_a_link_stays_a_list(fake_llm):
    fake_llm.queue(_verdict("list", subject="vector databases"))
    text = "10 vector database repos like https://github.com/pgvector/pgvector"
    assert (await classify(_dm(text), fake_llm))["intent"] == "list"


async def test_the_classifier_sees_a_capped_preview_of_the_fetched_page(fake_llm):
    fake_llm.queue(_verdict("explain"))
    page = README + "Z" * (LINKED_PAGE_PREVIEW_CHARS + 500)
    await classify(_dm(DM_615, extracted={"url_texts": [{"url": DM_615, "text": page}]}), fake_llm)
    user = fake_llm.calls[0].user
    assert "Next-Concept Modeling (NCM)" in user and "LINKED PAGE" in user
    assert user.count("Z") < LINKED_PAGE_PREVIEW_CHARS


def test_the_prompt_defines_explain_and_keeps_clauses_to_the_request():
    flat = " ".join(INTENT_SYSTEM.split())
    assert "mostly a single link" in flat and "reference text only" in flat


@pytest.mark.parametrize("text,expected", [
    (DM_612, False), (DM_615, False), (DM_616, False),
    ("10 awesome GitHub repositories for interview preparation", True),
    ("give me five papers on retrieval", True),
    ("https://example.com/v2/3 what is this", False),
])
def test_count_stated_only_when_the_request_says_a_number(text, expected):
    assert count_stated(text) is expected


@pytest.mark.parametrize("text,expected", [
    (DM_615, True), ("see https://arxiv.org/abs/2610.14162", True), (DM_612, False),
    ("10 repos like https://github.com/a/b for interview prep", False),
])
def test_is_link_share(text, expected):
    assert is_link_share(text) is expected


# ------------------------------------------------------------------- routing


async def test_enumerate_items_does_not_search_for_an_explainer(fake_llm, fake_http, settings):
    fake_llm.queue(_verdict("explain", subject="NCM"))
    out = await enumerate_items(_dm(), fake_llm, fake_http, settings)
    assert out["intent"] == "explain" and out["clauses"] == CLAUSES
    assert fake_http.calls == []


async def test_the_router_sends_an_explainer_to_research_with_the_hints(
    fake_llm, fake_http, settings, monkeypatch,
):
    seen = {}

    async def fake_research(item, llm, http, settings, hints=None):
        seen["hints"] = hints
        return {"research": [{"claim": "c"}]}

    monkeypatch.setattr(main_mod, "research", fake_research)
    fake_llm.queue(_verdict("explain", subject="Next-Concept Modeling (NCM)"))
    out = await main_mod.research_or_enumerate(_dm(DM_612), fake_llm, fake_http, settings)
    assert seen["hints"]["clauses"] == CLAUSES and seen["hints"]["intent"] == "explain"
    assert out["intent"] == "news" and fake_http.calls == []


async def test_a_real_list_still_takes_the_enumeration_path(fake_llm, fake_http, settings, monkeypatch):
    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("research must not run for a list")

    monkeypatch.setattr(main_mod, "research", boom)
    fake_llm.queue(_verdict("list", count=10, subject="interview prep", queries=["interview prep"],
                            catalogue="repos"))
    fake_http.respond(200, {"results": []})
    with pytest.raises(NeedsInput):
        await main_mod.research_or_enumerate(
            _dm("10 awesome GitHub repositories for interview preparation"),
            fake_llm, fake_http, settings)


async def _thin_list_question(fake_llm, fake_http, settings, text):
    fake_llm.queue(_verdict("list", count=8, subject="interview preparation",
                            queries=["interview preparation"], catalogue="repos"))
    fake_http.respond(200, {"results": [{
        "url": "https://github.com/a/interview-preparation", "title": "x", "popularity": 40,
        "content": "A solid collection of interview preparation material for engineers.",
        "tags": ["x"]}]})
    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_dm(text), fake_llm, fake_http, settings)
    return exc.value.question


async def test_the_shortfall_names_a_count_only_if_the_operator_gave_one(fake_llm, fake_http, settings):
    q = await _thin_list_question(fake_llm, fake_http, settings, "repos for interview preparation")
    assert "I only found 1 solid item" in q and "you asked for" not in q


async def test_the_shortfall_still_names_a_requested_count(fake_llm, fake_http, settings):
    q = await _thin_list_question(fake_llm, fake_http, settings, "8 repos for interview preparation")
    assert "not the 8 you asked for" in q


# --------------------------------------------------------------- query shaping


def test_queries_are_short_deduped_and_start_with_the_bare_subject():
    soup = ["Next-Concept Modeling NCM how it works paper arxiv machine learning AI language model",
            "next concept modeling", "Next-Concept   Modeling", ""]
    out = R.shape_queries(soup, "Next-Concept Modeling")
    assert out[0] == "Next-Concept Modeling"
    assert all(len(q.split()) <= R.MAX_QUERY_WORDS for q in out) and "" not in out
    assert len({R._key(q) for q in out}) == len(out) == 2


def test_the_query_count_is_capped_with_the_bare_query_kept():
    out = R.shape_queries([f"q{i} word" for i in range(10)], "Subject")
    assert len(out) == R.MAX_QUERIES and out[0] == "Subject"


def test_bare_subject_drops_a_parenthetical():
    assert R.bare_subject("Next-Concept Modeling (NCM)") == "Next-Concept Modeling"
    assert R.bare_subject("NCM") == "NCM"


async def test_plan_queries_shapes_an_operator_request_and_keeps_a_named_entitys_context(fake_llm, settings):
    plan = {"entity": "Cursor", "entity_context": "Anysphere AI coding editor",
            "queries": ["cursor openai block", "x " * 20], "image_query": "i", "clauses": []}
    fake_llm.queue(plan)
    queries, *_ = await R.plan_queries(_dm("OpenAI blocks Cursor"), fake_llm, settings, hints={})
    assert queries[0] == "Cursor Anysphere AI coding editor"
    fake_llm.queue(plan)
    again, *_ = await R.plan_queries(_dm("x"), fake_llm, settings)
    assert again == queries


async def test_the_planner_gets_the_clauses_and_keeps_the_classifiers_split(fake_llm, settings):
    item = _dm(DM_615, extracted={"url_texts": [{"url": DM_615, "text": README}]})
    fake_llm.queue({"entity": "Next-Concept Modeling (NCM)", "entity_context": "",
                    "queries": ["NCM vs LLM"], "image_query": "i", "clauses": ["planner clause"]})
    _q, _s, _i, clauses = await R.plan_queries(
        item, fake_llm, settings, hints={"subject": "NCM", "clauses": CLAUSES})
    assert clauses == CLAUSES and all(c in fake_llm.calls[0].user for c in CLAUSES)
    assert "LINKED PAGE" in R.PLAN_SYSTEM


@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/pdf/2610.14162.pdf", "https://arxiv.org/abs/2610.14162"),
    ("https://arxiv.org/pdf/2610.14162v2", "https://arxiv.org/abs/2610.14162v2"),
    ("https://github.com/HazyResearch/ncm", "https://github.com/HazyResearch/ncm"),
    ("https://evilarxiv.org/pdf/1.pdf", "https://evilarxiv.org/pdf/1.pdf"),
])
def test_an_arxiv_pdf_is_read_through_its_abstract_page(url, expected):
    assert search.readable_url(url) == expected


async def test_fetch_text_asks_for_the_abstract_not_the_pdf(monkeypatch):
    seen = []

    async def fake_fetch(http, url, policy):
        seen.append(url)
        return search.Refusal.NOT_TEXT_TYPE

    monkeypatch.setattr(search, "fetch_public", fake_fetch)
    await search.fetch_text(object(), "https://arxiv.org/pdf/2610.14162.pdf")
    assert seen == ["https://arxiv.org/abs/2610.14162"]


def test_primary_sources_outrank_news_but_keep_search_order_otherwise():
    hits = [_hit("https://techcrunch.com/a"), _hit("https://github.com/HazyResearch/ncm"),
            _hit("https://arxiv.org/abs/2610.14162"), _hit("https://notgithub.com/b")]
    assert [h["url"] for h in R.prefer_primary(hits)] == [
        "https://github.com/HazyResearch/ncm", "https://arxiv.org/abs/2610.14162",
        "https://techcrunch.com/a", "https://notgithub.com/b"]


async def test_a_primary_source_below_the_first_page_is_still_read(fake_llm, fake_http, settings):
    hits = [_hit(f"https://news{i}.example/a") for i in range(4)] + [_hit("https://arxiv.org/abs/2610.14162")]
    fake_http.respond_for("/search", {"results": hits})
    fake_http.respond(200, PAGE)
    fake_llm.queue({"relevant": True, "why": "x"})
    fake_llm.queue({"claim": "c", "detail": "d", "confidence": "high", "answers": True})
    note = await R._research_one(0, "q", "NCM", _dm(), fake_llm, fake_http,
                                 _cfg(settings, results_per_query=3))
    assert note["sources"] == ["https://arxiv.org/abs/2610.14162"]


# ------------------------------------------------ the gate, non-answers, primary


async def test_the_relevance_gate_is_given_the_capped_request_to_judge_the_sense(fake_llm, settings):
    fake_llm.queue({"relevant": False, "why": "an association"})
    await R._is_relevant(fake_llm, "NCM", "https://ncmassociates.com/", "body", settings,
                         request="~" * (R.REQUEST_CONTEXT_CHARS + 300))
    user = fake_llm.calls[0].user
    assert "REQUEST" in user and user.count("~") == R.REQUEST_CONTEXT_CHARS
    assert "sense" in " ".join(R.RELEVANCE_SYSTEM.split())


async def test_research_one_passes_the_request_to_the_gate_and_drops_a_non_answer(
    fake_llm, fake_http, settings,
):
    fake_http.respond_for("/search", {"results": [_hit("https://a.example/1")]})
    fake_http.respond(200, PAGE)
    fake_llm.queue({"relevant": True, "why": "x"})
    fake_llm.queue({"claim": "The excerpts do not explain how NCM works.", "detail": "",
                    "confidence": "low", "answers": False})
    assert await R._research_one(0, "q", "NCM", _dm(), fake_llm, fake_http, _cfg(settings)) is None
    assert DM_616 in fake_llm.calls[0].user
    assert "answers" in R.NOTE_SCHEMA["required"]


async def test_a_linked_page_is_a_primary_note_written_from_that_page(fake_llm, fake_http, settings):
    item = _dm(DM_615, extracted={"url_texts": [{"url": DM_615, "text": README}]})
    fake_llm.queue({"entity": "Next-Concept Modeling (NCM)", "entity_context": "", "queries": ["a", "b"],
                    "image_query": "i", "clauses": []})
    fake_llm.queue({"claim": "NCM is a two-stage framework", "detail": "d", "confidence": "high",
                    "answers": True})
    fake_llm.queue({"uncovered": []})
    fake_http.respond_for("/search", {"results": []})
    out = await R.research(item, fake_llm, fake_http, _cfg(settings), hints={"clauses": CLAUSES})
    note = out["research"][0]
    assert note["sources"] == [DM_615]
    assert README in next(c for c in fake_llm.calls if c.schema and "claim" in c.schema["properties"]).user


async def test_a_channel_post_proceeds_on_its_own_linked_article(fake_llm, fake_http, settings):
    """T377: items 739/740 fetched their whole article, the web had no coverage
    of a day-old story yet, and the item failed: the link did not count."""
    item = Item(id=2, source="channel", status=Status.TRIAGED, raw_text="story https://x.example/a",
                extracted={"url_texts": [{"url": "https://x.example/a", "text": README}]})
    fake_llm.queue(_plan())
    fake_llm.queue({"claim": "NCM is a two-stage framework", "detail": "d", "confidence": "high",
                    "answers": True})
    fake_http.respond_for("/search", {"results": []})
    out = await R.research(item, fake_llm, fake_http, _cfg(settings), hints={"subject": "NCM"})
    assert [n["sources"] for n in out["research"]] == [["https://x.example/a"]]


async def test_a_non_answer_primary_page_and_an_unreadable_one_are_skipped(fake_llm, settings, caplog):
    item = _dm(extracted={"url_texts": [{"url": "https://bad.example/1", "text": "t1"},
                                        {"url": "https://ok.example/2", "text": "t2"}]})
    fake_llm.queue(ValueError("garbled"))
    fake_llm.queue({"claim": "ok", "detail": "d", "confidence": "high", "answers": True})
    notes = await R._primary_notes(item, [], fake_llm, settings)
    assert [n["sources"] for n in notes] == [["https://ok.example/2"]] and "bad.example" in caplog.text
    fake_llm.queue({"claim": "x", "detail": "", "confidence": "low", "answers": False})
    assert await R._primary_notes(_dm(extracted={"url_texts": [{"url": "https://a.example", "text": "t"}]}),
                                  [], fake_llm, settings) == []


async def test_an_outage_on_a_primary_page_propagates(fake_llm, settings):
    fake_llm.queue(Retryforever("down"))
    with pytest.raises(Retryforever):
        await R._primary_notes(_dm(extracted={"url_texts": [{"url": "https://a.example", "text": "t"}]}),
                               [], fake_llm, settings)


async def test_links_in_the_operators_reply_are_fetched_as_primary_pages_up_to_the_cap(
    fake_llm, fake_http, settings,
):
    urls = [f"https://p{i}.example/doc" for i in range(6)]
    item = _dm(answer="try " + " ".join(urls))
    assert "https://p0.example/doc" in R._item_context(item)
    fake_http.respond(200, PAGE)
    for _ in range(R.MAX_PRIMARY_PAGES):
        fake_llm.queue({"claim": "c", "detail": "d", "confidence": "high", "answers": True})
    notes = await R._primary_notes(item, [], fake_llm, settings, http=fake_http)
    assert [n["sources"][0] for n in notes] == urls[:R.MAX_PRIMARY_PAGES] and len(fake_http.calls) == 2


# ------------------------------------------------- too little found: park, don't fail


def _plan():
    return {"entity": "Next-Concept Modeling (NCM)", "entity_context": "", "queries": ["a", "b"],
            "image_query": "i", "clauses": []}


async def _run(fake_llm, fake_http, settings, *, source="dm", intent="explain", good=1):
    """Three queries (the bare subject is added first): `good` of them yield a real note."""
    fake_llm.queue(_plan())
    fake_http.respond_for("/search", {"results": [_hit("https://arxiv.example/abs/1")]})
    fake_http.respond(200, PAGE)
    for _ in range(good):
        fake_llm.queue({"relevant": True, "why": "x"})
        fake_llm.queue({"claim": "NCM produces concepts first.", "detail": "d", "confidence": "high",
                        "answers": True})
    for _ in range(3 - good):
        fake_llm.queue({"relevant": False, "why": "other sense"})
    fake_llm.queue({"uncovered": []})
    return await R.research(_dm(source=source), fake_llm, fake_http, _cfg(settings),
                            hints={"intent": intent, "subject": "NCM", "clauses": []})


async def test_an_explainer_dm_proceeds_from_one_real_source(fake_llm, fake_http, settings):
    out = await _run(fake_llm, fake_http, settings)
    assert [n["claim"] for n in out["research"]] == ["NCM produces concepts first."]
    assert out["answer"] is None, "the follow-up is used up once research has run"


async def _uncovered_run(fake_llm, fake_http, settings, intent):
    fake_llm.queue(_plan())
    fake_http.respond_for("/search", {"results": [_hit("https://arxiv.example/abs/1")]})
    fake_http.respond(200, PAGE)
    for good in ((True, False, False) if intent == "explain" else (True, True, False)):
        fake_llm.queue({"relevant": good, "why": "x"})
        if good:
            fake_llm.queue({"claim": "NCM produces concepts first.", "detail": "d",
                            "confidence": "high", "answers": True})
    fake_llm.queue({"uncovered": ["how it differs from an LLM"]})
    return await R.research(_dm(), fake_llm, fake_http, _cfg(settings),
                            hints={"intent": intent, "clauses": ["what it is", "how it differs from an LLM"]})


async def test_an_explainer_dm_posts_with_the_unanswered_clause_marked_as_a_gap(fake_llm, fake_http, settings):
    """compose already tells the model to cover what the notes support and say the rest
    is not covered, without claiming it does not exist (the /post path)."""
    out = await _uncovered_run(fake_llm, fake_http, settings, "explain")
    assert out["gaps"] == ["how it differs from an LLM"] and len(out["research"]) == 1


async def test_a_news_dm_with_one_unanswered_clause_posts_with_the_gap(fake_llm, fake_http, settings):
    out = await _uncovered_run(fake_llm, fake_http, settings, "news")
    assert out["gaps"] == ["how it differs from an LLM"]


async def test_a_news_dm_with_one_source_asks_and_keeps_it(fake_llm, fake_http, settings):
    with pytest.raises(NeedsInput) as exc:
        await _run(fake_llm, fake_http, settings, intent="news")
    q = exc.value.question
    assert "only 1 solid source" in q and "arxiv.example" in q and "/post" in q and "/drop" in q
    assert exc.value.resume_status is Status.TRIAGED and len(exc.value.fields["research"]) == 1


async def test_no_source_on_a_dm_asks_without_the_post_option(fake_llm, fake_http, settings):
    with pytest.raises(NeedsInput) as exc:
        await _run(fake_llm, fake_http, settings, good=0)
    assert "/post" not in exc.value.question and "/drop" in exc.value.question
    assert exc.value.fields["research"] == []


async def test_a_channel_post_with_one_source_still_retries(fake_llm, fake_http, settings):
    with pytest.raises(Retryable, match="need at least 2"):
        await _run(fake_llm, fake_http, settings, source="channel")


async def test_a_crashing_researcher_is_still_a_retry(fake_llm, fake_http, settings):
    fake_llm.queue(_plan())
    fake_http.respond_for("/search", {"results": [_hit("https://f.example/a")]})
    fake_http.respond(200, PAGE)
    for _ in range(3):
        fake_llm.queue(ValueError("boom"))
    with pytest.raises(Retryable, match="crashed"):
        await R.research(_dm(), fake_llm, fake_http, _cfg(settings), hints={"intent": "news"})


async def test_the_question_resumes_through_the_existing_post_mechanics(db, fake_llm, fake_http, settings):
    class Msg:
        text = "/post"
        reply_to_message = None

    class Bot:
        async def send_message(self, chat_id=None, text=None, **kw):
            pass

    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text=DM_616)
    with pytest.raises(NeedsInput) as exc:
        await _run(fake_llm, fake_http, settings, intent="news")
    e = exc.value
    await ask(db, Bot(), settings, await db.get_item(i), e.question, e.resume_status, e.confidence, e.fields)
    assert await handle_answer(Msg(), db, settings, Bot()) == "posting"
    after = await db.get_item(i)
    assert after.status is Status.RESEARCHED and len(after.research) == 1


# ------------------------------------------- malformed add_slide (612's "add visuals")


@pytest.mark.parametrize("kind", te.ADDABLE_TYPES)
def test_the_add_prompt_names_every_types_required_fields(kind):
    flat = " ".join(te.SYSTEM_ADD.split())
    assert f"{kind}: {', '.join(REQUIRED_BODY_FIELDS[kind])}" in flat


async def test_a_slide_with_no_body_is_rejected_with_a_reason_that_says_so(fake_llm):
    from tests.revision.test_text_edit import act, item

    bad = {"type": "notes", "headline": "How NCM works"}
    fake_llm.queue_each([bad, bad])
    it = item()
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide", slide=2, instruction="explain"),
                                  "material")
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and out.reason == te.REASON_EMPTY_SLIDE
    assert "body" in fake_llm.calls[1].user, "the retry must name the field the slide lacks"


def test_empty_slide_has_operator_copy():
    from pipeline.revision import copy as rc

    row = rc.resolve(OutcomeCode.REJECTED_INTEGRITY, "add_slide", te.REASON_EMPTY_SLIDE)
    assert "no content" in row.text and te.REASON_EMPTY_SLIDE in rc.EXECUTOR_REASONS


@pytest.mark.parametrize("answer,expected", [
    ("try https://arxiv.org/abs/2411.00001.", "https://arxiv.org/abs/2411.00001"),
    ("see (https://arxiv.org/abs/2411.00001) for more", "https://arxiv.org/abs/2411.00001"),
    ("https://a.example/x, https://a.example/x", "https://a.example/x"),
])
async def test_a_link_in_the_reply_is_fetched_without_its_punctuation(
    fake_llm, fake_http, settings, answer, expected,
):
    fake_http.respond(200, PAGE)
    fake_llm.queue({"claim": "c", "detail": "d", "confidence": "high", "answers": True})
    notes = await R._primary_notes(_dm(answer=answer), [], fake_llm, settings, http=fake_http)
    assert [n["sources"] for n in notes] == [[expected]] and len(fake_http.calls) == 1


async def test_a_channel_posts_queries_are_passed_through_unshaped(fake_llm, settings):
    long_q = "OpenAI will block Cursor users from accessing its models within three months"
    fake_llm.queue({"entity": "OKF", "entity_context": "", "queries": [long_q, "okf spec"],
                    "image_query": "i", "clauses": []})
    queries, *_ = await R.plan_queries(
        Item(id=3, source="channel", status=Status.TRIAGED, raw_text="x"), fake_llm, settings, hints={})
    assert queries == [long_q, "okf spec"], "no bare query, no word cap"
