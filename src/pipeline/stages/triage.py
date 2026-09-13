"""Triage: decide whether an item is worth researching at all.

The source channel posts a lot that is not worth a carousel — one-liners,
promos, and the same story three times. Research is by far the most expensive
stage, so the gate goes in front of it.
"""

from __future__ import annotations

from ..db import Database
from ..models import Item, Status

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"},
        "topic": {"type": "string"},
    },
    "required": ["score", "reason", "topic"],
}

SYSTEM = """You screen tech and AI news for an Instagram publisher.

Score 0-10: is there a specific, researchable story here?

You are NOT judging whether the claim is true, sourced, or already proven.
Everything you see arrives unverified — that is normal for a news channel, and a
later research stage checks it against the open web. Assessing credibility is
not your job here. Never lower a score because no source, link, or official
announcement is attached; that describes almost every item worth posting.

Ask one question: does this name something specific enough to go and research?

High (7-10): names a product, company, model, number, date, place, or event that
a researcher could look up. "Uber deploys 20 Wayve-powered cars in London"
scores high with no source attached at all, because it is entirely researchable.

Middle (4-6): real but vague. An announcement with no specifics, or something
only a narrow audience would care about.

Low (0-3): nothing to research. Greetings, memes, subscriber appeals, giveaways,
adverts, job posts, pure opinion with no claim, or a decorative image that shows
no story.

Judge substance, not writing quality and not sourcing. Reply with score, a
one-sentence reason, and a short topic label."""

MIN_LENGTH = 40


def judgeable_text(item: Item) -> str:
    """Everything triage can actually read, not just the message body.

    Extraction runs *before* triage precisely so this can include what a
    screenshot said. Many channels post an image with no caption at all; judging
    those on body text alone drops real stories and reports it as a quiet
    channel.
    """
    parts = [(item.raw_text or "").strip()]
    extracted = item.extracted or {}

    for described in extracted.get("image_descriptions", []):
        if described.get("description"):
            parts.append(f"[image] {described['description']}")
    for page in extracted.get("url_texts", []):
        if page.get("text"):
            parts.append(f"[link {page.get('url', '')}]\n{page['text']}")

    return "\n\n".join(part for part in parts if part.strip())


async def triage(
    item: Item, llm, db: Database, threshold: int = 6
) -> dict:
    """Score an item and route it onward or to ``DROPPED``."""

    # Operator-submitted items skip the gate entirely. If they sent it, they
    # want it — and spending a model call to second-guess them is pointless.
    if item.source == "dm":
        return {
            "triage_score": 10,
            "triage_reason": "operator submission",
            "_next": Status.TRIAGED,
        }

    body = (item.raw_text or "").strip()
    text = judgeable_text(item)

    if len(text) < MIN_LENGTH:
        return {
            "triage_score": 0,
            "triage_reason": (
                f"nothing to judge ({len(text)} chars after extraction)"
            ),
            "_next": Status.DROPPED,
        }

    # News channels repost the same story with trivial edits. Catching that here
    # costs one indexed query instead of a full research run. Only the body is
    # hashed — image descriptions vary run to run, so they cannot dedupe.
    if len(body) >= MIN_LENGTH and await db.seen_hash_recently(body, exclude_id=item.id):
        return {
            "triage_score": 0,
            "triage_reason": "near-duplicate of a recent item",
            "_next": Status.DROPPED,
        }

    verdict = await llm.cheap(SYSTEM, f"ITEM:\n{text}", schema=TRIAGE_SCHEMA)
    score = int(verdict.get("score", 0))
    reason = str(verdict.get("reason", ""))[:500]

    return {
        "triage_score": score,
        "triage_reason": reason,
        "_next": Status.TRIAGED if score >= threshold else Status.DROPPED,
    }
