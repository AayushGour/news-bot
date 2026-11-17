"""Telethon channel listener and boot-time backfill.

Telethon delivers live events only. Anything posted while the process was down
is simply never seen, so the listener backfills on boot using the highest
``source_msg_id`` already stored per channel. The ``UNIQUE`` constraint on
``(source_chat_id, source_msg_id)`` makes that replay safe to run every time.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..db import Database

log = logging.getLogger(__name__)

#: Cap on how far back a single boot will reach. A process down for a week
#: should not wake up and research two hundred stale items.
BACKFILL_LIMIT = 20

#: Clips the vision model cannot read. A still from one stands in for it: items
#: 686-691 sent their .mp4 to vision, it failed, and the post lost its picture.
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
FRAME_TIMEOUT_S = 60


async def video_frame(video: Path) -> Path | None:
    """A representative still from ``video`` as a JPEG beside it, or None."""
    frame = video.with_suffix(".jpg")
    proc = await asyncio.create_subprocess_exec(
        # Scaled first: "thumbnail" holds 100 decoded frames, ~1.75 GB at 4K.
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(video),
        "-vf", "scale=1280:-2,thumbnail", "-frames:v", "1", "-q:v", "2", str(frame),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), FRAME_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        frame.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or not frame.is_file():
        log.warning("no frame from %s: %s", video.name, err.decode(errors="replace")[:200])
        frame.unlink(missing_ok=True)
        return None
    return frame


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
                if path and Path(path).suffix.lower() in VIDEO_SUFFIXES:
                    video = Path(path)
                    path = await video_frame(video)
                    video.unlink(missing_ok=True)  # nothing downstream uses the clip
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

    async def backfill(self, limit: int | None = None) -> int:
        """Replay messages posted while the process was down."""
        limit = limit if limit is not None else getattr(
            self.settings, "backfill_limit", BACKFILL_LIMIT)
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
