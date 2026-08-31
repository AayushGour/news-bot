"""Daily digest and failure alerts.

The pipeline drops a lot of items at triage by design. Without a digest that is
invisible, and there is no way to tell "the channel was quiet" from "triage is
mis-tuned and threw away everything worth posting".
"""

from __future__ import annotations

import logging
from typing import Any

from .db import Database
from .models import Status

log = logging.getLogger(__name__)

FAILURE_ALERT = "❌ Item {id} failed after retries.\n\n{reason}\n\nSource text:\n{text}"


async def build_digest(db: Database, hours: int = 24) -> str:
    counts = await db.status_counts_since(hours)
    waiting = len(await db.list_by_status(Status.AWAITING_APPROVAL))
    published = await db.published_since(hours)

    total = sum(counts.values())
    dropped = counts.get(Status.DROPPED.value, 0)
    failed = counts.get(Status.FAILED.value, 0)

    lines = [
        f"📊 Last {hours}h",
        "",
        f"seen: {total}",
        f"dropped at triage: {dropped}",
        f"published: {published}",
        f"failed: {failed}",
        f"awaiting your approval: {waiting}",
    ]

    if dropped:
        reasons = await db.dropped_reasons_since(hours, limit=5)
        if reasons:
            lines += ["", "recent drops:"] + [f"· {r}" for r in reasons]

    if waiting:
        lines += ["", f"{waiting} preview(s) still need a decision."]

    return "\n".join(lines)


async def send_digest(bot: Any, db: Database, settings: Any, hours: int = 24) -> str:
    text = await build_digest(db, hours)
    if bot is not None:
        await bot.send_message(settings.operator_user_id, text)
    return text


def make_failure_notifier(bot: Any, settings: Any):
    """Build the ``on_failure`` callback the worker uses for terminal failures."""

    async def notify(item, reason: str) -> None:
        if bot is None:
            return
        text = FAILURE_ALERT.format(
            id=item.id, reason=reason[:600],
            text=(item.raw_text or "")[:300] or "(no text)",
        )
        await bot.send_message(settings.operator_user_id, text)

    return notify
