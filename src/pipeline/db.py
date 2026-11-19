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

from .lease import host_fence
from .models import Item, Status
from .storage import require_local_fs

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
  priority          INTEGER NOT NULL DEFAULT 0,
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TEXT,
  last_error        TEXT,

  triage_score      INTEGER,
  triage_reason     TEXT,
  text_hash         TEXT,
  -- 1 when the newest DM fragment sat exactly on a Telegram length ceiling, i.e.
  -- more of the same message may follow (T364). Gates continuation.
  at_ceiling        INTEGER NOT NULL DEFAULT 0,

  extracted         TEXT    DEFAULT '{}',
  research          TEXT    DEFAULT '[]',
  clauses           TEXT    DEFAULT '[]',
  -- Clauses of the request that research could NOT answer. Stored rather than
  -- only written into the operator's question, so compose can be told what is
  -- missing instead of discovering a hole and inventing a thesis to fill it.
  gaps              TEXT    DEFAULT '[]',
  brief             TEXT,
  slides            TEXT    DEFAULT '[]',
  caption           TEXT,
  regen_note        TEXT,
  theme             TEXT,
  intent            TEXT,
  question          TEXT,
  question_msg_id   INTEGER,
  answer            TEXT,
  confidence        INTEGER,
  resume_status     TEXT,

  rendered_paths    TEXT    DEFAULT '[]',
  media_urls        TEXT    DEFAULT '[]',

  verification      TEXT,
  integrity         TEXT,
  revision_request  TEXT,
  revision          TEXT,
  theme_pin         TEXT,
  -- The theme the deck was actually RENDERED with (T358). items.theme is only the
  -- composer's suggestion; this is what shipped, so the next post can avoid it.
  rendered_theme    TEXT,

  approval_msg_id   INTEGER,
  ig_child_ids      TEXT    DEFAULT '[]',
  ig_carousel_id    TEXT,
  ig_post_id        TEXT,
  published_at      TEXT,
  -- Every carousel this item has ever put on the account. Unlike ig_post_id,
  -- which a requeue must clear so a redo can publish, nothing clears this.
  publish_log       TEXT    DEFAULT '[]',

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

