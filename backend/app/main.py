"""FastAPI application: device API, dashboard API and the static dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import BACKEND_VERSION
from .agent import Agent, load_prompt
from .config import Settings, get_settings
from .db import Database
from .device_config import ConfigStore
from .events import EventLog, run_mode_for
from .providers import Providers, build_providers
from .routers import admin, device
from .security import DeviceAuth, hash_token, new_token
from .sessions import SessionStore
from .turns import TurnManager

STATIC_DIR = Path(__file__).parent / "static"


class _NoSecretsFilter(logging.Filter):
    """Belt-and-braces: drop any log record that looks like it carries a credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("sk-" in msg or "Bearer " in msg)


class AppContext:
    def __init__(self, settings: Settings, providers: Providers | None = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.voice_dir = settings.data_dir / "voice_messages"
        self.retained_dir = settings.data_dir / "retained_audio"
        self.voice_dir.mkdir(exist_ok=True)
        self.db = Database(settings.data_dir / "unscripted.db")
        self.prompt = load_prompt(settings.prompt_path)
        self.providers = providers or build_providers(settings)
        self.events = EventLog(self.db, self.prompt.version,
                               {"stt": self.providers.stt.model, "llm": self.providers.llm.model,
                                "tts": self.providers.tts.model},
                               self.providers.label if settings.ai_ready else "AI_NOT_CONFIGURED")
        self.config = ConfigStore(self.db)
        self.sessions = SessionStore(settings.session_expiry_s, settings.history_turns)
        self.agent = Agent(self.providers.llm, self.prompt)
        self.turns = TurnManager(settings, self.db, self.events, self.sessions, self.providers, self.agent,
                                 self.config, self.voice_dir, self.retained_dir)
        self.last_seen: dict[str, float] = {}
        self._last_state: dict[str, str] = {}

    # Called on every config poll (~1 Hz) - cheap, and only writes on change.
    def heartbeat(self, auth: DeviceAuth, state: str | None, rssi: str | None, applied: str | None) -> None:
        now = time.monotonic()
        first = auth.device_id not in self.last_seen or now - self.last_seen[auth.device_id] > 5.0
        self.last_seen[auth.device_id] = now
        try:
            applied_v = int(applied) if applied else None
        except ValueError:
            applied_v = None
        try:
            rssi_v = int(rssi) if rssi else None
        except ValueError:
            rssi_v = None
        self.db.execute("UPDATE devices SET last_seen_at=?, firmware=?, boot_id=?, state=?, rssi=?, applied_version=?, "
                        "client_kind=? WHERE device_id=?",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), auth.firmware, auth.boot_id,
                         (state or "")[:16], rssi_v, applied_v, auth.client_kind, auth.device_id))
        rm = run_mode_for(auth.client_kind)
        if first:
            self.events.log("device_connected", source="backend", run_mode=rm, device_id=auth.device_id,
                            boot_id=auth.boot_id, firmware=auth.firmware, value=rssi_v)
        pending = self.db.kv_get("pending_condition")
        if pending and applied_v is not None and applied_v >= pending["version"]:
            # End-to-end condition latency: dashboard change -> device acknowledges in its next poll.
            # Includes up to one poll interval (1 s); see docs/TESTING.md.
            self.events.log("condition_applied_ack", source="backend", run_mode=rm, device_id=auth.device_id,
                            boot_id=auth.boot_id, condition=pending["condition"], value=applied_v,
                            duration_ms=round((time.time() - pending["changed_at"]) * 1000, 1))
            self.db.kv_set("pending_condition", None)

    def provision_device(self, device_id: str, label: str = "") -> str:
        token = new_token()
        self.db.execute("INSERT INTO devices(device_id, token_hash, label, created_at) VALUES(?,?,?,datetime('now')) "
                        "ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash, label=excluded.label",
                        (device_id, hash_token(token), label))
        return token


def create_app(settings: Settings | None = None, providers: Providers | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(_NoSecretsFilter())
    ctx = AppContext(settings, providers)
    log = logging.getLogger("unscripted")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if ctx.providers.label == "MOCK_PROVIDER":
            log.warning("AI provider is MOCK_PROVIDER - test mode; replies are canned, not real AI")
        elif not settings.ai_ready:
            log.warning("OPENAI_API_KEY is not set: device turns will return 503 ai_unavailable")
        log.info("models stt=%s llm=%s tts=%s voice=%s prompt=%s", settings.stt_model, settings.llm_model,
                 settings.tts_model, settings.tts_voice, ctx.prompt.version)

        async def gc_loop():
            while True:
                await asyncio.sleep(15)
                ctx.turns.gc()

        task = asyncio.create_task(gc_loop())
        ctx.events.log("backend_started", source="backend", data={"ai_ready": settings.ai_ready})
        try:
            yield
        finally:
            task.cancel()
            ctx.db.close()

    app = FastAPI(title="[un]scripted 2.0 backend", version=BACKEND_VERSION, lifespan=lifespan)
    app.state.ctx = ctx

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Never echo request bodies back (they could contain personal context).
        fields = [".".join(str(p) for p in e.get("loc", [])) for e in exc.errors()][:10]
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request",
                                                                "message": "Invalid request", "fields": fields}})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": BACKEND_VERSION, "ai_mode": ctx.events.ai_mode, "ai_ready": settings.ai_ready,
                "prompt_version": ctx.prompt.version}

    app.include_router(device.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
