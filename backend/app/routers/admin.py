"""Caregiver / researcher dashboard API (bearer role tokens).

Role separation (protocol §3.1, "asymmetric roles"):
  caregiver  - personal context, voice messages, comfort/accessibility settings, device status
  researcher - everything above + research condition, probes, LED calibration, retention,
               simulation tools, metrics and export
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from .. import BACKEND_VERSION
from ..audio import decode_wav_to_device_pcm, pcm_to_wav
from ..context import PERSON_STATUS, delete_all_personal_data, load_approved, now_iso
from ..device_config import CAREGIVER_FIELDS, ConfigError
from ..metrics import compute as compute_metrics
from ..providers import ProviderError
from ..security import Role, admin_role, hash_token, new_token, require_researcher

router = APIRouter(prefix="/api/v1/admin", tags=["dashboard"])


def ctx_of(request: Request):
    return request.app.state.ctx


def _bad(e: Exception) -> HTTPException:
    return HTTPException(422, detail={"code": "invalid", "message": str(e)})


# ---------------------------------------------------------------- status ---
@router.get("/whoami")
def whoami(role: Role = Depends(admin_role)):
    return {"role": role}


@router.get("/status")
def status(request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    s = ctx.settings
    devices = []
    now = time.monotonic()
    for d in ctx.db.query("SELECT device_id, label, last_seen_at, firmware, boot_id, state, rssi, applied_version, "
                          "client_kind, last_interaction_at FROM devices ORDER BY device_id"):
        seen = ctx.last_seen.get(d["device_id"])
        d["connected"] = seen is not None and now - seen < 5.0
        d["seconds_since_seen"] = round(now - seen, 1) if seen is not None else None
        diag = ctx.db.one("SELECT data, received_at FROM diagnostics WHERE device_id=? ORDER BY id DESC LIMIT 1",
                          (d["device_id"],))
        d["diagnostics"] = json.loads(diag["data"]) | {"received_at": diag["received_at"]} if diag else None
        devices.append(d)
    cfg = ctx.config.device()
    rc = ctx.config.research()
    return {
        "role": role,
        "backend_version": BACKEND_VERSION,
        "ai": {"provider": s.ai_provider, "mode": ctx.events.ai_mode, "ready": s.ai_ready,
               "stt_model": s.stt_model, "llm_model": s.llm_model, "tts_model": s.tts_model, "tts_voice": s.tts_voice,
               "tts_speed": s.tts_speed, "llm_temperature": s.llm_temperature,
               "llm_reasoning_effort": s.llm_reasoning_effort, "llm_max_output_tokens": s.llm_max_output_tokens},
        "prompt": {"version": ctx.prompt.version, "sha256_16": ctx.prompt.sha256},
        "devices": devices,
        "device_config": cfg,
        "research": rc if role == "researcher" else {"participant_code": rc.get("participant_code")},
        "active_sessions": ctx.sessions.count(),
        "notifications": {"caregiver_notifications_configured": False,
                          "note": "Safety flags are logged for the research team only. No message is sent to anyone."},
    }


@router.get("/events")
def events(request: Request, limit: int = 200, since_id: int = 0, role: Role = Depends(require_researcher)):
    return ctx_of(request).events.recent(min(max(limit, 1), 1000), since_id)


@router.get("/metrics")
def metrics(request: Request, role: Role = Depends(require_researcher)):
    return compute_metrics(ctx_of(request).db)


# ------------------------------------------------------------- settings ---
class DeviceSettingsIn(BaseModel):
    changes: dict[str, Any]


@router.put("/device-config")
def put_device_config(body: DeviceSettingsIn, request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    allowed = None if role == "researcher" else CAREGIVER_FIELDS
    try:
        cfg, applied = ctx.config.update_device(body.changes, allowed)
    except ConfigError as e:
        raise HTTPException(403 if "not permitted" in str(e) else 422, detail={"code": "invalid", "message": str(e)})
    for k, v in applied.items():
        name = "condition_changed" if k == "condition" else "setting_changed"
        ctx.events.log(name, source="dashboard", detail=f"{k}:{v['from']}->{v['to']}"[:80], code=role,
                       value=cfg["version"], condition=cfg["condition"])
        if k in ("reduced_motion", "lights_off") and cfg["condition"] in ("B2", "B3") and v["to"]:
            ctx.events.log("accessibility_override", source="dashboard", detail=k, code=role,
                           condition=cfg["condition"])
    return {"device_config": cfg, "applied": applied}


class ConditionIn(BaseModel):
    condition: Literal["B1", "B2", "B3"]


@router.post("/condition")
def set_condition(body: ConditionIn, request: Request, role: Role = Depends(require_researcher)):
    return put_device_config(DeviceSettingsIn(changes={"condition": body.condition}), request, role)


class ProbeIn(BaseModel):
    status: Literal["IDLE", "LISTENING", "PROCESSING", "SPEAKING", "ERROR", "OFFLINE"]
    duration_ms: int = Field(8000, ge=1000, le=30000)


@router.post("/probe", summary="Show a status light pattern only (status comprehension probe)")
def probe(body: ProbeIn, request: Request, role: Role = Depends(require_researcher)):
    ctx = ctx_of(request)
    cfg = ctx.config.probe(body.status, body.duration_ms)
    ctx.events.log("status_probe_requested", source="dashboard", detail=body.status, value=body.duration_ms)
    return {"probe": cfg["probe"]}


class LedDiagIn(BaseModel):
    pattern: Literal["logical_zero", "rgb", "all_white_low"]


@router.post("/led-diag")
def led_diag(body: LedDiagIn, request: Request, role: Role = Depends(require_researcher)):
    cfg = ctx_of(request).config.led_diag(body.pattern)
    ctx_of(request).events.log("led_diag_requested", source="dashboard", detail=body.pattern)
    return {"diag": cfg["diag"]}


@router.post("/session/reset", summary="Start a fresh conversation (new participant or new block)")
def reset_session(request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    n = ctx.sessions.reset_all()
    cfg = ctx.config.reset_session()
    ctx.events.log("session_reset", source="dashboard", value=n, code=role)
    return {"sessions_cleared": n, "session_reset_seq": cfg["session_reset_seq"]}


class ResearchIn(BaseModel):
    changes: dict[str, Any]
    consent_recorded: bool = False


@router.get("/research-config")
def get_research(request: Request, role: Role = Depends(require_researcher)):
    return ctx_of(request).config.research()


@router.put("/research-config")
def put_research(body: ResearchIn, request: Request, role: Role = Depends(require_researcher)):
    ctx = ctx_of(request)
    old_code = ctx.config.research().get("participant_code")
    try:
        rc, applied = ctx.config.update_research(body.changes, body.consent_recorded)
    except ConfigError as e:
        raise _bad(e)
    for k, v in applied.items():
        ctx.events.log("research_setting_changed", source="dashboard",
                       detail=f"{k}:{v['to']}"[:80] if k not in ("emergency_text", "self_harm_text") else k)
    if "participant_code" in applied and applied["participant_code"]["to"] != old_code:
        # A different participant must never inherit the previous conversation.
        reset_session(request, role)
    return rc


# ------------------------------------------------------- personal context ---
class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    relationship: str = Field(min_length=1, max_length=60)
    status: Literal["living", "deceased_verified", "unknown"] = "unknown"
    notes: str = Field("", max_length=200)
    approved: bool = False


class RoutineIn(BaseModel):
    time_label: str = Field("", max_length=40)
    description: str = Field(min_length=1, max_length=200)
    approved: bool = False


class ActivityIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    approved: bool = False


class ProfileIn(BaseModel):
    preferred_name: str = Field("", max_length=60)
    communication_style: str = Field("", max_length=300)


TABLES = {"people": PersonIn, "routines": RoutineIn, "activities": ActivityIn}


@router.get("/context")
def get_context(request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    return {
        "profile": ctx.db.kv_get("profile", {}) or {},
        "people": ctx.db.query("SELECT * FROM people ORDER BY id"),
        "routines": ctx.db.query("SELECT * FROM routines ORDER BY id"),
        "activities": ctx.db.query("SELECT * FROM activities ORDER BY id"),
        "voice_messages": ctx.db.query("SELECT id, label, person_id, description, duration_s, approved, created_at "
                                       "FROM voice_messages ORDER BY created_at"),
        "person_status_options": PERSON_STATUS,
        "prompt_preview": load_approved(ctx.db).to_prompt(),
    }


@router.put("/context/profile")
def put_profile(body: ProfileIn, request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    ctx.db.kv_set("profile", body.model_dump())
    ctx.events.log("context_changed", source="dashboard", detail="profile", code=role)
    return body.model_dump()


def _upsert(table: str, data: BaseModel, request: Request, role: str, row_id: int | None = None):
    ctx = ctx_of(request)
    d = data.model_dump()
    d["approved"] = int(d["approved"])
    d["updated_at"] = now_iso()
    cols = list(d)
    if row_id is None:
        cur = ctx.db.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                             tuple(d.values()))
        row_id = cur.lastrowid
    else:
        cur = ctx.db.execute(f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} WHERE id=?",
                             (*d.values(), row_id))
        if cur.rowcount == 0:
            raise HTTPException(404, detail={"code": "not_found", "message": f"{table} {row_id} not found"})
    ctx.events.log("context_changed", source="dashboard", detail=table, code=role)   # never the content
    return ctx.db.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))


def _parse(model: type[BaseModel], body: dict) -> BaseModel:
    try:
        return model.model_validate(body)
    except ValidationError as e:
        # Report field names only; never echo the submitted personal data.
        fields = [".".join(str(p) for p in err["loc"]) for err in e.errors()][:10]
        raise HTTPException(422, detail={"code": "invalid", "message": "Invalid fields: " + ", ".join(fields)})


def _register(table: str, model: type[BaseModel]) -> None:
    # Bodies are validated explicitly (model is a closure variable, which FastAPI
    # cannot resolve from a postponed annotation).
    def create(request: Request, body: dict = Body(...), role: Role = Depends(admin_role)):
        return _upsert(table, _parse(model, body), request, role)

    def update(row_id: int, request: Request, body: dict = Body(...), role: Role = Depends(admin_role)):
        return _upsert(table, _parse(model, body), request, role, row_id)

    def delete(row_id: int, request: Request, role: Role = Depends(admin_role)):
        ctx = ctx_of(request)
        n = ctx.db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,)).rowcount
        ctx.events.log("context_deleted", source="dashboard", detail=table, code=role)
        return {"deleted": n}

    router.add_api_route(f"/context/{table}", create, methods=["POST"], name=f"create_{table}")
    router.add_api_route(f"/context/{table}/{{row_id}}", update, methods=["PUT"], name=f"update_{table}")
    router.add_api_route(f"/context/{table}/{{row_id}}", delete, methods=["DELETE"], name=f"delete_{table}")


for _table, _model in TABLES.items():
    _register(_table, _model)


@router.delete("/context", summary="Delete ALL caregiver-entered personal data and voice recordings")
def delete_context(request: Request, confirm: str = "", role: Role = Depends(admin_role)):
    if confirm != "DELETE":
        raise HTTPException(400, detail={"code": "confirm_required", "message": "Pass confirm=DELETE"})
    ctx = ctx_of(request)
    n = delete_all_personal_data(ctx.db, ctx.voice_dir)
    ctx.sessions.reset_all()
    ctx.events.log("personal_data_deleted", source="dashboard", value=n, code=role)
    return {"deleted_items": n}


# --------------------------------------------------------- voice messages ---
@router.post("/voice-messages", summary="Upload an authorised familiar-voice recording (WAV)")
async def upload_voice(request: Request, file: UploadFile, label: str, description: str = "",
                       person_id: int | None = None, approved: bool = False, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    if not 1 <= len(label) <= 60 or len(description) > 120:
        raise HTTPException(422, detail={"code": "invalid", "message": "label 1-60 chars, description <=120"})
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, detail={"code": "too_large", "message": "File larger than 10 MB"})
    try:
        pcm, seconds = decode_wav_to_device_pcm(data, ctx.settings.max_voice_message_s)
    except ValueError as e:
        raise _bad(e)
    vid = "vm_" + secrets.token_hex(4)
    fname = f"{vid}.pcm"
    (ctx.voice_dir / fname).write_bytes(pcm)
    ctx.db.execute("INSERT INTO voice_messages(id, label, person_id, description, filename, duration_s, approved, "
                   "created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (vid, label, person_id, description, fname, round(seconds, 2), int(approved), now_iso()))
    ctx.events.log("voice_message_added", source="dashboard", detail=vid, duration_ms=seconds * 1000, code=role)
    return ctx.db.one("SELECT id, label, person_id, description, duration_s, approved FROM voice_messages WHERE id=?",
                      (vid,))


class VoicePatch(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=60)
    description: str | None = Field(None, max_length=120)
    approved: bool | None = None
    person_id: int | None = None


@router.patch("/voice-messages/{vid}")
def patch_voice(vid: str, body: VoicePatch, request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    if not ctx.db.one("SELECT id FROM voice_messages WHERE id=?", (vid,)):
        raise HTTPException(404, detail={"code": "not_found", "message": "No such message"})
    for k, v in body.model_dump(exclude_none=True).items():
        ctx.db.execute(f"UPDATE voice_messages SET {k}=? WHERE id=?", (int(v) if isinstance(v, bool) else v, vid))
    ctx.events.log("voice_message_changed", source="dashboard", detail=vid, code=role)
    return ctx.db.one("SELECT id, label, person_id, description, duration_s, approved FROM voice_messages WHERE id=?",
                      (vid,))


@router.get("/voice-messages/{vid}/audio")
def voice_audio(vid: str, request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    row = ctx.db.one("SELECT filename FROM voice_messages WHERE id=?", (vid,))
    if not row or not (ctx.voice_dir / row["filename"]).exists():
        raise HTTPException(404, detail={"code": "not_found", "message": "No such message"})
    return Response(pcm_to_wav((ctx.voice_dir / row["filename"]).read_bytes()), media_type="audio/wav",
                    headers={"Cache-Control": "no-store"})


@router.delete("/voice-messages/{vid}")
def delete_voice(vid: str, request: Request, role: Role = Depends(admin_role)):
    ctx = ctx_of(request)
    row = ctx.db.one("SELECT filename FROM voice_messages WHERE id=?", (vid,))
    if row and (ctx.voice_dir / row["filename"]).exists():
        (ctx.voice_dir / row["filename"]).unlink()
    n = ctx.db.execute("DELETE FROM voice_messages WHERE id=?", (vid,)).rowcount
    ctx.events.log("voice_message_deleted", source="dashboard", detail=vid, code=role)
    return {"deleted": n}


# ------------------------------------------------------------- devices ---
class ProvisionIn(BaseModel):
    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,30}$")
    label: str = Field("", max_length=60)


@router.post("/devices", summary="Provision a device; the token is shown ONCE")
def provision(body: ProvisionIn, request: Request, role: Role = Depends(require_researcher)):
    ctx = ctx_of(request)
    token = ctx.provision_device(body.device_id, body.label)
    ctx.events.log("device_provisioned", source="dashboard", device_id=body.device_id)
    return {"device_id": body.device_id, "token": token,
            "note": "Copy this into firmware/main/secrets.h now; it cannot be shown again."}


# --------------------------------------------------- simulation / testing ---
class TextTurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=12)


@router.post("/simulate/text-turn", summary="Researcher text test of the agent (no device, no audio)")
async def simulate_text(body: TextTurnIn, request: Request, role: Role = Depends(require_researcher)):
    ctx = ctx_of(request)
    if not ctx.settings.ai_ready:
        raise HTTPException(503, detail={"code": "ai_unavailable", "message": "OPENAI_API_KEY is not configured"})
    from ..sessions import Exchange
    hist = [Exchange("sim", h.get("user", "")[:500], h.get("assistant", "")[:500]) for h in body.history]
    rc = ctx.config.research()
    try:
        res = await ctx.agent.respond(body.text, hist, load_approved(ctx.db), rc["emergency_text"], rc["self_harm_text"])
    except ProviderError as e:
        raise HTTPException(502, detail={"code": f"llm_{e.code}", "message": "Language model failed"})
    ctx.events.log("simulated_text_turn", source="dashboard", run_mode="SOFTWARE_SIMULATOR",
                   duration_ms=round(res.llm_ms, 1),
                   data={"topic": res.reply.topic, "safety_flag": res.reply.safety_flag, "replaced": res.report.replaced,
                         "rules": res.report.rules})
    return {"labels": ["SOFTWARE_SIMULATOR", ctx.providers.label], "reply": res.reply.model_dump(),
            "validation": res.report.__dict__, "model": res.llm.model if res.llm else None}


# ---------------------------------------------------------------- export ---
EXPORT_COLUMNS = ["id", "received_at", "wall_ms", "t_mono_ms", "source", "run_mode", "ai_mode", "device_id", "boot_id",
                  "participant_code", "session_id", "turn_id", "firmware", "backend_version", "prompt_version",
                  "stt_model", "llm_model", "tts_model", "condition", "name", "state", "value", "duration_ms", "code",
                  "detail", "data"]


@router.get("/export", summary="Anonymised research events (no audio, no transcripts, no tokens)")
def export(request: Request, fmt: Literal["csv", "json"] = "csv", role: Role = Depends(require_researcher)):
    ctx = ctx_of(request)
    rows = ctx.db.query(f"SELECT {','.join(EXPORT_COLUMNS)} FROM events ORDER BY id")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ctx.events.log("data_exported", source="dashboard", value=len(rows), detail=fmt)
    if fmt == "json":
        return JSONResponse(rows, headers={"Content-Disposition": f'attachment; filename="unscripted_events_{stamp}.json"'})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="unscripted_events_{stamp}.csv"'})


@router.delete("/events", summary="Delete research events (configurable deletion)")
def delete_events(request: Request, confirm: str = "", role: Role = Depends(require_researcher)):
    if confirm != "DELETE":
        raise HTTPException(400, detail={"code": "confirm_required", "message": "Pass confirm=DELETE"})
    ctx = ctx_of(request)
    n = ctx.db.execute("DELETE FROM events").rowcount
    t = ctx.db.execute("DELETE FROM transcripts").rowcount
    ctx.events.log("research_data_deleted", source="dashboard", value=n, data={"transcripts": t})
    return {"events_deleted": n, "transcripts_deleted": t}


__all__ = ["router", "hash_token", "new_token"]
