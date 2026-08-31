"""SQLite persistence. The ``status`` column is the state machine.

Every stage transition writes the new field values and the new status in a
single transaction, alongside an ``events`` row. That is what makes the pipeline
resumable: kill the process at any moment and the database describes exactly
where each item stands.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .models import Item, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  source            TEXT    NOT NULL,
  source_chat_id    INTEGER,
  source_msg_id     INTEGER,
  created_at        TEXT    NOT NULL,

  raw_text          TEXT    DEFAULT '',
  raw_media_paths   TEXT    DEFAULT '[]',

  status            TEXT    NOT NULL,
  status_updated_at TEXT    NOT NULL,
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TEXT,
  last_error        TEXT,

  triage_score      INTEGER,
  triage_reason     TEXT,
  text_hash         TEXT,

  extracted         TEXT    DEFAULT '{}',
  research          TEXT    DEFAULT '[]',
  brief             TEXT,
  slides            TEXT    DEFAULT '[]',
  caption           TEXT,
  regen_note        TEXT,

  rendered_paths    TEXT    DEFAULT '[]',
  media_urls        TEXT    DEFAULT '[]',

  approval_msg_id   INTEGER,
  ig_child_ids      TEXT    DEFAULT '[]',
  ig_carousel_id    TEXT,
  ig_post_id        TEXT,
  published_at      TEXT,

  UNIQUE(source_chat_id, source_msg_id)
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  from_status TEXT,
  to_status   TEXT,
  at          TEXT NOT NULL,
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_items_hash   ON items(text_hash, created_at);
"""

#: Columns stored as JSON text, decoded on read and encoded on write.
JSON_COLUMNS = {
    "raw_media_paths",
    "extracted",
    "research",
    "slides",
    "rendered_paths",
    "media_urls",
    "ig_child_ids",
}

