"""Enumeration research: find N discrete things, score each one.

The news path asks "did this happen and what does it mean". This path asks
"which N things best answer this request". They need different queries,
different filtering, and a different notion of success — a news item succeeds
with two good sources, an enumeration succeeds only if it has enough items to
be worth posting.

Scoring is per item and deliberately cheap: signals already returned by search,
not another model call per candidate. An item nobody stars and nobody described
is not one the operator wants on a slide.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..conversation import NeedsInput
from ..coverage import unaddressed
from ..models import Item, Status
from pathlib import Path

from ..expand import expand_queries, widen
from ..search import (
    REPO_ENGINES, dedupe_by_domain, dedupe_by_path, download_avatar, fetch_text,
    search_many,
)

log = logging.getLogger(__name__)

#: Below this an item is not worth a slide.
MIN_ITEM_SCORE = 35
#: Below this the whole set is too thin to post, and the operator is asked.
MIN_SET_CONFIDENCE = 45
#: Instagram carousels hold 10 slides. A hook and a follow slide take two.
MAX_ITEMS = 8

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["news", "list"]},
        "count": {"type": "integer"},
        "subject": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"},
                    "minItems": 2, "maxItems": 4},
        "catalogue": {"type": "string", "enum": ["repos", "web"]},
        "clauses": {"type": "array", "items": {"type": "string"},
                    "minItems": 1, "maxItems": 4},
        "why": {"type": "string"},
    },
    "required": ["intent", "count", "subject", "queries", "why", "catalogue",
                 "clauses"],
}

INTENT_SYSTEM = """You are triaging a request sent to a content pipeline.

Decide which of two jobs it is.

"news": a claim about something that happened, to be verified and explained.
  "OpenAI blocks Cursor users", "Uber launches robotaxis in London".

"list": a request to enumerate several things — tools, repositories, papers,
  companies, techniques. "10 GitHub repos for interview prep", "best open
  source vector databases", "5 papers on retrieval". The answer is a
  collection, not a verdict on an event.

For "list" also give:
  "count": how many were asked for. Default 8 if unstated.
  "subject": what is being collected, in a few words, as a searcher would type
    it. For "10 awesome GitHub repositories for interview preparation" that is
    "interview preparation" — not "awesome GitHub repositories", which
    describes the container rather than the topic.
  "queries": 2-4 searches that would surface the actual things. Search for the
    things themselves, never for articles about them.
  "catalogue": where these things live.
    "repos" — software you could clone or install: repositories, libraries,
      CLI tools, datasets, models. Indexed on GitHub.
    "web" — anything else: symptoms, signs, techniques, papers, companies,
      events, practices, concepts. These are NOT on GitHub, and searching there
      for them returns nothing at all.
    Getting this wrong is not a degraded search, it is an empty one. "The early
    warning signs of burnout" is a real enumeration whose things are signs, not
    repositories. Ask yourself whether a reader could clone the answer.

"clauses": the distinct things the request asks for, in the requester's own
words. One ask is one clause. A request joining several with commas or "and"
has one clause each — "the early signs of burnout and depression in IT
professionals, how ai is causing more/less" is two, and answering only the
second is how half a request goes missing without anyone noticing.

For "news", count and subject may be empty and queries are ignored.

