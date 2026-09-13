"""Enumeration path: find N things, score them, ask when too thin."""

import pytest

from pipeline.conversation import NeedsInput
from pipeline.models import Item, Status
from pipeline.stages.enumerate_items import (
    MAX_ITEMS,
    MIN_ITEM_SCORE,
    _confidence,
    classify,
    enumerate_items,
    is_relevant,
    score_candidate,
    subject_terms,
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
                    "queries": ["interview preparation"], "why": "asks for ten things", "catalogue": "repos"})
    plan = await classify(_item(), fake_llm)
    assert plan["intent"] == "list"
    assert plan["subject"] == "interview preparation"


async def test_a_news_claim_is_not_treated_as_a_list(fake_llm):
    fake_llm.queue({"intent": "news", "count": 0, "subject": "", "queries": [],
                    "why": "an event to verify", "catalogue": "repos"})
    plan = await classify(_item("OpenAI blocks Cursor users from its models"), fake_llm)
    assert plan["intent"] == "news"


async def test_requested_count_is_capped_at_the_carousel_limit(fake_llm):
    """Instagram allows 10 slides; a hook and a follow slide take two."""
    fake_llm.queue({"intent": "list", "count": 50, "subject": "s",
                    "queries": ["q"], "why": "w", "catalogue": "repos"})
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
                    "queries": ["obscure thing"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": []})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert exc.value.confidence == 0
    assert "found nothing worth posting" in exc.value.question
    assert "/drop" in exc.value.question
    assert exc.value.resume_status == Status.TRIAGED


async def test_a_thin_set_asks_and_names_what_it_found(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "vector databases",
                    "queries": ["vector databases"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": [_repo("pgvector/pgvector", 900)]})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert "pgvector/pgvector" in exc.value.question, "say what was actually found"
    assert "post what you have" in exc.value.question


async def test_a_full_set_proceeds_without_asking(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "interview prep",
                    "queries": ["interview prep"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": [
        _repo(f"owner{i}/awesome-interview-prep-{i}", 200 + i * 50) for i in range(8)
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert out["intent"] == "list"
    assert len(out["research"]) == 8
    assert out["confidence"] >= 45
    assert all(n["sources"] for n in out["research"]), "every item cites its repo"


async def test_items_arrive_ranked_by_score(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "list", "count": 8, "subject": "s",
                    "queries": ["q"], "why": "w", "catalogue": "repos"})
    fake_http.respond(200, {"results": [
        _repo("low/repo", 12), _repo("high/awesome-repo", 5000), _repo("mid/repo", 300),
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    scores = [n["score"] for n in out["research"]]
    assert scores == sorted(scores, reverse=True)


async def test_an_operator_answer_is_searched_first(fake_llm, fake_http, settings):
    """Their words beat anything inferred from the original request."""
    fake_llm.queue({"intent": "list", "count": 8, "subject": "wrong guess",
                    "queries": ["wrong guess"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": [
        _repo(f"o{i}/leetcode-patterns-{i}", 300) for i in range(8)]})

    await enumerate_items(
        _item(answer="try leetcode patterns instead"), fake_llm, fake_http, settings
    )
    assert fake_http.calls[0].params["q"] == "try leetcode patterns instead"


async def test_a_news_item_returns_immediately(fake_llm, fake_http, settings):
    fake_llm.queue({"intent": "news", "count": 0, "subject": "", "queries": [], "why": "w", "catalogue": "repos"})
    out = await enumerate_items(_item("OpenAI cut off Cursor"), fake_llm, fake_http, settings)
    assert out == {"intent": "news"}
    assert fake_http.calls == [], "must not search on the news path"


# --- relevance gate ---------------------------------------------------------
#
# score_candidate measures how good a repository is, never what it is about.
# "Research about panpsychism" enumerated eight popular developer tools at
# confidence 96, because stars and a long description score highly whatever
# the subject was.

def _cand(url, title="", content="", tags=None, popularity=0):
    return {"url": url, "title": title, "content": content,
            "tags": tags or [], "popularity": popularity}


def test_subject_terms_drops_container_words():
    assert subject_terms("best open source github repos") == []
    assert subject_terms("interview preparation") == ["interview", "preparation"]


def test_subject_terms_drops_words_too_short_to_mean_anything():
    assert "ai" not in subject_terms("ai tools")


def test_unrelated_repo_is_rejected_however_popular():
    terms = subject_terms("panpsychism")
    starry = _cand("https://github.com/Powerlevel9k/powerlevel9k",
                   title="powerlevel9k",
                   content="A Zsh theme with a lot of segments and options",
                   popularity=13000)
    assert is_relevant(starry, terms) is False
    # And it would otherwise have sailed past the score threshold.
    assert score_candidate(starry) >= MIN_ITEM_SCORE


def test_on_subject_repo_is_kept():
    terms = subject_terms("interview preparation")
    assert is_relevant(
        _cand("https://github.com/ElizaLo/Interview-Preparation"), terms) is True


def test_a_match_in_the_description_counts():
    terms = subject_terms("panpsychism")
    assert is_relevant(
        _cand("https://github.com/someone/mind",
              content="Essays on panpsychism and consciousness"), terms) is True


def test_a_match_in_tags_counts():
    terms = subject_terms("kubernetes operators")
    assert is_relevant(
        _cand("https://github.com/x/y", tags=["kubernetes", "go"]), terms) is True


def test_no_usable_terms_filters_nothing():
    """Dropping every candidate is worse than not filtering at all."""
    assert is_relevant(_cand("https://github.com/a/b"), []) is True


async def test_off_topic_results_ask_instead_of_composing_a_wrong_deck(
    fake_llm, fake_http, settings
):
    """The panpsychism failure, end to end.

    A GitHub search for a subject with no repositories returns popular
    unrelated ones. They score highly on stars and description alone, so
    without a relevance gate the pipeline composed eight developer tools under
    a philosophy request at confidence 96. Asking is the correct outcome.
    """
    fake_llm.queue({"intent": "list", "count": 8, "subject": "panpsychism",
                    "queries": ["panpsychism"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": [
        _repo("Powerlevel9k/powerlevel9k", 13000, "A Zsh theme with many segments"),
        _repo("inputsh/awesome-c", 3000, "A curated list of C frameworks"),
        _repo("phuocng/csslayout", 8000, "A collection of CSS layout patterns"),
        _repo("mcallegari/qlcplus", 900, "Lighting control software"),
    ]})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert exc.value.confidence == 0


async def test_on_topic_results_still_compose(fake_llm, fake_http, settings):
    """The gate must not reject everything — the counterpart to the test above."""
    fake_llm.queue({"intent": "list", "count": 4, "subject": "vector databases",
                    "queries": ["vector databases"], "why": "list", "catalogue": "repos"})
    fake_http.respond(200, {"results": [
        _repo("pgvector/pgvector", 900, "Open-source vector similarity search"),
        _repo("qdrant/qdrant", 2000, "Vector database for the next generation"),
        _repo("milvus-io/milvus", 3000, "A cloud-native vector database"),
        _repo("weaviate/weaviate", 1500, "The open source vector database"),
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert len(out["research"]) == 4


# --- catalogue: not everything enumerable lives on GitHub -------------------
#
# "the early signs of burnout in IT professionals" is a real enumeration whose
# things are signs. It was sent to GitHub, which indexes none, and returned 0
# candidates — right queries, wrong index.

def _page(url, title, content, tags=None):
    return {"url": url, "title": title, "content": content,
            "popularity": None, "tags": tags or []}


def test_a_web_page_scores_above_the_bar_as_web_and_below_it_as_a_repo():
    page = _page("https://mayoclinic.org/diseases-conditions/burn-out/symptoms",
                 "Job burnout: How to spot it and take action",
                 "Job burnout is a special type of work-related stress. "
                 "Know the signs, causes and what you can do about it.")
    assert score_candidate(page, repos=False) >= MIN_ITEM_SCORE
    assert score_candidate(page, repos=True) < MIN_ITEM_SCORE, \
        "repo signals reject a good page, which is how the set came back empty"


def test_a_homepage_scores_below_an_article():
    home = _page("https://example.com/", "Example", "A publication about things.")
    deep = _page("https://example.com/burnout-early-warning-signs",
                 "Early warning signs of burnout",
                 "The signs that appear before exhaustion sets in, and what they mean.")
    assert score_candidate(deep, repos=False) > score_candidate(home, repos=False)


async def test_a_web_enumeration_searches_categories_not_github(
    fake_llm, fake_http, settings
):
    fake_llm.queue({"intent": "list", "count": 5, "subject": "burnout signs",
                    "queries": ["early signs of burnout"], "why": "list",
                    "catalogue": "web"})
    fake_llm.queue({"expansions": []})
    fake_http.respond(200, {"results": [
        _page(f"https://health{i}.example/signs-of-burnout",
              f"Warning signs of burnout {i}",
              "Exhaustion, cynicism and reduced efficacy are the recognised "
              "dimensions of burnout, and each shows early.")
        for i in range(5)
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)

    assert out["intent"] == "list"
    params = fake_http.calls[0].params
    assert "engines" not in params, "a web enumeration must not be pinned to github"
    assert params.get("categories")


async def test_a_repo_enumeration_still_targets_github(
    fake_llm, fake_http, settings
):
    fake_llm.queue({"intent": "list", "count": 5, "subject": "vector databases",
                    "queries": ["vector database"], "why": "list",
                    "catalogue": "repos"})
    fake_llm.queue({"expansions": []})
    fake_http.respond(200, {"results": [_repo(f"o{i}/vector-database", 400)
                                        for i in range(5)]})

    await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert fake_http.calls[0].params.get("engines") == "github"


async def test_web_notes_carry_no_invented_repo_fields(
    fake_llm, fake_http, settings
):
    """Filling owner/stars/language from a URL path would put
    "diseases-conditions/burn-out" on a slide as a repository name."""
    fake_llm.queue({"intent": "list", "count": 4, "subject": "burnout signs",
                    "queries": ["early signs of burnout"], "why": "list",
                    "catalogue": "web"})
    fake_llm.queue({"expansions": []})
    fake_http.respond(200, {"results": [
        _page(f"https://health{i}.example/diseases/burnout-signs",
              f"Signs of burnout {i}",
              "Exhaustion, cynicism and reduced efficacy show up early in "
              "workers under sustained pressure.")
        for i in range(4)
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    for note in out["research"]:
        assert note["kind"] == "web"
        assert "owner" not in note and "stars" not in note
        assert note["url"].startswith("https://health")


def test_an_unknown_catalogue_defaults_to_web():
    """An empty search is worse than a broad one."""
    from pipeline.stages.enumerate_items import INTENT_SYSTEM
    collapsed = " ".join(INTENT_SYSTEM.split())
    assert "not a degraded search, it is an empty one" in collapsed


async def test_a_web_enumeration_prefers_distinct_sources(
    fake_llm, fake_http, settings
):
    """Eight signs from one publisher is one source, not eight."""
    fake_llm.queue({"intent": "list", "count": 6, "subject": "burnout signs",
                    "queries": ["early signs of burnout"], "why": "list",
                    "catalogue": "web"})
    fake_llm.queue({"expansions": []})
    fake_http.respond(200, {"results": [
        _page(f"https://onesite.example/page-{i}", f"Signs {i}",
              "Exhaustion, cynicism and reduced efficacy appear early in "
              "workers under sustained pressure, long before collapse.")
        for i in range(6)
    ]})

    with pytest.raises(NeedsInput):
        await enumerate_items(_item(), fake_llm, fake_http, settings)


# --- web results must rank, not just pass -----------------------------------
#
# The first version saturated: description, title and path depth all maxed, so
# every decent article scored exactly 76 and "keep the best 8 of 40" was really
# "keep the first 8 the search returned".

def test_authority_breaks_the_tie_between_similar_pages():
    journal = _page("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/",
                    "Burnout among IT staff: a systematic review",
                    "A systematic review of burnout prevalence among IT staff, "
                    "covering measurement instruments and interventions.")
    blog = _page("https://someblog.example/posts/burnout",
                 "Burnout among IT staff: a systematic review",
                 "A systematic review of burnout prevalence among IT staff, "
                 "covering measurement instruments and interventions.")
    assert score_candidate(journal, repos=False) > score_candidate(blog, repos=False)


def test_scores_are_not_all_the_same():
    pages = [
        _page("https://nih.gov/a/b/c", "A long and specific article title here",
              "x" * 300),
        _page("https://forbes.com/a/b", "A moderately long title for this one",
              "x" * 120),
        _page("https://blog.example/p", "Short", "x" * 30),
    ]
    scores = [score_candidate(p, repos=False) for p in pages]
    assert len(set(scores)) == len(scores), "a ranking that ties ranks nothing"
    assert scores == sorted(scores, reverse=True)


def test_a_bare_homepage_falls_below_the_bar():
    home = _page("https://contentfarm.example/", "Home", "A site.")
    assert score_candidate(home, repos=False) < MIN_ITEM_SCORE


def test_authority_is_matched_on_host_not_substring():
    """A host that merely contains an authoritative name must not inherit it."""
    from pipeline.stages.enumerate_items import authority

    assert authority("https://nih.gov/x") == 18
    assert authority("https://pmc.ncbi.nlm.nih.gov/x") == 18
    assert authority("https://notnih.gov.evil.example/x") == 0
    assert authority("https://example.com/x") == 0


# --- matching is on words, not substrings -----------------------------------

def test_a_substring_does_not_satisfy_a_term():
    """"content" must not be answered by "discontent", nor "generation" by
    "regeneration" — that is how an off-subject page looks on-subject."""
    terms = subject_terms("automated content generation")
    page = _page("https://x.example/essay", "Discontent and regeneration",
                 "A long study of discontent in modern regeneration projects.")
    assert is_relevant(page, terms) is False


def test_a_plural_subject_is_answered_by_the_singular():
    terms = subject_terms("vector databases")
    page = _page("https://x.example/db", "A fast vector database",
                 "An open-source vector database for similarity search.")
    assert is_relevant(page, terms) is True


def test_a_singular_subject_is_answered_by_the_plural():
    terms = subject_terms("burnout symptom")
    page = _page("https://x.example/s", "Burnout symptoms",
                 "The symptoms that show up first.")
    assert is_relevant(page, terms) is True


def test_a_single_rare_term_still_matches():
    terms = subject_terms("panpsychism")
    page = _page("https://x.example/p", "Panpsychism", "Essays on panpsychism.")
    assert is_relevant(page, terms) is True


def test_a_term_inside_a_url_slug_counts():
    terms = subject_terms("kubernetes operators")
    page = _page("https://x.example/kubernetes-operator-guide", "Guide", "d" * 80)
    assert is_relevant(page, terms) is True


# --- web enumerations list things, not sources ------------------------------
#
# "Highlight the early signs of burnout" produced eight slides named after
# their sources — "ACM study on developer burnout", "NIH systematic review" —
# because enumeration composes one slide per search result. A repo IS the
# thing; a page only describes it.

def _web_plan(count=4, subject="burnout signs"):
    return {"intent": "list", "count": count, "subject": subject,
            "queries": ["early signs of burnout"], "why": "list",
            "catalogue": "web"}


async def test_a_web_enumeration_names_things_not_documents(
    fake_llm, fake_http, settings, monkeypatch
):
    import pipeline.stages.enumerate_items as mod
    monkeypatch.setattr(mod, "fetch_text", lambda http, url: _text())

    async def _text():
        return "Exhaustion, cynicism and reduced efficacy are the recognised signs."

    fake_llm.queue(_web_plan())
    fake_llm.queue({"expansions": []})
    fake_llm.queue({"things": [
        {"name": "Emotional exhaustion", "detail": "Depleted before the day starts.",
         "source": "1"},
        {"name": "Cynicism and detachment", "detail": "Distance from the work.",
         "source": "2"},
        {"name": "Reduced efficacy", "detail": "Output falls, effort does not.",
         "source": "1"},
        {"name": "Sleep disruption", "detail": "Waking at three, mind running.",
         "source": "3"},
    ]})
    fake_http.respond(200, {"results": [
        _page(f"https://health{i}.example/signs", f"Signs of burnout {i}",
              "Exhaustion, cynicism and reduced efficacy appear early in "
              "workers under sustained pressure, long before collapse.")
        for i in range(4)
    ]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    claims = [n["claim"] for n in out["research"]]

    assert "Emotional exhaustion" in claims
    assert not any("study" in c.lower() or "review" in c.lower() for c in claims), \
        "a slide must name the sign, not the paper that mentions it"
    for note in out["research"]:
        assert note["sources"], "every thing still cites where it came from"


async def test_duplicate_things_across_sources_are_merged(
    fake_llm, fake_http, settings, monkeypatch
):
    """Four papers describing exhaustion is one sign, not four."""
    import pipeline.stages.enumerate_items as mod

    async def _text(http, url):
        return "Exhaustion is the first sign."

    monkeypatch.setattr(mod, "fetch_text", _text)
    fake_llm.queue(_web_plan())
    fake_llm.queue({"expansions": []})
    fake_llm.queue({"things": [
        {"name": "Exhaustion", "detail": "d", "source": "1"},
        {"name": "exhaustion", "detail": "d", "source": "2"},
        {"name": "Cynicism", "detail": "d", "source": "1"},
    ]})
    fake_http.respond(200, {"results": [
        _page(f"https://h{i}.example/s", f"Signs {i}", "d" * 90) for i in range(4)]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    claims = [n["claim"].lower() for n in out["research"]]
    assert claims.count("exhaustion") == 1


async def test_extraction_failure_falls_back_to_listing_sources(
    fake_llm, fake_http, settings, monkeypatch
):
    """A worse deck still beats no deck."""
    import pipeline.stages.enumerate_items as mod

    async def _text(http, url):
        return "some body text"

    monkeypatch.setattr(mod, "fetch_text", _text)
    fake_llm.queue(_web_plan())
    fake_llm.queue({"expansions": []})
    fake_llm.queue({"things": []})
    fake_http.respond(200, {"results": [
        _page(f"https://h{i}.example/s", f"Signs {i}", "d" * 90) for i in range(4)]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert len(out["research"]) == 4, "fell back to the sources"


async def test_a_repo_enumeration_does_not_extract(
    fake_llm, fake_http, settings
):
    """A repository is the thing; there is nothing to read out of it."""
    fake_llm.queue({"intent": "list", "count": 4, "subject": "vector databases",
                    "queries": ["vector database"], "why": "list",
                    "catalogue": "repos"})
    fake_llm.queue({"expansions": []})
    fake_http.respond(200, {"results": [_repo(f"o{i}/vector-database", 400)
                                        for i in range(4)]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert all(n["kind"] == "repo" for n in out["research"])
    assert len(fake_llm.calls) == 2, "classify + expand only, no extraction call"


# --- enumeration answers every clause, or asks ------------------------------
#
# The clause gate lived only on the news path, so an enumeration could return
# eight items answering half the request and report confidence 91.

async def test_an_enumeration_missing_a_clause_asks(
    fake_llm, fake_http, settings, monkeypatch
):
    import pipeline.stages.enumerate_items as mod

    async def _text(http, url):
        return "Exhaustion and cynicism are the first signs."

    monkeypatch.setattr(mod, "fetch_text", _text)
    fake_llm.queue({**_web_plan(count=4), "clauses": [
        "the early signs of burnout", "the early signs of depression"]})
    fake_llm.queue({"expansions": []})
    fake_llm.queue({"things": [
        {"name": "Exhaustion", "detail": "d", "source": "1"},
        {"name": "Cynicism", "detail": "d", "source": "1"},
        {"name": "Reduced efficacy", "detail": "d", "source": "1"},
        {"name": "Sleep disruption", "detail": "d", "source": "1"},
    ]})
    fake_llm.queue({"uncovered": ["the early signs of depression"]})
    fake_http.respond(200, {"results": [
        _page(f"https://h{i}.example/s", f"Signs {i}", "d" * 90) for i in range(4)]})

    with pytest.raises(NeedsInput) as exc:
        await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert "depression" in exc.value.question


async def test_an_enumeration_covering_every_clause_proceeds(
    fake_llm, fake_http, settings, monkeypatch
):
    import pipeline.stages.enumerate_items as mod

    async def _text(http, url):
        return "Exhaustion and cynicism are the first signs."

    monkeypatch.setattr(mod, "fetch_text", _text)
    fake_llm.queue({**_web_plan(count=3), "clauses": ["the early signs of burnout"]})
    fake_llm.queue({"expansions": []})
    fake_llm.queue({"things": [
        {"name": "Exhaustion", "detail": "d", "source": "1"},
        {"name": "Cynicism", "detail": "d", "source": "1"},
        {"name": "Reduced efficacy", "detail": "d", "source": "1"},
    ]})
    fake_llm.queue({"uncovered": []})
    fake_http.respond(200, {"results": [
        _page(f"https://h{i}.example/s", f"Signs {i}", "d" * 90) for i in range(4)]})

    out = await enumerate_items(_item(), fake_llm, fake_http, settings)
    assert out["clauses"] == ["the early signs of burnout"]
    assert len(out["research"]) == 3
