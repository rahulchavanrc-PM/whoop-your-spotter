"""SQLite schema and helpers for coach.db.

Every public function opens and closes its own connection — call volume for
a single-user coach is low enough that this is simpler than passing a
connection around, and it keeps mcp_server.py's tool functions one-liners.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "coach.db"

EVENT_KINDS = {"alcohol", "caffeine", "food", "note", "mood", "other"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    date TEXT PRIMARY KEY,
    recovery INTEGER,
    hrv REAL,
    rhr INTEGER,
    sleep_perf INTEGER,
    sleep_hours REAL,
    strain REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_ts TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_snapshot(
    date: str,
    recovery: int | None = None,
    hrv: float | None = None,
    rhr: int | None = None,
    sleep_perf: int | None = None,
    sleep_hours: float | None = None,
    strain: float | None = None,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO daily_snapshots (date, recovery, hrv, rhr, sleep_perf, sleep_hours, strain, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                recovery = COALESCE(excluded.recovery, daily_snapshots.recovery),
                hrv = COALESCE(excluded.hrv, daily_snapshots.hrv),
                rhr = COALESCE(excluded.rhr, daily_snapshots.rhr),
                sleep_perf = COALESCE(excluded.sleep_perf, daily_snapshots.sleep_perf),
                sleep_hours = COALESCE(excluded.sleep_hours, daily_snapshots.sleep_hours),
                strain = COALESCE(excluded.strain, daily_snapshots.strain),
                fetched_at = excluded.fetched_at
            """,
            (date, recovery, hrv, rhr, sleep_perf, sleep_hours, strain, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_snapshot(date: str) -> sqlite3.Row | None:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM daily_snapshots WHERE date = ?", (date,)
        ).fetchone()
    finally:
        conn.close()


def get_snapshots(days: int = 14) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM daily_snapshots WHERE date >= ? ORDER BY date ASC", (since,)
        ).fetchall()
    finally:
        conn.close()


def insert_event(kind: str, note: str, at: str | None = None) -> sqlite3.Row:
    if kind not in EVENT_KINDS:
        raise ValueError(f"kind must be one of {sorted(EVENT_KINDS)}, got {kind!r}")
    at = at or _now_iso()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO events (kind, note, at, created_at) VALUES (?, ?, ?, ?)",
            (kind, note, at, _now_iso()),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    finally:
        conn.close()


def get_events(kind: str | None = None, days: int = 30) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        if kind:
            return conn.execute(
                "SELECT * FROM events WHERE kind = ? AND at >= ? ORDER BY at DESC",
                (kind, since),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM events WHERE at >= ? ORDER BY at DESC", (since,)
        ).fetchall()
    finally:
        conn.close()


def add_conversation(thread_ts: str | None, role: str, content: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO conversations (thread_ts, role, content, at) VALUES (?, ?, ?, ?)",
            (thread_ts, role, content, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_conversation(thread_ts: str, limit: int = 20) -> list[sqlite3.Row]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE thread_ts = ? ORDER BY id DESC LIMIT ?",
            (thread_ts, limit),
        ).fetchall()
        return list(reversed(rows))
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def clear_setting(key: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def resolve_timezone() -> ZoneInfo:
    """The user's current timezone — the single source of truth.

    Lives in db.py because two *separate processes* need the same answer:
    the scheduler in app.py, and the MCP server child process that stamps
    event times. They share nothing but this SQLite file, so the Slack
    lookup is done once per tick by app.py and cached here under `slack_tz`
    — that keeps Slack's API out of the child process entirely.

    Order: manual override ("I'm in Tokyo this week") > Slack profile >
    host zone (only a fallback; the Oracle VM runs UTC).
    """
    for key in ("tz_override", "slack_tz"):
        name = get_setting(key)
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("ignoring unusable timezone %r in setting %s", name, key)
    return get_localzone()


def local_today(now: datetime | None = None) -> str:
    """The owner's current calendar date (ISO), never the host machine's.

    The host's date is wrong whenever the host runs UTC (the Oracle VM) and the
    owner is in the Americas: from 20:00 Eastern the host already says
    "tomorrow", so "7pm today" would be filed on the wrong day. `now` is an
    injection seam for tests.
    """
    tz = resolve_timezone()
    return (now.astimezone(tz) if now else datetime.now(tz)).date().isoformat()


def set_setting(key: str, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