CREATE TABLE IF NOT EXISTS messages (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  role    TEXT    NOT NULL,   -- 'pipeline' | 'operator'
  text    TEXT    NOT NULL,
  surface TEXT,               -- 'telegram' | 'dashboard'
  at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_item ON messages(item_id, id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_items_hash   ON items(text_hash, created_at);
"""

#: Columns stored as JSON text, decoded on read and encoded on write.
JSON_COLUMNS = {
    "raw_media_paths",
    "extracted",
    "research",
    "clauses",
    "gaps",
    "slides",
    "rendered_paths",
    "media_urls",
    "ig_child_ids",
    "publish_log",
    "verification",
    "integrity",
    "revision_request",
    "revision",
}

#: Stored as INTEGER, exposed as bool. SQLite has no boolean, so without
#: this a field declared bool on the model comes back as 0/1.
BOOL_COLUMNS: set[str] = set()

#: JSON columns that must store SQL NULL when Python None is written,
#: not the string "null" (T234 trap).
NULLABLE_JSON_COLUMNS = {
    "verification",
    "integrity",
    "revision_request",
    "revision",
}


def _encode(key: str, value) -> str | int | None:
    """Encode a field value for SQL storage.

    - Nullable JSON columns: None → SQL NULL; otherwise json.dumps
    - Other JSON columns: json.dumps
    - Non-JSON: returned as-is
    """
    if key in NULLABLE_JSON_COLUMNS and value is None:
        return None
    if key in JSON_COLUMNS:
        return json.dumps(value)
    return value


_ITEM_FIELDS = {
    "id", "source", "status", "source_chat_id", "source_msg_id", "created_at",
    "raw_text", "raw_media_paths", "status_updated_at",
    "priority", "attempts", "next_attempt_at", "last_error",
    "triage_score", "triage_reason", "extracted", "research", "clauses",
    "gaps",
    "brief", "slides",
    "caption", "regen_note", "theme", "intent", "question", "question_msg_id",
    "answer",
    "confidence", "resume_status",
    "rendered_paths", "media_urls", "approval_msg_id",
    "verification", "integrity", "revision_request", "revision", "theme_pin",
    "rendered_theme",
    "ig_child_ids", "ig_carousel_id", "ig_post_id", "published_at",
    "publish_log",
}


#: Statuses short of research, where a later fragment can still be folded in.
_CONTINUABLE_STATUSES = (
    Status.INGESTED.value, Status.EXTRACTED.value, Status.TRIAGED.value,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: A markdown link, as Telethon renders a channel post's links: ``[text](url)``.
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)\s]*\)")

#: Text shorter than this is too generic to call a copy of anything.
MIN_COPY_CHARS = 40


def text_hash(text: str) -> str:
    """Normalised hash for near-duplicate detection.

    News channels repost the same story with trivial edits; collapsing
    whitespace, case, and punctuation catches most of that cheaply. Links are
    unwrapped to their text first: a channel post arrives as markdown and the
    same post forwarded to the bot as plain text (#790/#791).
    """
    normalised = _MD_LINK.sub(r"\1", text or "")
    normalised = re.sub(r"[^a-z0-9 ]+", "", normalised.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.sha256(normalised.encode()).hexdigest()


class Database:
    def __init__(self, path: str | Path, *, lease_path: Path | None = None) -> None:
        self.path = Path(path)
        self._lease_path = lease_path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> Database:
        # T348/D298: checked before anything is created or opened. In a container
        # the file must be on a local fs; on the host, a container's fresh lease
        # means the container owns the database.
        require_local_fs(self.path, "the database")
        host_fence(self._lease_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        return self

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently skips an existing table, so new
        columns never appear on a live database without this.
        """
        rows = await self._conn.execute_fetchall("PRAGMA table_info(items)")
        existing = {r["name"] for r in rows}
        for column, ddl in [
            ("theme", "TEXT"), ("intent", "TEXT"), ("question", "TEXT"),
            ("answer", "TEXT"), ("confidence", "INTEGER"),
            ("resume_status", "TEXT"), ("clauses", "TEXT"),
            ("question_msg_id", "INTEGER"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("publish_log", "TEXT DEFAULT '[]'"),
            ("gaps", "TEXT DEFAULT '[]'"),
            ("verification", "TEXT"),
            ("integrity", "TEXT"),
            ("revision_request", "TEXT"),
            ("revision", "TEXT"),
            ("theme_pin", "TEXT"),
            ("rendered_theme", "TEXT"),
            ("at_ceiling", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if column not in existing:
                await self._conn.execute(f"ALTER TABLE items ADD COLUMN {column} {ddl}")

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
        at_ceiling: bool = False,
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
                                      text_hash, at_ceiling)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    source, source_chat_id, source_msg_id, now_iso(),
                    raw_text, json.dumps(raw_media_paths or []),
                    Status.INGESTED.value, now_iso(), text_hash(raw_text),
                    int(at_ceiling),
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
            values.append(_encode(key, value))

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

    async def transition_if(
        self,
        item_id: int,
        expected_status: Status,
        expected_updated_at: str | None,
        to_status: Status,
        fields: dict | None = None,
        detail: str | None = None,
    ) -> bool:
        """``transition`` that only fires if the item is still where the caller saw it.

        A background job (retention) decides from a snapshot; the operator may
        approve in between. The guard is status AND status_updated_at, because
        a regen cycle can bring an item back to the same status with a fresh
        clock, and that is a different item-state than the one judged. One
        UPDATE ... WHERE is the atomic check-and-set; the event row is written
        only when it won. Unlike ``transition``, ``fields`` may override the
        cleared defaults (``last_error`` carries the reason).
        """
        assign: dict = {
            "status_updated_at": now_iso(),
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
        }
        for key, value in (fields or {}).items():
            if key not in _ITEM_FIELDS:
                raise KeyError(f"unknown item column: {key}")
            assign[key] = _encode(key, value)
        assign["status"] = str(to_status)
        sets = ", ".join(f"{k}=?" for k in assign)
        cur = await self.conn.execute(
            f"UPDATE items SET {sets} WHERE id=? AND status=? AND status_updated_at IS ?",
            (*assign.values(), item_id, str(expected_status), expected_updated_at),
        )
        if cur.rowcount != 1:
            # Lost the race: the UPDATE matched nothing, so there is nothing to
            # undo. Never rollback() here - the connection is shared with the
            # worker, and a rollback would discard its uncommitted writes.
            return False
        await self.conn.execute(
            "INSERT INTO events (item_id, from_status, to_status, at, detail) VALUES (?,?,?,?,?)",
            (item_id, str(expected_status), str(to_status), now_iso(), detail),
        )
        await self.conn.commit()
        return True

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

    async def update_fields(self, item_id: int, fields: dict) -> None:
        """Persist field values without changing status.

        Publishing uses this to save Instagram container ids the instant they
        are created, so a retry reuses them instead of creating a second post.
        """
        if not fields:
            return
        sets, values = [], []
        for key, value in fields.items():
            if key not in _ITEM_FIELDS:
                raise KeyError(f"unknown item column: {key}")
            sets.append(f"{key}=?")
            values.append(_encode(key, value))
        await self.conn.execute(
            f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*values, item_id)
        )
        await self.conn.commit()

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

        Highest priority first, then oldest. Priority defaults to 0, so a queue
        nobody has touched behaves exactly as it did before — strictly
        oldest-first — and one bumped item jumps the whole line without
        reordering anything else.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        rows = await self.conn.execute_fetchall(
            f"""SELECT * FROM items
                 WHERE status IN ({placeholders})
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 ORDER BY priority DESC, id LIMIT ?""",
            (*[str(s) for s in statuses], now_iso(), limit),
        )
        return [self._to_item(r) for r in rows]

    async def list_by_status(self, status: Status, limit: int = 100) -> list[Item]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM items WHERE status=? ORDER BY id LIMIT ?", (str(status), limit)
        )
        return [self._to_item(r) for r in rows]

    async def page_by_statuses(self, statuses, limit: int, offset: int) -> list[Item]:
        """Newest first, for the bot's /status pages."""
        placeholders = ",".join("?" * len(statuses))
        rows = await self.conn.execute_fetchall(
            f"SELECT * FROM items WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ? OFFSET ?",
            (*[str(s) for s in statuses], limit, offset),
        )
        return [self._to_item(r) for r in rows]

    async def set_priority(self, item_id: int, priority: int) -> None:
        """Move an item up or down the queue. Higher runs sooner."""
        await self.conn.execute(
            "UPDATE items SET priority=? WHERE id=?", (int(priority), item_id)
        )
        await self.conn.commit()

    async def queue_order(self, statuses) -> list[Item]:
        """Every claimable item, in the order the worker will actually take it.

        The same ordering as ``claim_items`` but unpaginated and including
        items in backoff, because "why is this not running" is exactly the
        question the list is there to answer — hiding them would make the queue
        disagree with itself.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        rows = await self.conn.execute_fetchall(
            f"""SELECT * FROM items WHERE status IN ({placeholders})
                 ORDER BY priority DESC, id""",
            tuple(str(s) for s in statuses),
        )
        return [self._to_item(r) for r in rows]

    async def reorder(self, ordered_ids: list[int]) -> None:
        """Make this exact sequence the execution order.

        Priorities are rewritten as one descending run rather than nudged, so
        the list on screen and the order the worker takes are the same thing.
        Nudging a single value leaves ties, and a tie means the displayed order
        and the real one quietly disagree.
        """
        top = len(ordered_ids)
        await self.conn.executemany(
            "UPDATE items SET priority=? WHERE id=?",
            [(top - n, item_id) for n, item_id in enumerate(ordered_ids)],
        )
        await self.conn.commit()

    async def already_ingested(self, chat_id: int | None, msg_id: int | None) -> bool:
        """Has this exact Telegram message already become an item?

        Checked before a continuation: a redelivered message carries the same
        id and is a duplicate, not a second half.
        """
        if chat_id is None or msg_id is None:
            return False
        rows = await self.conn.execute_fetchall(
            "SELECT 1 FROM items WHERE source_chat_id=? AND source_msg_id=? LIMIT 1",
            (chat_id, msg_id),
        )
        return bool(rows)

    async def continuable_dm(self, chat_id: int | None, within_s: int) -> Item | None:
        """The operator's last DM, if it is recent, unresearched and cut at a ceiling.

        Telegram splits a message over 4096 characters into several, each
        arriving separately. Only an item whose newest fragment sat exactly on
        a Telegram length ceiling can have more coming; any other message is its
        own request (T364), so back-to-back normal DMs never merge.
        """
        if chat_id is None:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_s)).isoformat()
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM items WHERE source='dm' AND source_chat_id=?"
            "   AND created_at >= ? AND at_ceiling=1 AND status IN (?,?,?)"
            " ORDER BY id DESC LIMIT 1",
            (chat_id, cutoff, *_CONTINUABLE_STATUSES),
        )
        return self._to_item(rows[0]) if rows else None

    async def append_raw_text(
        self, item_id: int, extra: str, *, at_ceiling: bool = False,
    ) -> bool:
        """Add a continuation to an item that has not been researched yet.

        ``at_ceiling`` says whether *this* fragment is itself cut at a ceiling,
        so a third part can still join. Returns False, changing nothing, if the
        item is missing or has moved past TRIAGED since it was looked up.
        """
        rows = await self.conn.execute_fetchall(
            "SELECT raw_text FROM items WHERE id=?", (item_id,)
        )
        if not rows:
            return False
        joined = f"{rows[0]['raw_text'] or ''}\n\n{extra}".strip()
        cur = await self.conn.execute(
            "UPDATE items SET raw_text=?, text_hash=?, status=?,"
            " status_updated_at=?, attempts=0, at_ceiling=? WHERE id=?"
            " AND status IN (?,?,?)",
            (joined, text_hash(joined), Status.INGESTED.value, now_iso(),
             int(at_ceiling), item_id, *_CONTINUABLE_STATUSES),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_message(
        self, item_id: int, role: str, text: str, surface: str = "",
    ) -> None:
        """Append to an item's conversation.

        One thread per item, whichever surface it arrived on — a question asked
        in Telegram and answered in the dashboard is the same exchange, and
        splitting them by surface would show each side half of it.
        """
        await self.conn.execute(
            "INSERT INTO messages (item_id, role, text, surface, at)"
            " VALUES (?,?,?,?,?)",
            (item_id, role, text, surface, now_iso()),
        )
        await self.conn.commit()

    async def messages_for(self, item_id: int) -> list[dict]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM messages WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [dict(r) for r in rows]

    async def status_counts(self) -> dict[str, int]:
        rows = await self.conn.execute_fetchall(
            "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}

    async def events_for(self, item_id: int) -> list[dict]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [dict(r) for r in rows]

    async def recent_copy(
        self, text: str, hours: int = 48, exclude_id: int | None = None,
        ignore: tuple[Status, ...] = (),
    ) -> Item | None:
        """The newest item with equivalent text from the last ``hours``, or None.

        ``exclude_id`` must be passed when checking an item that is already
        stored, otherwise it matches its own row and every item looks like a
        duplicate of itself. Items in an ``ignore`` status do not count.
        """
        if len((text or "").strip()) < MIN_COPY_CHARS:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        skip = [str(s) for s in ignore]
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM items WHERE text_hash=? AND created_at>=? AND id IS NOT ?"
            f" AND status NOT IN ({','.join('?' * len(skip))}) ORDER BY id DESC LIMIT 1",
            (text_hash(text), cutoff, exclude_id, *skip),
        )
        return self._to_item(rows[0]) if rows else None

    async def max_source_msg_id(self, chat_id: int) -> int:
        rows = await self.conn.execute_fetchall(
            "SELECT MAX(source_msg_id) AS m FROM items WHERE source_chat_id=?", (chat_id,)
        )
        return (rows[0]["m"] or 0) if rows else 0

    async def status_counts_since(self, hours: int = 24) -> dict[str, int]:
        """Status histogram over recently created items, for the daily digest."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self.conn.execute_fetchall(
            "SELECT status, COUNT(*) AS c FROM items WHERE created_at>=? GROUP BY status",
            (cutoff,),
        )
        return {r["status"]: r["c"] for r in rows}

    async def dropped_reasons_since(self, hours: int = 24, limit: int = 10) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self.conn.execute_fetchall(
            """SELECT triage_reason FROM items
                WHERE status=? AND created_at>=? AND triage_reason IS NOT NULL
                ORDER BY id DESC LIMIT ?""",
            (Status.DROPPED.value, cutoff, limit),
        )
        return [r["triage_reason"] for r in rows]

    async def published_since(self, hours: int = 24) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self.conn.execute_fetchall(
            "SELECT COUNT(*) AS c FROM items WHERE published_at>=?", (cutoff,)
        )
        return rows[0]["c"] if rows else 0

    async def last_published_theme(self, exclude_id: int | None = None) -> str | None:
        """Theme the most recently published item was rendered with (T358).

        None when nothing has been published, or when that post predates the
        ``rendered_theme`` column (nothing to avoid then). ``exclude_id`` keeps an
        item from being compared with its own earlier publication.
        """
        rows = await self.conn.execute_fetchall(
            """SELECT rendered_theme FROM items
                WHERE published_at IS NOT NULL AND id != ?
                ORDER BY published_at DESC, id DESC LIMIT 1""",
            (-1 if exclude_id is None else exclude_id,),
        )
        return (rows[0]["rendered_theme"] or None) if rows else None

    async def last_published_at(self) -> datetime | None:
        """When the most recent item was published, or None if never."""
        rows = await self.conn.execute_fetchall(
            "SELECT MAX(published_at) AS m FROM items WHERE published_at IS NOT NULL"
        )
        value = rows[0]["m"] if rows else None
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

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
                    if value:
                        value = json.loads(value)
                    elif key in NULLABLE_JSON_COLUMNS:
                        value = None
                    elif key == "extracted":
                        value = {}
                    else:
                        value = []
                except (TypeError, json.JSONDecodeError):
                    value = (
                        None
                        if key in NULLABLE_JSON_COLUMNS
                        else ([] if key != "extracted" else {})
                    )
            data[key] = value
        data["status"] = Status(data["status"])
        data["raw_text"] = data.get("raw_text") or ""
        # SQLite has no boolean, so a flag declared bool on the model comes
        # back as 0/1. Coerce at the boundary rather than leaving every caller
        # to remember that a flag can be truthy while `is True` is false.
        for flag in BOOL_COLUMNS:
            if flag in data:
                data[flag] = bool(data[flag])
        return Item(**data)