"why": one sentence on the classification."""


async def classify(item: Item, llm) -> dict:
    """Decide whether this is a news claim or an enumeration request."""
    text = (item.raw_text or "").strip()
    verdict = await llm.cheap(INTENT_SYSTEM, f"REQUEST:\n{text}", schema=INTENT_SCHEMA)
    intent = str(verdict.get("intent", "news")).lower()
    if intent not in ("news", "list"):
        intent = "news"
    return {
        "intent": intent,
        "count": max(3, min(int(verdict.get("count") or MAX_ITEMS), MAX_ITEMS)),
        "subject": str(verdict.get("subject", "")).strip(),
        "queries": [str(q).strip() for q in (verdict.get("queries") or []) if str(q).strip()],
        "why": str(verdict.get("why", ""))[:200],
        # Default to web: an empty search is worse than a broad one, and
        # everything that is not clonable lives outside GitHub.
        "catalogue": ("repos" if str(verdict.get("catalogue", "")).strip().lower()
                      == "repos" else "web"),
        "clauses": [str(c).strip() for c in (verdict.get("clauses") or [])
                    if str(c).strip()],
    }


#: Words that describe the container, not the topic. A candidate matching only
#: these has told us nothing about whether it is on subject.
_SUBJECT_STOPWORDS = frozenset({
    "the", "and", "for", "with", "about", "best", "top", "good", "great",
    "open", "source", "opensource", "github", "repo", "repos", "repository",
    "repositories", "tool", "tools", "project", "projects", "library",
    "libraries", "awesome", "curated", "list", "lists", "resource",
    "resources", "guide", "guides", "free", "new", "modern", "popular",
})

#: Shorter than this a term matches too much to mean anything ("ai" is inside
#: "chain", "detail", "email").
_MIN_TERM_LEN = 4


def subject_terms(subject: str) -> list[str]:
    """The words in a subject that actually carry its meaning."""
    words = re.findall(r"[a-z0-9]+", (subject or "").lower())
    return [w for w in words
            if len(w) >= _MIN_TERM_LEN and w not in _SUBJECT_STOPWORDS]


def _stem(word: str) -> str:
    """Crude singular. "databases" and "database" are the same word here."""
    if len(word) > 4 and word.endswith("es") and not word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


#: Prefixes that change the word they attach to into a different subject.
#: "discontent" is
#: not about content and "regeneration" is not about content generation, while
#: "pgvector" is very much about vectors — which is why the
#: match is per token with these excluded rather than a blanket word boundary —
#: requiring whole words rejected pgvector for a "vector databases" search.
_NEGATING = ("dis", "un", "non", "anti", "mal", "mis", "counter", "re")


def _token_matches(token: str, term: str) -> bool:
    """Does this token carry the term's meaning?"""
    if token == term:
        return True
    if not token.endswith(term):
        return token.startswith(term) and len(token) > len(term)
    prefix = token[: -len(term)]
    return bool(prefix) and not any(prefix.endswith(n) for n in _NEGATING)


#: How much of ``content`` can still be a description rather than a dumped
#: README. SearXNG returns a repository's description here, except when it
#: returns the whole README: measured on one search, every genuine result had
#: 62-270 characters while four spam repositories had ~64,000. In those, the
#: subject word appeared once, around offset 9,000, which was enough to pass
#: relevance — a Chinese propaganda repo and an anime message board ranked
#: alongside a machine-learning interview repo, all three scoring 88.
#:
#: This bounds the window used for MATCHING only. Nothing is discarded: the
#: full content stays on the candidate for every later stage to read.
DESCRIPTION_CHARS = 1000


def _haystack_words(candidate: dict) -> set[str]:
    text = " ".join(
        str(candidate.get(field) or "")[:DESCRIPTION_CHARS if field == "content" else None]
        for field in ("url", "title", "content")
    )
    tags = candidate.get("tags") or []
    if isinstance(tags, (list, tuple)):
        text += " " + " ".join(str(t) for t in tags)
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for word in words for w in (word, _stem(word))}


def _matches(words: set[str], term: str) -> bool:
    stemmed = _stem(term)
    return any(_token_matches(w, term) or _token_matches(w, stemmed) for w in words)


