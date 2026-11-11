"""Research event log: timestamped, non-sensitive, labelled by provenance.

Every row records whether it came from REAL_HARDWARE or a SOFTWARE_SIMULATOR
(run_mode; SYSTEM for dashboard/backend housekeeping not tied to a client) and
whether AI output was LIVE_AI, MOCK_PROVIDER, LOOPBACK or AI_NOT_CONFIGURED (ai_mode), so simulated and real measurements are never mixed silently.
No audio and no transcript text is ever written here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import BACKEND_VERSION
from .db import Database

_SENSITIVE_KEYS = {"text", "transcript", "reply_text", "audio", "pcm", "token", "api_key", "authorization"}


def _clean(data: dict | None) -> str | None:
    if not data:
        return None
    safe = {k: v for k, v in data.items() if k.lower() not in _SENSITIVE_KEYS}
    return json.dumps(safe, default=str)[:2000]


def run_mode_for(client_kind: str | None) -> str:
    return "REAL_HARDWARE" if client_kind == "device" else "SOFTWARE_SIMULATOR"


class EventLog:
    def __init__(self, db: Database, prompt_version: str, models: dict[str, str], ai_mode: str):
        self.db = db
        self.prompt_version = prompt_version
        self.models = models
        self.ai_mode = ai_mode

    def research(self) -> dict:
        return self.db.kv_get("research_config", {}) or {}

    def log(self, name: str, *, source: str = "backend", run_mode: str = "SYSTEM",
            device_id: str | None = None, boot_id: str | None = None, session_id: str | None = None,
            turn_id: str | None = None, firmware: str | None = None, condition: str | None = None,
            state: str | None = None, value: float | None = None, duration_ms: float | None = None,
            code: str | int | None = None, detail: str | None = None, wall_ms: int | None = None,
            t_mono_ms: int | None = None, ai_mode: str | None = None, data: dict[str, Any] | None = None) -> None:
        if not re.fullmatch(r"[a-z_]{1,48}", name):
            raise ValueError("invalid event name")
        rc = self.research()
        if condition is None:
            cfg = self.db.kv_get("device_config", {}) or {}
            condition = cfg.get("condition")
        self.db.execute(
            """INSERT INTO events(received_at, wall_ms, t_mono_ms, source, run_mode, ai_mode, device_id, boot_id,
                participant_code, session_id, turn_id, firmware, backend_version, prompt_version, stt_model,
                llm_model, tts_model, condition, name, state, value, duration_ms, code, detail, data)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), wall_ms, t_mono_ms, source, run_mode,
             ai_mode or self.ai_mode, device_id, boot_id, rc.get("participant_code"), session_id, turn_id, firmware,
             BACKEND_VERSION, self.prompt_version, self.models.get("stt"), self.models.get("llm"),
             self.models.get("tts"), condition, name, state, value, duration_ms,
             None if code is None else str(code), (detail or "")[:80] or None, _clean(data)),
        )

    def recent(self, limit: int = 200, since_id: int = 0) -> list[dict]:
        return self.db.query("SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", (since_id, limit))
