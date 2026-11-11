"""Authentication: per-device bearer tokens and role tokens for the dashboard."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request

Role = Literal["researcher", "caregiver"]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Missing bearer token"})
    return authorization[7:].strip()


@dataclass
class DeviceAuth:
    device_id: str
    client_kind: str      # device | simulator
    boot_id: str | None
    firmware: str | None


def device_auth(request: Request, authorization: str | None = Header(None),
                x_device_id: str | None = Header(None), x_client_kind: str | None = Header(None),
                x_boot_id: str | None = Header(None), x_firmware_version: str | None = Header(None)) -> DeviceAuth:
    token = _bearer(authorization)
    if not x_device_id or len(x_device_id) > 32:
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Missing device id"})
    db = request.app.state.ctx.db
    row = db.one("SELECT token_hash FROM devices WHERE device_id=?", (x_device_id,))
    if not row or not hmac.compare_digest(row["token_hash"], hash_token(token)):
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Unknown device or token"})
    kind = "device" if x_client_kind == "device" else "simulator"
    return DeviceAuth(x_device_id, kind, (x_boot_id or "")[:16] or None, (x_firmware_version or "")[:32] or None)


def admin_role(request: Request, authorization: str | None = Header(None)) -> Role:
    token = _bearer(authorization)
    s = request.app.state.ctx.settings
    configured = False
    for role in ("researcher", "caregiver"):
        secret = getattr(s, f"{role}_token")
        if secret and secret.get_secret_value():
            configured = True
            if hmac.compare_digest(secret.get_secret_value(), token):
                return role  # type: ignore[return-value]
    if not configured:
        raise HTTPException(503, detail={"code": "dashboard_not_configured",
                                         "message": "Set RESEARCHER_TOKEN and CAREGIVER_TOKEN in backend/.env"})
    raise HTTPException(401, detail={"code": "unauthenticated", "message": "Invalid dashboard token"})


def require_researcher(role: Role = Depends(admin_role)) -> Role:
    if role != "researcher":
        raise HTTPException(403, detail={"code": "forbidden", "message": "Researcher access required"})
    return role