def is_relevant(candidate: dict, terms: list[str]) -> bool:
    """Is this candidate about the subject at all?

    Scoring below measures how good a repository is, never what it is about.
    Searching GitHub for "panpsychism" returns popular but unrelated projects
    when nothing matches, and those score highly on stars and description
    alone — the pipeline then published eight developer tools under a
    philosophy request, at confidence 96.

    Matching is per token with negating prefixes excluded, which is narrower
    than a raw substring and wider than a whole word. Raw substrings let
    "discontent" answer "content"; whole words rejected "pgvector" for a
    "vector databases" search, and pgvector is exactly what that search wants.
    Plurals are folded, so "databases" is answered by a page about a database.

    With no usable terms there is nothing to judge, so nothing is excluded:
    silently dropping every candidate would be worse than not filtering.
    """
    if not terms:
        return True
    words = _haystack_words(candidate)
    return any(_matches(words, term) for term in terms)


def score_candidate(candidate: dict, repos: bool = True) -> int:
    """0-100, from signals search already returned.

    Deliberately not a model call: this runs on every candidate, and a model
    asked to rate a page it cannot see would be guessing from the same fields
    anyway.

    The signals differ by catalogue. Stars and an owner/repo path say a lot
    about a repository and nothing about an article — scoring a web result on
    them puts every page under the threshold and empties the set, which is the
    same empty-result failure as searching the wrong index, one stage later.
    """
    if not repos:
        return _score_web(candidate)

    score = 0

    stars = candidate.get("popularity")
    if isinstance(stars, (int, float)) and stars > 0:
        # Log-ish: 10 stars is a signal, 100 is better, 10k is not 1000x better.
        for threshold, points in ((10, 12), (50, 10), (200, 10), (1000, 8), (5000, 5)):
            if stars >= threshold:
                score += points

    description = (candidate.get("content") or "").strip()
    if len(description) > 40:
        score += 25
    elif len(description) > 15:
        score += 12

    if candidate.get("tags"):
        score += 8

    path = urlparse(candidate.get("url", "")).path.strip("/")
    if path.count("/") == 1:                       # owner/repo, not a subpage
        score += 15
    if re.search(r"awesome|curated|collection|list", path, re.I):
        score += 10

    return min(score, 100)


#: Hosts and suffixes whose material is worth more on a slide with the
#: operator's handle on it. A question about symptoms answered from a content
#: farm and the same question answered from a journal are not equivalent, and
#: nothing else in a SearXNG result distinguishes them.
_AUTHORITY = {
    "high": (".gov", ".edu", ".ac.uk", "nih.gov", "who.int", "ncbi.nlm.nih.gov",
             "acm.org", "ieee.org", "nature.com", "science.org", "arxiv.org",
             "tandfonline.com", "springer.com", "sciencedirect.com",
             "mayoclinic.org", "nhs.uk", "apa.org", "cdc.gov"),
    "medium": ("wikipedia.org", "reuters.com", "apnews.com", "bbc.co.uk",
               "ft.com", "economist.com", "nytimes.com", "theguardian.com",
               "harvard.edu", "mit.edu", "stanford.edu", "forbes.com",
               "hbr.org", "stackoverflow.com", "github.io"),
}


def authority(url: str) -> int:
    """0, 8 or 18 — how much weight this host's word carries."""
    host = urlparse(url or "").netloc.lower()
    if not host:
        return 0
    if any(host == m or host.endswith(m) for m in _AUTHORITY["high"]):
        return 18
    if any(host == m or host.endswith(m) for m in _AUTHORITY["medium"]):
        return 8
    return 0


def _score_web(candidate: dict) -> int:
    """0-100 for a page, from what a search result actually carries.

    The earlier version gated but did not rank: description, title and path
    depth all saturated, so every decent article scored exactly 76 and "keep
    the best 8 of 40" degenerated into "keep the first 8". The gradients below
    are finer and host authority breaks the ties, which is the web's nearest
    equivalent to a star count.
    """
    score = authority(candidate.get("url", ""))

    description = (candidate.get("content") or "").strip()
    for threshold, points in ((240, 30), (160, 26), (100, 21), (60, 16), (20, 9)):
        if len(description) >= threshold:
            score += points
            break

    title = (candidate.get("title") or "").strip()
    if len(title) > 30:
        score += 16
    elif title:
        score += 10

    path = urlparse(candidate.get("url", "")).path.strip("/")
    if path:
        # A homepage is a publication, not an answer to the question.
        score += 14
    if len(path) > 12:
        score += 6
    if path.count("/") >= 2:
        # A deep path is usually an article rather than a section index.
        score += 4

    return min(score, 100)


