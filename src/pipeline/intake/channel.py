"""Telethon channel listener and boot-time backfill.

Telethon delivers live events only. Anything posted while the process was down
is simply never seen, so the listener backfills on boot using the highest
``source_msg_id`` already stored per channel. The ``UNIQUE`` constraint on
``(source_chat_id, source_msg_id)`` makes that replay safe to run every time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..db import Database

log = logging.getLogger(__name__)

#: Cap on how far back a single boot will reach. A process down for a week
#: should not wake up and research two hundred stale items.
BACKFILL_LIMIT = 20


def message_fields(message: Any) -> dict:
    """Extract the parts of a Telethon message the pipeline cares about."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        peer = getattr(message, "peer_id", None)
        chat_id = getattr(peer, "channel_id", None)
    return {
        "source_chat_id": chat_id,
        "source_msg_id": getattr(message, "id", None),
        "raw_text": (getattr(message, "text", None) or "").strip(),
    }


def is_usable(message: Any) -> bool:
    """Worth storing at all? Text or media; anything else is noise."""
    text = (getattr(message, "text", None) or "").strip()
    return bool(text) or getattr(message, "media", None) is not None


class ChannelListener:
    def __init__(self, db: Database, client: Any, settings: Any) -> None:
        self.db = db
        self.client = client
        self.settings = settings

    async def ingest(self, message: Any) -> int | None:
        if not is_usable(message):
            return None

        fields = message_fields(message)
        media_paths: list[str] = []

        if getattr(message, "media", None) is not None:
            target = Path(self.settings.media_dir) / "channel"
            target.mkdir(parents=True, exist_ok=True)
            try:
                path = await self.client.download_media(message, str(target))
                if path:
                    media_paths.append(str(path))
            except Exception as exc:
                # Media is a bonus; the text carries the story.
                log.warning("media download failed for %s: %s",
                            fields["source_msg_id"], exc)

        item_id = await self.db.insert_item(
            source="channel", raw_media_paths=media_paths, **fields
        )
        if item_id is not None:
            log.info("ingested channel item %s from %s",
                     item_id, fields["source_chat_id"])
        return item_id

    async def backfill(self, limit: int = BACKFILL_LIMIT) -> int:
        """Replay messages posted while the process was down."""
        total = 0
        for chat_id in self.settings.channel_ids:
            try:
                last_seen = await self.db.max_source_msg_id(chat_id)
                messages = await self.client.get_messages(chat_id, limit=limit)
            except Exception as exc:
                log.warning("backfill failed for %s: %s", chat_id, exc)
                continue

            # get_messages returns newest first; ingest oldest first so ids
            # ascend the way live delivery would have produced them.
            for message in reversed(list(messages or [])):
                if last_seen and (getattr(message, "id", 0) or 0) <= last_seen:
                    continue
                if await self.ingest(message) is not None:
                    total += 1

        if total:
            log.info("backfilled %d missed messages", total)
        return total

    def register(self) -> None:
        """Subscribe to live messages on the configured channels."""
        from telethon import events

        @self.client.on(events.NewMessage(chats=list(self.settings.channel_ids)))
        async def _on_new_message(event: Any) -> None:  # pragma: no cover - needs network
            try:
                await self.ingest(event.message)
            except Exception:
                log.exception("failed to ingest channel message")
