"""Queue a request without going through Telegram.

The bot is the normal way in. This is the same thing typed at a terminal or in
the dashboard, and it exists because both need it — a second implementation
would drift from the first, and the id-allocation rule below is exactly the
kind of detail that drifts silently.
"""

from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger(__name__)

#: Below this a request has nothing to research, matching the DM intake rule.
#: Research always finds *something*, so a bare prompt yields a confident,
#: sourced-looking post about whatever it stumbled across.
MIN_REQUEST_CHARS = 15

TOO_THIN = (
    "That's too short to research — it would end up inventing a story around "
    "it. Send a headline, a link, a paragraph, or an image."
)


class TooThin(ValueError):
    """The request has nothing in it worth researching."""


async def queue_request(
    db: Database, settings, text: str, source: str = "dm",
) -> int:
    """Insert a hand-written request and return its item id.

    Telegram message ids are positive and increasing, so hand-queued items take
    negative ones. That keeps them in their own range where they can never
    collide with a message the listener backfills later — the UNIQUE constraint
    on (chat, message) is what makes backfill safe to re-run, and a collision
    would silently drop either this request or a real message.
    """
    text = (text or "").strip()
    if len(text) < MIN_REQUEST_CHARS:
        raise TooThin(TOO_THIN)

    row = await db.conn.execute_fetchall(
        "SELECT MIN(source_msg_id) AS lowest FROM items WHERE source_msg_id < 0"
    )
    lowest = row[0]["lowest"] if row and row[0]["lowest"] is not None else 0

    item_id = await db.insert_item(
        source=source,
        source_chat_id=settings.operator_user_id,
        source_msg_id=lowest - 1,
        raw_text=text,
    )
    if item_id is None:  # pragma: no cover — the id range makes this unreachable
        raise RuntimeError("could not allocate an id for the request")

    log.info("queued item %s (%s, %d chars)", item_id, source, len(text))
    return item_id