async def enumerate_items(item: Item, llm, http, settings) -> dict:
    """Find and score N things. Raises NeedsInput when the result is too thin."""
    plan = await classify(item, llm)
    if plan["intent"] != "list":
        return {"intent": "news"}

    subject = plan["subject"] or (item.raw_text or "")[:60]
    queries = plan["queries"] or [subject]
    if item.answer:
        # The operator answered a question about this item; their words are
        # better than anything inferred from the original request.
        queries = [item.answer] + queries

    # Judge relevance against the operator's own words when they corrected us:
    # the inferred subject is precisely what they were correcting, so filtering
    # on it would throw away the results their answer was meant to find.
    terms = subject_terms(item.answer or subject)

    # Widen before searching, and search everything at once. One phrasing of a
    # query is one vocabulary; the repositories worth finding are indexed under
    # several. The queries are independent, so running them in series only
    # meant waiting for the slowest engine once per query.
    planned = queries[:4]
    expansions = await expand_queries(llm, planned, subject)
    widened = [q for query in planned for q in widen(query, expansions)]
    log.info("item %s searching %d queries (%d planned, %d expanded)",
             item.id, len(widened), len(planned), len(widened) - len(planned))

    seen: dict[str, dict] = {}
    off_topic = 0
    # Enumerating N things and enumerating N *repositories* are different
    # jobs, and treating them as one sent a request for the warning signs of
    # burnout to GitHub, which indexes none. The queries were fine; the index
    # was not.
    repos = plan["catalogue"] == "repos"
    try:
        results = await search_many(
            http, settings.searxng_url, widened, limit=20,
            **({"engines": REPO_ENGINES} if repos
               else {"categories": settings.searxng_categories}),
        )
    except Exception as exc:
        log.warning("enumeration search failed: %s", exc)
        results = []

    # Dedupe by what makes two results the same answer. For repositories that
    # is the URL — twenty github.com results are twenty different projects, and
    # collapsing by domain would keep one. For the open web it is the site: a
    # list of eight signs wants eight sources, not eight pages from one
    # publisher, which is the same reason the news path dedupes by domain.
    pool = (dedupe_by_path(results, 40) if repos
            else dedupe_by_domain(results, 40))
    for candidate in pool:
        key = (urlparse(candidate["url"]).path.rstrip("/").lower() if repos
               else candidate["url"].rstrip("/").lower())
        if not key or key in seen:
            continue
        # Relevance first: a repository that is not about the subject
        # cannot be rescued by having a lot of stars.
        if not is_relevant(candidate, terms):
            off_topic += 1
            continue
        candidate["score"] = score_candidate(candidate, repos)
        seen[key] = candidate

    kept = sorted(
        (c for c in seen.values() if c["score"] >= MIN_ITEM_SCORE),
        key=lambda c: -c["score"],
    )[: plan["count"]]

    confidence = _confidence(kept, plan["count"])
    log.info(
        "item %s enumeration [%s]: %d candidates, %d off-topic, %d kept, confidence %d"
        " (subject %r)",
        item.id, plan["catalogue"], len(seen) + off_topic, off_topic,
        len(kept), confidence, subject,
    )

    if confidence < MIN_SET_CONFIDENCE:
        # Carry whatever was kept, even though it did not clear the bar. /post
        # sends what we have on for approval, and an item parked here with
        # nothing stored has nothing to send — the operator would be offered
        # an option that cannot work. These are the cheap notes deliberately:
        # extraction and logo fetching cost calls, and the set may never be
        # asked for.
        raise NeedsInput(
            _question(kept, plan, len(seen)),
            resume_status=Status.TRIAGED,
            confidence=confidence,
            fields={"research": [_note(c, subject, repos) for c in kept]},
        )

    if repos:
        notes = [_note(c, subject, repos) for c in kept]
        # Only a repository has an owner avatar to fetch.
        await _attach_logos(notes, item, http, settings)
    else:
        # A repository IS the thing being listed; a web page only DESCRIBES
        # it. Listing the pages produced a deck of eight slides named after
        # their sources — "ACM study on developer burnout", "NIH systematic
        # review" — when the request asked for the signs themselves. So the
        # things are read out of the documents instead.
        notes = await _extract_things(kept, plan, item, llm, http)
        if not notes:
            log.info("item %s extraction found nothing; listing sources", item.id)
            notes = [_note(c, subject, repos) for c in kept]

    # The same coverage check the news path runs. Enumeration bypassed it, so
    # a request asking for two things could return eight items answering only
    # one and report confidence 91.
    # Same reasoning as the news path: only an operator request has clauses.
    clauses = plan["clauses"] if item.source == "dm" else []
    uncovered = await unaddressed(
        llm, clauses,
        [f"{n.get('claim','')} {n.get('detail','')}" for n in notes],
    )
    if uncovered:
        raise NeedsInput(
            "I found items for this, but nothing that answers:\n"
            + "\n".join(f"  · {c}" for c in uncovered)
            + "\n\nReply with a better angle or a source, /post to send what "
              "I have for approval, or /drop.",
            resume_status=Status.TRIAGED,
            confidence=confidence,
            # Which clauses failed, not just that some did: /post uses
            # this to tell compose what it is missing.
            fields={"research": notes, "clauses": clauses,
                    "gaps": uncovered},
        )

    return {"intent": "list", "confidence": confidence, "research": notes,
            "clauses": clauses}


