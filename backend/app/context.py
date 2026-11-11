"""Caregiver-approved personal context.

Persistent personal information enters the system ONLY through this module,
from the authenticated caregiver/researcher dashboard. The AI never writes
here. Only rows with approved=1 are ever shown to the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import Database

PERSON_STATUS = ("living", "deceased_verified", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ApprovedContext:
    preferred_name: str = ""
    communication_style: str = ""
    people: list[dict] = field(default_factory=list)
    routines: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)
    voice_messages: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.preferred_name or self.people or self.routines or self.activities or self.voice_messages)

    def terms(self) -> set[str]:
        """Lower-case words the reply may use as verified personal facts."""
        out: set[str] = set()
        for text in [self.preferred_name] + [p["name"] for p in self.people] + [p["relationship"] for p in self.people]:
            out |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text or "")}
        return out

    def voice_ids(self) -> set[str]:
        return {v["id"] for v in self.voice_messages}

    def to_prompt(self) -> str:
        if self.empty:
            return ("No caregiver-approved personal context is available. Do not mention any personal names, "
                    "relationships, places, routines or times unless the person just said them.")
        lines = []
        if self.preferred_name:
            lines.append(f"- The person likes to be called: {self.preferred_name}")
        if self.communication_style:
            lines.append(f"- Communication preferences: {self.communication_style}")
        for p in self.people:
            status = {"living": "", "deceased_verified": " (has died - verified by caregiver)",
                      "unknown": ""}.get(p["status"], "")
            note = f"; {p['notes']}" if p.get("notes") else ""
            lines.append(f"- Person: {p['name']}, {p['relationship']}{status}{note}")
        for r in self.routines:
            when = f"{r['time_label']}: " if r.get("time_label") else ""
            lines.append(f"- Routine: {when}{r['description']}")
        for a in self.activities:
            lines.append(f"- Comforting activity: {a['description']}")
        out = "\n".join(lines)
        if self.voice_messages:
            out += "\n\nApproved familiar voice messages (offer, never force):\n" + "\n".join(
                f"- id={v['id']}: {v['label']}" + (f" ({v['description']})" if v.get("description") else "")
                for v in self.voice_messages)
        return out


def load_approved(db: Database) -> ApprovedContext:
    profile = db.kv_get("profile", {}) or {}
    return ApprovedContext(
        preferred_name=(profile.get("preferred_name") or "")[:60] if profile.get("approved", True) else "",
        communication_style=(profile.get("communication_style") or "")[:300],
        people=db.query("SELECT id, name, relationship, status, notes FROM people WHERE approved=1 ORDER BY id"),
        routines=db.query("SELECT id, time_label, description FROM routines WHERE approved=1 ORDER BY id"),
        activities=db.query("SELECT id, description FROM activities WHERE approved=1 ORDER BY id"),
        voice_messages=db.query("SELECT id, label, description FROM voice_messages WHERE approved=1 ORDER BY created_at"),
    )


def delete_all_personal_data(db: Database, voice_dir) -> int:
    n = 0
    for table in ("people", "routines", "activities"):
        n += db.execute(f"DELETE FROM {table}").rowcount
    for v in db.query("SELECT filename FROM voice_messages"):
        p = voice_dir / v["filename"]
        if p.exists():
            p.unlink()
        n += 1
    db.execute("DELETE FROM voice_messages")
    db.kv_set("profile", {})
    return n


now_iso = _now
