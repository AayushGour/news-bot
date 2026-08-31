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

Score 0-10: how much does this item deserve a researched carousel post?

High (7-10): a concrete, checkable claim about a product launch, model release,
acquisition, funding round, outage, policy change, or benchmark result. Something
a reader could learn from and a researcher could verify.

Middle (4-6): real news but thin — an announcement with no detail, or a story
only interesting to a narrow audience.

Low (0-3): chatter, greetings, memes, subscriber appeals, giveaways, adverts,
job posts, pure opinion with no claim, or a link with no context.

Judge the substance, not the writing quality. Reply with score, a one-sentence
reason, and a short topic label."""

MIN_LENGTH = 40


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

    text = (item.raw_text or "").strip()

    if len(text) < MIN_LENGTH and not item.raw_media_paths:
        return {
            "triage_score": 0,
            "triage_reason": f"too short ({len(text)} chars) and no media",
            "_next": Status.DROPPED,
        }

    # News channels repost the same story with trivial edits. Catching that here
    # costs one indexed query instead of a full research run.
    if await db.seen_hash_recently(text, exclude_id=item.id):
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