#: SearXNG puts the repository language ahead of the description, as
#: "Shell / Collection of resources for...". Splitting it out gives the slide a
#: badge instead of burying it in prose.
_LANG = re.compile(r"^([A-Za-z+#.\- ]{1,22})\s*/\s*(.+)$", re.S)


def _note(candidate: dict, subject: str, repos: bool = True) -> dict:
    """One researched thing, in the shape its catalogue calls for.

    A web page has no owner, no stars and no language. Filling those fields
    from a URL path would put "diseases-conditions/burn-out" on a slide as a
    repository name and hand _restore_repo_facts something to restore that was
    never researched.
    """
    url = candidate["url"]
    path = urlparse(url).path.strip("/")
    owner, _, name = path.partition("/")

    raw = (candidate.get("content") or "").strip()
    match = _LANG.match(raw)
    language, detail = (match.group(1).strip(), match.group(2).strip()) if match else ("", raw)

    note = {
        "question": subject,
        "claim": (path if repos else (candidate.get("title") or "").strip())
                 or (candidate.get("title") or url),
        "detail": detail if repos else raw,
        "confidence": "high" if candidate["score"] >= 60 else "medium",
        "sources": [url],
        "score": candidate["score"],
        "kind": "repo" if repos else "web",
        "url": url,
    }
    if repos:
        # Fields the slide renders directly rather than asking a model to
        # restate: the name, where to find it, and the two signals a reader
        # uses to judge a repository at a glance.
        note.update({
            "repo": path,
            "owner": owner,
            "name": name,
            "stars": candidate.get("popularity"),
            "language": language,
        })
    return note


THINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "things": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "detail": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "detail", "source"],
            },
        },
    },
    "required": ["things"],
}

