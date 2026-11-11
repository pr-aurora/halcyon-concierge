"""SQLite persistence for conversations, members, their memory and bookings.

All access goes through one connection guarded by a lock. The workload is small,
so this keeps the code simple and makes writes safe across threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .timeutil import utc_iso

FACT_CATEGORIES = (
    "preferred_name",
    "dietary",
    "likes",
    "dislikes",
    "neighbourhood",
    "occasion",
    "party",
    "general",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    value_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (member_id, category, value_key)
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member', 'assistant')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, seq);
CREATE INDEX IF NOT EXISTS messages_member ON messages (member_id, seq);
CREATE TABLE IF NOT EXISTS bookings (
    reference TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    venue_name TEXT NOT NULL,
    on_date TEXT NOT NULL,
    at_time TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_member ON bookings (member_id, on_date);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredMessage:
    seq: int
    id: str
    conversation_id: str
    member_id: str
    role: str
    text: str
    created_at: str


@dataclass(frozen=True)
class BookingRecord:
    reference: str
    conversation_id: str
    venue_id: str
    venue_name: str
    on_date: str
    at_time: str
    party_size: int
    status: str


class Store:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._lock = threading.RLock()

    # Messages ------------------------------------------------------------

    def add_member_message(
        self,
        *,
        conversation_id: str,
        message_id: str,
        member_id: str,
        member_name: str,
        text: str,
    ) -> StoredMessage | None:
        """Store an inbound member message. Returns None if this message_id was already seen."""
        now = utc_iso()
        with self._lock:
            self._upsert_member(member_id, member_name, now)
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO messages
                   (id, conversation_id, member_id, role, text, created_at)
                   VALUES (?, ?, ?, 'member', ?, ?)""",
                (message_id, conversation_id, member_id, text, now),
            )
            self._db.commit()
            if cursor.rowcount == 0:
                return None
            return self._message_by_id(message_id)

    def add_assistant_message(self, *, conversation_id: str, member_id: str, text: str) -> StoredMessage:
        message_id = str(uuid.uuid4())
        now = utc_iso()
        with self._lock:
            self._db.execute(
                """INSERT INTO messages (id, conversation_id, member_id, role, text, created_at)
                   VALUES (?, ?, ?, 'assistant', ?, ?)""",
                (message_id, conversation_id, member_id, text, now),
            )
            self._db.commit()
            return self._message_by_id(message_id)

    def conversation_messages(self, conversation_id: str) -> list[StoredMessage]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (conversation_id,)
            ).fetchall()
        return [_to_message(row) for row in rows]

    def messages_before(self, conversation_id: str, seq: int, limit: int = 60) -> list[StoredMessage]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM (SELECT * FROM messages
                                  WHERE conversation_id = ? AND seq <= ?
                                  ORDER BY seq DESC LIMIT ?)
                   ORDER BY seq""",
                (conversation_id, seq, limit),
            ).fetchall()
        return [_to_message(row) for row in rows]

    def other_conversation_messages(self, member_id: str, exclude_conversation: str, limit: int = 12) -> list[StoredMessage]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM (SELECT * FROM messages
                                  WHERE member_id = ? AND conversation_id != ?
                                  ORDER BY seq DESC LIMIT ?)
                   ORDER BY seq""",
                (member_id, exclude_conversation, limit),
            ).fetchall()
        return [_to_message(row) for row in rows]

    def has_assistant_reply_after(self, conversation_id: str, seq: int) -> bool:
        with self._lock:
            row = self._db.execute(
                """SELECT 1 FROM messages WHERE conversation_id = ? AND seq > ? AND role = 'assistant' LIMIT 1""",
                (conversation_id, seq),
            ).fetchone()
        return row is not None

    def message_by_id(self, message_id: str) -> StoredMessage | None:
        with self._lock:
            return self._message_by_id(message_id)

    def unanswered_member_messages(self) -> list[StoredMessage]:
        """Member messages with no assistant message after them, used to resume after a restart."""
        with self._lock:
            rows = self._db.execute(
                """SELECT m.* FROM messages m
                   WHERE m.role = 'member'
                     AND NOT EXISTS (SELECT 1 FROM messages a
                                     WHERE a.conversation_id = m.conversation_id
                                       AND a.seq > m.seq AND a.role = 'assistant')
                   ORDER BY m.seq"""
            ).fetchall()
        return [_to_message(row) for row in rows]

    def _message_by_id(self, message_id: str) -> StoredMessage | None:
        row = self._db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _to_message(row) if row else None

    # Members and memory --------------------------------------------------

    def member_name(self, member_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT name FROM members WHERE id = ?", (member_id,)).fetchone()
        return row["name"] if row else None

    def remember(self, member_id: str, category: str, value: str) -> bool:
        """Store a fact about a member. Returns True if it was new."""
        if category not in FACT_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        value = " ".join(value.split())[:200]
        if not value:
            return False
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO member_facts (member_id, category, value, value_key, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (member_id, category, value, value.lower(), utc_iso()),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def facts(self, member_id: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT category, value FROM member_facts WHERE member_id = ? ORDER BY category, id",
                (member_id,),
            ).fetchall()
        return [(row["category"], row["value"]) for row in rows]

    # Bookings ------------------------------------------------------------

    def add_booking(
        self,
        *,
        reference: str,
        member_id: str,
        conversation_id: str,
        venue_id: str,
        venue_name: str,
        on_date: str,
        at_time: str,
        party_size: int,
    ) -> None:
        now = utc_iso()
        with self._lock:
            self._db.execute(
                """INSERT INTO bookings (reference, member_id, conversation_id, venue_id, venue_name,
                                         on_date, at_time, party_size, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)""",
                (reference, member_id, conversation_id, venue_id, venue_name, on_date, at_time, party_size, now, now),
            )
            self._db.commit()

    def booking_for_member(self, member_id: str, reference: str) -> BookingRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bookings WHERE member_id = ? AND reference = ?", (member_id, reference)
            ).fetchone()
        return _to_booking(row) if row else None

    def active_booking_at(self, member_id: str, venue_id: str, on_date: str, at_time: str) -> BookingRecord | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM bookings
                   WHERE member_id = ? AND venue_id = ? AND on_date = ? AND at_time = ? AND status = 'confirmed'
                   ORDER BY created_at LIMIT 1""",
                (member_id, venue_id, on_date, at_time),
            ).fetchone()
        return _to_booking(row) if row else None

    def set_booking_status(self, reference: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE bookings SET status = ?, updated_at = ? WHERE reference = ?",
                (status, utc_iso(), reference),
            )
            self._db.commit()

    def bookings_for_member(self, member_id: str, *, on_or_after: str) -> list[BookingRecord]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM bookings WHERE member_id = ? AND on_date >= ?
                   ORDER BY on_date, at_time""",
                (member_id, on_or_after),
            ).fetchall()
        return [_to_booking(row) for row in rows]

    # Audit ---------------------------------------------------------------

    def log_tool_call(
        self, *, conversation_id: str, message_id: str, name: str, arguments: dict, result: dict
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO tool_calls (conversation_id, message_id, name, arguments, result, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (conversation_id, message_id, name, json.dumps(arguments), json.dumps(result), utc_iso()),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # Helpers -------------------------------------------------------------

    def _upsert_member(self, member_id: str, name: str, now: str) -> None:
        self._db.execute(
            """INSERT INTO members (id, name, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at""",
            (member_id, name, now),
        )


def _to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        seq=row["seq"],
        id=row["id"],
        conversation_id=row["conversation_id"],
        member_id=row["member_id"],
        role=row["role"],
        text=row["text"],
        created_at=row["created_at"],
    )


def _to_booking(row: sqlite3.Row) -> BookingRecord:
    return BookingRecord(
        reference=row["reference"],
        conversation_id=row["conversation_id"],
        venue_id=row["venue_id"],
        venue_name=row["venue_name"],
        on_date=row["on_date"],
        at_time=row["at_time"],
        party_size=row["party_size"],
        status=row["status"],
    )

