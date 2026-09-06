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
from ..models import Item, Status
from ..search import REPO_ENGINES, dedupe_by_path, searx

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
        "why": {"type": "string"},
    },
    "required": ["intent", "count", "subject", "queries", "why"],
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
    }


def score_candidate(candidate: dict) -> int:
    """0-100, from signals search already returned.

    Deliberately not a model call: this runs on every candidate, and a model
    asked to rate a repository it cannot see would be guessing from the same
    fields anyway.
    """
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

    seen: dict[str, dict] = {}
    for query in queries[:4]:
        try:
            results = await searx(
                http, settings.searxng_url, query,
                limit=20, engines=REPO_ENGINES,
            )
        except Exception as exc:
            log.warning("enumeration search failed for %r: %s", query[:50], exc)
            continue
        for candidate in dedupe_by_path(results, 20):
            key = urlparse(candidate["url"]).path.rstrip("/").lower()
            if key and key not in seen:
                candidate["score"] = score_candidate(candidate)
                seen[key] = candidate

    kept = sorted(
        (c for c in seen.values() if c["score"] >= MIN_ITEM_SCORE),
        key=lambda c: -c["score"],
    )[: plan["count"]]

    confidence = _confidence(kept, plan["count"])
    log.info(
        "item %s enumeration: %d candidates, %d kept, confidence %d",
        item.id, len(seen), len(kept), confidence,
    )

    if confidence < MIN_SET_CONFIDENCE:
        raise NeedsInput(
            _question(kept, plan, len(seen)),
            resume_status=Status.TRIAGED,
            confidence=confidence,
        )

    return {
        "intent": "list",
        "confidence": confidence,
        "research": [
            {
                "question": subject,
                "claim": (c.get("title") or c["url"]).strip(),
                "detail": (c.get("content") or "").strip(),
                "confidence": "high" if c["score"] >= 60 else "medium",
                "sources": [c["url"]],
                "score": c["score"],
                "stars": c.get("popularity"),
            }
            for c in kept
        ],
    }


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
            "Reply with a better search term, a specific source to look at, or "
            "/drop."
        )
    names = ", ".join(
        urlparse(c["url"]).path.strip("/") for c in kept[:3]
    )
    return (
        f"I only found {len(kept)} solid item(s) for “{plan['subject']}”, "
        f"not the {plan['count']} you asked for.\n\nBest so far: {names}\n\n"
        "Reply with a better search term to widen it, say “post what you have” "
        "to continue with these, or /drop."
    )