THINGS_SYSTEM = """You extract the things a request asked to be listed.

You get a SUBJECT, how many things are wanted, and SOURCES — real pages about
the subject, each numbered.

Return "things": the distinct items the sources actually describe.

  "name":   the thing itself, 2-6 words. "Cynicism and detachment", not
            "Forbes article on burnout". A reader sees this as a headline, so
            it must name the thing, never the document that mentions it.
  "detail": one or two sentences explaining it, drawn from the sources.
  "source": the number of the source that describes it, as a string.

Rules:
- One entry per distinct thing. If four sources describe exhaustion, that is
  ONE thing, not four. Merge them and cite the clearest source.
- Never invent a thing no source mentions, and never pad to reach the count.
  Returning fewer real things is correct; the shortfall is handled elsewhere.
- If the sources genuinely do not describe discrete things — they are essays
  with no enumerable content — return an empty list rather than manufacturing
  one."""


async def _extract_things(
    candidates: list[dict], plan: dict, item: Item, llm, http,
) -> list[dict]:
    """Read the pages and pull out the things they describe.

    Best effort: a failure here returns [] and the caller falls back to listing
    the sources, which is a worse deck but still a deck.
    """
    subject = plan["subject"]
    corpus, cited = [], []
    for candidate in candidates[:6]:
        text = await fetch_text(http, candidate["url"])
        body = (text or candidate.get("content") or "").strip()
        if not body:
            continue
        cited.append(candidate)
        corpus.append(
            f"[{len(cited)}] {candidate.get('title','')}\n"
            f"{candidate['url']}\n{body}"
        )

    if not corpus:
        return []

    user = (
        f"SUBJECT: {subject}\nWANTED: {plan['count']} things\n\n"
        f"SOURCES:\n\n" + "\n\n---\n\n".join(corpus)
    )
    try:
        result = await llm.good(THINGS_SYSTEM, user, schema=THINGS_SCHEMA)
    except Exception as exc:
        log.warning("item %s thing-extraction failed: %s", item.id, exc)
        return []

    notes: list[dict] = []
    seen: set[str] = set()
    for entry in (result.get("things") or [])[: plan["count"]]:
        name = str(entry.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        try:
            source = cited[int(str(entry.get("source", "1")).strip()) - 1]
        except (ValueError, IndexError):
            source = cited[0]
        notes.append({
            "question": subject,
            "claim": name,
            "detail": str(entry.get("detail", "")).strip(),
            "confidence": "high",
            "sources": [source["url"]],
            "score": source.get("score", 0),
            "kind": "web",
            "url": source["url"],
        })
    return notes


async def _attach_logos(notes: list[dict], item, http, settings) -> None:
    """Fetch each owner's avatar. Best effort — a missing logo is not an error."""
    target = Path(settings.media_dir) / "logos" / str(item.id)
    for note in notes:
        owner = note.get("owner")
        if not owner:
            continue
        path = await download_avatar(
            http, f"https://github.com/{owner}.png?size=200", target
        )
        if path:
            note["logo"] = str(path)


def _confidence(kept: list[dict], wanted: int) -> int:
    """How much of a post this actually is."""
    if not kept:
        return 0
    coverage = min(len(kept) / max(wanted, 1), 1.0)
    quality = sum(c["score"] for c in kept) / (len(kept) * 100)
    # Coverage dominates: four excellent items still do not answer "give me ten".
    return int((coverage * 0.65 + quality * 0.35) * 100)


def _question(kept: list[dict], plan: dict, seen: int) -> str:
    """Say what was actually found, and ask something answerable."""
    if not kept:
        return (
            f"I searched for “{plan['subject']}” and found nothing worth posting "
            f"({seen} candidates, none above the quality bar).\n\n"
            "Reply with a better search term, a specific source to look at, "
            "say \u201cpost what you have\u201d to continue with these, or "
            "/drop."
        )
    names = ", ".join(
        urlparse(c["url"]).path.strip("/") for c in kept[:3]
    )
    return (
        f"I only found {len(kept)} solid item(s) for “{plan['subject']}”, "
        f"not the {plan['count']} you asked for.\n\nBest so far: {names}\n\n"
        "Reply with a better search term to widen it, /post to send what I "
        "have for approval, or /drop."
    )
