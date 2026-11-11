"""Short-lived conversation sessions (in memory only, never persisted).

* A session belongs to exactly one device and expires after 10 minutes without
  activity, or immediately when the dashboard starts a new participant session.
* History holds at most `max_turns` exchanges; the AI never writes persistent memory.
* A newer turn supersedes older ones, so late results can be rejected as stale.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Exchange:
    turn_id: str
    user: str
    assistant: str


@dataclass
class Session:
    session_id: str
    device_id: str
    created: float
    last_activity: float
    history: list[Exchange] = field(default_factory=list)
    active_turn: str | None = None


class SessionStore:
    def __init__(self, expiry_s: int = 600, max_turns: int = 6, max_sessions: int = 256):
        self.expiry_s, self.max_turns, self.max_sessions = expiry_s, max_turns, max_sessions
        self._s: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _expired(self, s: Session, now: float) -> bool:
        return now - s.last_activity > self.expiry_s

    def resolve(self, device_id: str, session_id: str | None, now: float | None = None) -> tuple[Session, bool]:
        """Return (session, is_new). Unknown, expired, or other-device ids get a NEW session."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._gc(now)
            s = self._s.get(session_id or "")
            if s and s.device_id == device_id and not self._expired(s, now):
                s.last_activity = now
                return s, False
            sid = secrets.token_hex(8)
            s = Session(session_id=sid, device_id=device_id, created=now, last_activity=now)
            self._s[sid] = s
            return s, True

    def begin_turn(self, s: Session, turn_id: str) -> None:
        with self._lock:
            s.active_turn = turn_id

    def is_current(self, s: Session, turn_id: str) -> bool:
        with self._lock:
            return s.active_turn == turn_id and s.session_id in self._s

    def commit(self, s: Session, turn_id: str, user: str, assistant: str) -> bool:
        with self._lock:
            if s.active_turn != turn_id or s.session_id not in self._s:
                return False
            s.history.append(Exchange(turn_id, user, assistant))
            del s.history[:-self.max_turns]
            s.last_activity = time.monotonic()
            return True

    def rollback(self, turn_id: str) -> bool:
        with self._lock:
            for s in self._s.values():
                before = len(s.history)
                s.history = [e for e in s.history if e.turn_id != turn_id]
                if s.active_turn == turn_id:
                    s.active_turn = None
                if len(s.history) != before:
                    return True
        return False

    def history(self, s: Session) -> list[Exchange]:
        with self._lock:
            return list(s.history)

    def reset_device(self, device_id: str) -> int:
        with self._lock:
            ids = [k for k, s in self._s.items() if s.device_id == device_id]
            for k in ids:
                del self._s[k]
            return len(ids)

    def reset_all(self) -> int:
        with self._lock:
            n = len(self._s)
            self._s.clear()
            return n

    def _gc(self, now: float) -> None:
        for k in [k for k, s in self._s.items() if self._expired(s, now)]:
            del self._s[k]
        if len(self._s) > self.max_sessions:   # bounded memory
            for k, _ in sorted(self._s.items(), key=lambda kv: kv[1].last_activity)[: len(self._s) - self.max_sessions]:
                del self._s[k]

    def count(self) -> int:
        with self._lock:
            return len(self._s)
