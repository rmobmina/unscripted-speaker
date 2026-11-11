"""SQLite persistence (standard library only).

Tables are deliberately separate so privacy rules are easy to audit:
  * caregiver-approved context  (people, routines, activities, profile, voice_messages)
  * research events             (events)  - no audio, no transcript text
  * transcripts                 (only written when retention is explicitly enabled)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_seen_mono REAL,
    firmware TEXT,
    boot_id TEXT,
    state TEXT,
    rssi INTEGER,
    applied_version INTEGER,
    client_kind TEXT,
    last_interaction_at TEXT
);
CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    relationship TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT NOT NULL DEFAULT '',
    approved INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time_label TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_messages (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    person_id INTEGER,
    description TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    duration_s REAL NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    wall_ms INTEGER,
    t_mono_ms INTEGER,
    source TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    ai_mode TEXT NOT NULL,
    device_id TEXT,
    boot_id TEXT,
    participant_code TEXT,
    session_id TEXT,
    turn_id TEXT,
    firmware TEXT,
    backend_version TEXT,
    prompt_version TEXT,
    stt_model TEXT,
    llm_model TEXT,
    tts_model TEXT,
    condition TEXT,
    name TEXT NOT NULL,
    state TEXT,
    value REAL,
    duration_ms REAL,
    code TEXT,
    detail TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS events_name ON events(name);
CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    participant_code TEXT,
    session_id TEXT,
    turn_id TEXT,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
