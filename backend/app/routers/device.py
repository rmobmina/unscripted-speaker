"""Device API (bearer device token). Used by the ESP32 firmware and the simulators."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..events import run_mode_for
from ..schemas import DeviceEventBatch
from ..security import DeviceAuth, device_auth
from ..turns import TurnError

router = APIRouter(prefix="/api/v1/device", tags=["device"])
AUDIO_FORMAT = "pcm_s16le;rate=16000;channels=1"


def _err(status: int, code: str, message: str, turn_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"turn_id": turn_id, "error": {"code": code, "message": message}})


@router.get("/config", summary="Runtime settings (poll ~1 Hz; doubles as heartbeat)")
def get_config(request: Request, auth: DeviceAuth = Depends(device_auth),
               if_none_match: str | None = Header(None), x_applied_version: str | None = Header(None),
               x_device_state: str | None = Header(None), x_wifi_rssi: str | None = Header(None)):
    ctx = request.app.state.ctx
    ctx.heartbeat(auth, x_device_state, x_wifi_rssi, x_applied_version)
    cfg = ctx.config.device()
    etag = f'"{cfg["version"]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(cfg, headers={"ETag": etag, "Cache-Control": "no-store"})


@router.post("/turns", summary="Submit one recorded utterance (raw PCM s16le 16 kHz mono)")
async def submit_turn(request: Request, auth: DeviceAuth = Depends(device_auth),
                      x_turn_id: str = Header(...), x_session_id: str | None = Header(None),
                      x_audio_format: str | None = Header(None), x_capture_quality: str | None = Header(None),
                      x_followup: str | None = Header(None), x_condition: str | None = Header(None),
                      content_length: int | None = Header(None)):
    ctx = request.app.state.ctx
    s = ctx.settings
    if x_audio_format != AUDIO_FORMAT:
        return _err(415, "bad_audio_format", f"X-Audio-Format must be {AUDIO_FORMAT}", x_turn_id)
    if content_length is None:
        return _err(411, "length_required", "Content-Length required", x_turn_id)
    if content_length > s.max_upload_bytes:
        return _err(413, "too_large", "Recording exceeds the maximum length", x_turn_id)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > s.max_upload_bytes:
            return _err(413, "too_large", "Recording exceeds the maximum length", x_turn_id)
    if len(body) != content_length:
        # Incomplete upload (e.g. the device aborted on cancel): never process a partial request.
        return _err(400, "incomplete_upload", "Body shorter than Content-Length", x_turn_id)
    try:
        quality = int(x_capture_quality) if x_capture_quality is not None else None
    except ValueError:
        quality = None
    condition = x_condition if x_condition in ("B1", "B2", "B3") else None
    try:
        resp = await ctx.turns.run(auth, x_turn_id, x_session_id, bytes(body), quality, x_followup == "1",
                                   condition, request.is_disconnected)
    except TurnError as e:
        return _err(e.status, e.code, e.message, x_turn_id)
    finally:
        body = bytearray()
    return JSONResponse(resp.model_dump(mode="json"))


@router.get("/turns/{turn_id}/audio", summary="Stream the reply audio (chunked PCM s16le 16 kHz mono)")
async def turn_audio(turn_id: str, request: Request, auth: DeviceAuth = Depends(device_auth)):
    ctx = request.app.state.ctx
    try:
        ta = ctx.turns.get_audio(auth, turn_id)
    except TurnError as e:
        return _err(e.status, e.code, e.message, turn_id)
    t0 = time.perf_counter()

    async def gen():
        sent = 0
        try:
            async for chunk in ta.iterate():
                sent += len(chunk)
                yield chunk
            ctx.events.log("audio_download_completed", run_mode=run_mode_for(auth.client_kind),
                           device_id=auth.device_id, boot_id=auth.boot_id, turn_id=turn_id, value=sent,
                           duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        except Exception as e:  # noqa: BLE001 - logged, then the stream is aborted
            ctx.events.log("error", run_mode=run_mode_for(auth.client_kind), device_id=auth.device_id,
                           turn_id=turn_id, code="audio_stream_aborted", detail=type(e).__name__, value=sent)
            raise
        finally:
            ctx.turns.release_audio(turn_id)

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Audio-Format": AUDIO_FORMAT, "Cache-Control": "no-store"})


@router.delete("/turns/{turn_id}", summary="Cancel a turn (best effort; rejects any later result)")
async def cancel_turn(turn_id: str, request: Request, auth: DeviceAuth = Depends(device_auth)):
    try:
        await request.app.state.ctx.turns.cancel(auth, turn_id)
    except TurnError as e:
        return _err(e.status, e.code, e.message, turn_id)
    return {"turn_id": turn_id, "cancelled": True}


@router.post("/events", summary="Upload a batch of device research events (no audio, no text)")
def device_events(batch: DeviceEventBatch, request: Request, auth: DeviceAuth = Depends(device_auth)):
    ctx = request.app.state.ctx
    rm = run_mode_for(auth.client_kind)
    for e in batch.events:
        ctx.events.log(e.name, source=auth.client_kind, run_mode=rm, device_id=auth.device_id, boot_id=batch.boot_id,
                       turn_id=e.turn_id or None, firmware=auth.firmware, condition=e.condition or None,
                       state=e.state or None, value=e.value, code=e.code, detail=e.detail or None, wall_ms=e.wall_ms,
                       t_mono_ms=e.t_mono_ms)
    if batch.dropped:
        ctx.events.log("telemetry_dropped", source=auth.client_kind, run_mode=rm, device_id=auth.device_id,
                       boot_id=batch.boot_id, value=batch.dropped)
    return {"accepted": len(batch.events)}


@router.post("/diagnostics", summary="Boot diagnostics (hardware self-test results)")
async def diagnostics(request: Request, auth: DeviceAuth = Depends(device_auth)):
    raw = await request.body()
    if len(raw) > 8192:
        return _err(413, "too_large", "Diagnostics too large")
    try:
        data = json.loads(raw)
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        return _err(400, "bad_json", "Diagnostics must be a JSON object")
    ctx = request.app.state.ctx
    ctx.db.execute("INSERT INTO diagnostics(device_id, received_at, data) VALUES(?, datetime('now'), ?)",
                   (auth.device_id, json.dumps(data)))
    ctx.events.log("device_diagnostics", source=auth.client_kind, run_mode=run_mode_for(auth.client_kind),
                   device_id=auth.device_id, boot_id=data.get("boot_id"), firmware=data.get("firmware"),
                   data={k: data.get(k) for k in ("es8311", "es7210", "tca9555", "speaker_ok", "mic_ok",
                                                  "mic_selftest_left_dbfs", "mic_selftest_right_dbfs",
                                                  "psram_bytes", "led_count")})
    return {"stored": True}
