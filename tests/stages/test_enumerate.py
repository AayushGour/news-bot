"""Enumeration path: find N things, score them, ask when too thin."""

import pytest

from pipeline.conversation import NeedsInput
from pipeline.models import Item, Status
from pipeline.stages.enumerate_items import (
    MAX_ITEMS,
    MIN_ITEM_SCORE,
    classify,
    enumerate_items,
    score_candidate,
    _confidence,
)


def _item(text="10 awesome GitHub repositories for interview preparation", **kw):
    base = dict(id=1, source="dm", status=Status.TRIAGED, raw_text=text)
    base.update(kw)
    return Item(**base)


def _repo(name, stars, desc="A useful collection of things worth reading."):
    return {"url": f"https://github.com/{name}", "title": name,
            "content": desc, "popularity": stars, "tags": ["x"]}


# ------------------------------------------------------------- classification


async def test_a_list_request_is_recognised(fake_llm):
    fake_llm.queue({"intent": "list", "count": 10, "subject": "interview preparation",
                    "queries": ["interview preparation"], "why": "asks for ten things"})
    plan = await classify(_item(), fake_llm)
    assert plan["intent"] == "list"
    assert plan["subject"] == "interview preparation"


async def test_a_news_claim_is_not_treated_as_a_list(fake_llm):
    fake_llm.queue({"intent": "news", "count": 0, "subject": "", "queries": [],
                    "why": "an event to verify"})
    plan = await classify(_item("OpenAI blocks Cursor users from its models"), fake_llm)
    assert plan["intent"] == "news"


async def test_requested_count_is_capped_at_the_carousel_limit(fake_llm):
    """Instagram allows 10 slides; a hook and a follow slide take two."""
    fake_llm.queue({"intent": "list", "count": 50, "subject": "s",
                    "queries": ["q"], "why": "w"})
    assert (await classify(_item(), fake_llm))["count"] == MAX_ITEMS


def test_the_prompt_warns_against_searching_for_the_container():
    """'awesome GitHub repositories' describes the container; 'interview
    preparation' is the topic that actually finds things."""
    from pipeline.stages.enumerate_items import INTENT_SYSTEM

    collapsed = " ".join(INTENT_SYSTEM.split())
    assert "describes the container rather than the topic" in collapsed
    assert "Search for the things themselves, never for articles about them" in collapsed


# -------------------------------------------------------------------- scoring


def test_a_described_and_starred_repo_scores_well():
    assert score_candidate(_repo("owner/awesome-thing", 169)) >= 60


def test_an_undescribed_unstarred_repo_scores_poorly():
    bare = {"url": "https://github.com/a/b", "popularity": 1, "content": "", "tags": []}
    assert score_candidate(bare) < MIN_ITEM_SCORE


def test_stars_have_diminishing_returns():
    """10k stars is not a thousand times better than 10."""
    small = score_candidate(_repo("a/b", 12))
    huge = score_candidate(_repo("a/b", 50_000))
    assert huge > small
    assert huge - small <= 35


def test_a_subpage_scores_below_a_repository_root():
    root = score_candidate(_repo("owner/repo", 100))
    sub = score_candidate({**_repo("owner/repo/blob/main/README.md", 100)})
    assert root > sub


# ----------------------------------------------------------------- confidence


def test_coverage_dominates_confidence():
    """Four excellent items still do not answer 'give me ten'."""
    four_great = _confidence([{"score": 95}] * 4, 10)
    eight_ok = _confidence([{"score": 55}] * 8, 10)
    assert eight_ok > four_great


def test_nothing_found_is_zero_confidence():
    assert _confidence([], 8) == 0


# ------------------------------------------------- asking instead of guessing


async def test_nothing_found_asks_the_operator(fake_llm, fake_http, settings):
    """A deck assembled from nothing is worse than no deck: it costs a review
    and looks like a system working."""
    fake_llm.queue({"intent": "list", "count": 8, "subject": "obscure thing",
                    "queries": ["obscure thing"], "why": "list"})
    fake_http.respond(200, {"results": []})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert exc.value.confidence == 0
    assert "found nothing worth posting" in exc.value.question
    assert "/drop" in exc.value.question
    assert exc.value.resume_status == Status.TRIAGED


async def test_a_thin_set_asks_and_names_what_it_found(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "vector databases",
                    "queries": ["vector databases"], "why": "list"})
    fake_http.respond(200, {"results": [_repo("pgvector/pgvector", 900)]})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert "pgvector/pgvector" in exc.value.question, "say what was actually found"
    assert "post what you have" in exc.value.question


async def test_a_full_set_proceeds_without_asking(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "interview prep",
                    "queries": ["interview prep"], "why": "list"})
    fake_http.respond(200, {"results": [
        _repo(f"owner{i}/awesome-repo-{i}", 200 + i * 50) for i in range(8)
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert out["intent"] == "list"
    assert len(out["research"]) == 8
    assert out["confidence"] >= 45
    assert all(n["sources"] for n in out["research"]), "every item cites its repo"


async def test_items_arrive_ranked_by_score(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "s",
                    "queries": ["q"], "why": "w"})
    fake_http.respond(200, {"results": [
        _repo("low/repo", 12), _repo("high/awesome-repo", 5000), _repo("mid/repo", 300),
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    scores = [n["score"] for n in out["research"]]
    assert scores == sorted(scores, reverse=True)


async def test_an_operator_answer_is_searched_first(fake_llm, fake_http, settings):
    """Their words beat anything inferred from the original request."""
    fake_llm.queue({"intent": "list", "count": 8, "subject": "wrong guess",
                    "queries": ["wrong guess"], "why": "list"})
    fake_http.respond(200, {"results": [_repo(f"o{i}/awesome-r{i}", 300) for i in range(8)]})

    await enumerate_items(
        _item(answer="try leetcode patterns instead"), fake_llm, fake_http, settings
    )
    assert fake_http.calls[0].params["q"] == "try leetcode patterns instead"


async def test_a_news_item_returns_immediately(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "news", "count": 0, "subject": "", "queries": [], "why": "w"})
    out = await enumerate_items(_item("OpenAI cut off Cursor"), fake_llm, fake_http, settings)
    assert out == {"intent": "news"}
    assert fake_http.calls == [], "must not search on the news path"