_ITEM_FIELDS = {
    "id", "source", "status", "source_chat_id", "source_msg_id", "created_at",
    "raw_text", "raw_media_paths", "attempts", "next_attempt_at", "last_error",
    "triage_score", "triage_reason", "extracted", "research", "brief", "slides",
    "caption", "regen_note", "rendered_paths", "media_urls", "approval_msg_id",
    "ig_child_ids", "ig_carousel_id", "ig_post_id", "published_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_hash(text: str) -> str:
    """Normalised hash for near-duplicate detection.

    News channels repost the same story with trivial edits; collapsing
    whitespace, case, and punctuation catches most of that cheaply.
    """
    normalised = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.sha256(normalised.encode()).hexdigest()


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> Database:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._conn

    # ---------------------------------------------------------------- writes

    async def insert_item(
        self,
        *,
        source: str,
        source_chat_id: int | None,
        source_msg_id: int | None,
        raw_text: str = "",
        raw_media_paths: list[str] | None = None,
    ) -> int | None:
        """Insert a new item, or return ``None`` if it already exists.

        The ``UNIQUE(source_chat_id, source_msg_id)`` constraint is the
        idempotency guarantee that makes boot-time backfill and restart replay
        safe to run unconditionally.
        """
        try:
            cur = await self.conn.execute(
                """INSERT INTO items (source, source_chat_id, source_msg_id, created_at,
                                      raw_text, raw_media_paths, status, status_updated_at,
                                      text_hash)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    source, source_chat_id, source_msg_id, now_iso(),
                    raw_text, json.dumps(raw_media_paths or []),
                    Status.INGESTED.value, now_iso(), text_hash(raw_text),
                ),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def transition(
        self, item_id: int, to_status: Status, fields: dict | None = None,
        detail: str | None = None,
    ) -> None:
        """Advance an item and persist stage output atomically."""
        fields = dict(fields or {})
        fields.pop("_next", None)

        row = await self.conn.execute_fetchall(
            "SELECT status FROM items WHERE id=?", (item_id,)
        )
        from_status = row[0]["status"] if row else None

        sets, values = [], []
        for key, value in fields.items():
            if key not in _ITEM_FIELDS:
                raise KeyError(f"unknown item column: {key}")
            sets.append(f"{key}=?")
            values.append(json.dumps(value) if key in JSON_COLUMNS else value)

        sets += ["status=?", "status_updated_at=?", "attempts=0",
                 "next_attempt_at=NULL", "last_error=NULL"]
        values += [str(to_status), now_iso()]

        await self.conn.execute(
            f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*values, item_id)
        )
        await self.conn.execute(
            "INSERT INTO events (item_id, from_status, to_status, at, detail) VALUES (?,?,?,?,?)",
            (item_id, from_status, str(to_status), now_iso(), detail),
        )
        await self.conn.commit()

    async def record_failure(
        self, item_id: int, error: str, *, terminal: bool = False, max_attempts: int = 3
    ) -> Status:
        """Record a stage failure, applying backoff or giving up.

        Returns the status the item ended in, so callers can log meaningfully.
        """
        rows = await self.conn.execute_fetchall(
            "SELECT status, attempts FROM items WHERE id=?", (item_id,)
        )
        attempts = (rows[0]["attempts"] if rows else 0) + 1
        from_status = rows[0]["status"] if rows else None

        if terminal or attempts >= max_attempts:
            await self.conn.execute(
                """UPDATE items SET status=?, status_updated_at=?, attempts=?,
                                    last_error=?, next_attempt_at=NULL WHERE id=?""",
                (Status.FAILED.value, now_iso(), attempts, error[:2000], item_id),
            )
            await self.conn.execute(
                "INSERT INTO events (item_id, from_status, to_status, at, detail) VALUES (?,?,?,?,?)",
                (item_id, from_status, Status.FAILED.value, now_iso(), error[:2000]),
            )
            await self.conn.commit()
            return Status.FAILED

        delay = timedelta(minutes=2 ** (attempts - 1))
        await self.conn.execute(
            """UPDATE items SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?""",
            (attempts, error[:2000],
             (datetime.now(timezone.utc) + delay).isoformat(), item_id),
        )
        await self.conn.commit()
        return Status(from_status) if from_status else Status.INGESTED

    async def defer(self, item_id: int, seconds: int, error: str) -> None:
        """Back an item off **without** counting an attempt against it.

        Used when the infrastructure is down rather than the item being bad —
        Ollama being unreachable should not eventually mark items failed.
        """
        await self.conn.execute(
            "UPDATE items SET next_attempt_at=?, last_error=? WHERE id=?",
            ((datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
             error[:2000], item_id),
        )
        await self.conn.commit()

    async def clear_backoff(self, item_id: int) -> None:
        """Make an item immediately claimable again (used by tests and retries)."""
        await self.conn.execute(
            "UPDATE items SET next_attempt_at=NULL WHERE id=?", (item_id,)
        )
        await self.conn.commit()

    # ----------------------------------------------------------------- reads

    async def get_item(self, item_id: int) -> Item | None:
        rows = await self.conn.execute_fetchall("SELECT * FROM items WHERE id=?", (item_id,))
        return self._to_item(rows[0]) if rows else None

    async def claim_items(
        self, statuses: list[Status] | tuple[Status, ...], limit: int = 5
    ) -> list[Item]:
        """Fetch items due for work.

        Items in backoff (``next_attempt_at`` in the future) are excluded, which
        is what stops a failing stage from being retried in a tight loop.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        rows = await self.conn.execute_fetchall(
            f"""SELECT * FROM items
                 WHERE status IN ({placeholders})
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 ORDER BY id LIMIT ?""",
            (*[str(s) for s in statuses], now_iso(), limit),
        )
        return [self._to_item(r) for r in rows]

    async def list_by_status(self, status: Status, limit: int = 100) -> list[Item]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM items WHERE status=? ORDER BY id LIMIT ?", (str(status), limit)
        )
        return [self._to_item(r) for r in rows]

    async def events_for(self, item_id: int) -> list[dict]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [dict(r) for r in rows]

    async def seen_hash_recently(
        self, text: str, hours: int = 48, exclude_id: int | None = None
    ) -> bool:
        """Has equivalent text been seen recently?

        ``exclude_id`` must be passed when checking an item that is already
        stored, otherwise it matches its own row and every item looks like a
        duplicate of itself.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self.conn.execute_fetchall(
            "SELECT 1 FROM items WHERE text_hash=? AND created_at>=? AND id IS NOT ? LIMIT 1",
            (text_hash(text), cutoff, exclude_id),
        )
        return bool(rows)

    async def max_source_msg_id(self, chat_id: int) -> int:
        rows = await self.conn.execute_fetchall(
            "SELECT MAX(source_msg_id) AS m FROM items WHERE source_chat_id=?", (chat_id,)
        )
        return (rows[0]["m"] or 0) if rows else 0

    async def published_since(self, hours: int = 24) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self.conn.execute_fetchall(
            "SELECT COUNT(*) AS c FROM items WHERE published_at>=?", (cutoff,)
        )
        return rows[0]["c"] if rows else 0

    # ---------------------------------------------------------------- mapping

    @staticmethod
    def _to_item(row: aiosqlite.Row) -> Item:
        data = {}
        for key in row.keys():
            if key not in _ITEM_FIELDS:
                continue
            value = row[key]
            if key in JSON_COLUMNS:
                try:
                    value = json.loads(value) if value else ([] if key != "extracted" else {})
                except (TypeError, json.JSONDecodeError):
                    value = [] if key != "extracted" else {}
            data[key] = value
        data["status"] = Status(data["status"])
        data["raw_text"] = data.get("raw_text") or ""
        return Item(**data)
