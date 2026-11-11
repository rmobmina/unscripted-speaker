"""One conversational turn: audio in -> STT -> agent -> TTS audio out.

Stage timings use time.perf_counter() (monotonic); event chronology uses wall
clock. Raw audio is held in memory only for the duration of the turn unless
retention is explicitly enabled in the research configuration.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from .agent import DEFAULT_EMERGENCY_TEXT, DEFAULT_SELF_HARM_TEXT, Agent
from .audio import DEVICE_RATE, StreamingResampler, pcm_stats, pcm_to_wav
from .context import load_approved
from .events import EventLog, run_mode_for
from .providers import ProviderError, Providers
from .schemas import AgentReply, AudioInfo, LatencyMs, TurnResponse
from .security import DeviceAuth
from .sessions import SessionStore

log = logging.getLogger("unscripted.turns")

TURN_ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}-[0-9]{1,10}$")
SESSION_ID_RE = re.compile(r"^[a-f0-9]{16}$")
# Phrases speech recognisers commonly produce for silence or noise.
_STT_SILENCE_ARTIFACTS = {"", "you", "thank you", "thank you.", "thanks for watching", "thanks for watching!",
                          "bye", "bye.", ".", "..."}


class TurnError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class TurnAudio:
    turn_id: str
    device_id: str
    created: float = field(default_factory=time.monotonic)
    buf: bytearray = field(default_factory=bytearray)
    done: bool = False
    failed: bool = False
    cancelled: bool = False
    task: asyncio.Task | None = None
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    max_bytes: int = DEVICE_RATE * 2 * 120   # 2 minutes hard cap (bounded memory)

    async def append(self, data: bytes) -> None:
        if not data:
            return
        async with self.cond:
            if len(self.buf) + len(data) > self.max_bytes:
                self.failed = True
                self.done = True
            else:
                self.buf.extend(data)
            self.cond.notify_all()

    async def finish(self, failed: bool = False) -> None:
        async with self.cond:
            self.done = True
            self.failed = self.failed or failed
            self.cond.notify_all()

    async def iterate(self, chunk: int = 4096) -> AsyncIterator[bytes]:
        pos = 0
        while True:
            async with self.cond:
                while pos >= len(self.buf) and not self.done:
                    await self.cond.wait()
                data = bytes(self.buf[pos:pos + chunk])
                done, failed, cancelled = self.done, self.failed, self.cancelled
            if cancelled:
                raise asyncio.CancelledError("turn cancelled")
            if data:
                pos += len(data)
                yield data
                continue
            if done:
                if failed:
                    # Abort without the terminating chunk so the device sees an incomplete stream.
                    raise RuntimeError("tts failed mid-stream")
                return


class TurnManager:
    def __init__(self, settings, db, events: EventLog, sessions: SessionStore, providers: Providers, agent: Agent,
                 config_store, voice_dir: Path, retained_dir: Path):
        self.s, self.db, self.events, self.sessions = settings, db, events, sessions
        self.providers, self.agent, self.config = providers, agent, config_store
        self.voice_dir, self.retained_dir = voice_dir, retained_dir
        self.audio: dict[str, TurnAudio] = {}
        self.cancelled: dict[str, float] = {}

    # ------------------------------------------------------------------ helpers --
    def _log(self, auth: DeviceAuth, name: str, **kw) -> None:
        self.events.log(name, source="backend", run_mode=run_mode_for(auth.client_kind), device_id=auth.device_id,
                        boot_id=auth.boot_id, firmware=auth.firmware, **kw)

    def is_cancelled(self, turn_id: str) -> bool:
        return turn_id in self.cancelled

    def gc(self) -> None:
        now = time.monotonic()
        for tid in [t for t, a in self.audio.items() if now - a.created > self.s.audio_ttl_s]:
            a = self.audio.pop(tid)
            if a.task and not a.task.done():
                a.task.cancel()
        for tid in [t for t, ts in self.cancelled.items() if now - ts > 600]:
            del self.cancelled[tid]

    async def cancel(self, auth: DeviceAuth, turn_id: str) -> bool:
        if not TURN_ID_RE.match(turn_id):
            raise TurnError(400, "bad_turn_id", "Invalid turn id")
        self.cancelled[turn_id] = time.monotonic()
        rolled_back = self.sessions.rollback(turn_id)
        a = self.audio.pop(turn_id, None)
        if a and a.device_id == auth.device_id:
            a.cancelled = True
            if a.task and not a.task.done():
                a.task.cancel()
            await a.finish(failed=True)
        self._log(auth, "interaction_cancelled", turn_id=turn_id, detail="device_cancel",
                  data={"history_rolled_back": rolled_back})
        return True

    def get_audio(self, auth: DeviceAuth, turn_id: str) -> TurnAudio:
        a = self.audio.get(turn_id)
        if not a or a.device_id != auth.device_id:
            raise TurnError(404, "unknown_turn", "No audio for this turn")
        if a.cancelled or self.is_cancelled(turn_id):
            raise TurnError(409, "cancelled", "Turn was cancelled")
        return a

    def release_audio(self, turn_id: str) -> None:
        a = self.audio.pop(turn_id, None)
        if a:
            a.buf = bytearray()   # drop synthesized audio as soon as it has been delivered

    # --------------------------------------------------------------------- turn --
    async def run(self, auth: DeviceAuth, turn_id: str, session_id: str | None, pcm: bytes, capture_quality: int | None,
                  followup: bool, condition: str | None, disconnected: Callable[[], Awaitable[bool]]) -> TurnResponse:
        if not TURN_ID_RE.match(turn_id):
            raise TurnError(400, "bad_turn_id", "Invalid X-Turn-Id")
        if session_id and not SESSION_ID_RE.match(session_id):
            session_id = None
        if len(pcm) % 2:
            raise TurnError(400, "bad_audio", "PCM body must contain whole 16-bit samples")
        if len(pcm) < self.s.min_upload_bytes:
            raise TurnError(422, "audio_too_short", "Recording too short")
        t_turn = time.perf_counter()
        stats = pcm_stats(pcm)
        session, is_new = self.sessions.resolve(auth.device_id, session_id)
        self.sessions.begin_turn(session, turn_id)
        sid = session.session_id
        rc = self.config.research()
        loopback = bool(rc.get("loopback_test"))
        ai_mode = "LOOPBACK" if loopback else self.events.ai_mode
        common = dict(session_id=sid, turn_id=turn_id, condition=condition, ai_mode=ai_mode)
        self._log(auth, "audio_upload_completed", value=len(pcm), duration_ms=stats["seconds"] * 1000,
                  data={"rms_dbfs": round(stats["rms_dbfs"], 1), "peak": stats["peak"],
                        "clipped_fraction": round(stats["clipped_fraction"], 4), "capture_quality": capture_quality,
                        "followup": followup, "new_session": is_new}, **common)
        if stats["all_zero"]:
            # Audio capture failure (device side), distinct from a recognition failure.
            self._log(auth, "error", code="capture_no_signal", detail="all-zero audio", **common)
            raise TurnError(422, "capture_no_signal", "Recording contained no signal (microphone fault)")
        if loopback:
            return await self._loopback(auth, turn_id, sid, pcm, common)
        if not self.s.ai_ready:
            self._log(auth, "error", code="ai_unavailable", detail="OPENAI_API_KEY not configured", **common)
            raise TurnError(503, "ai_unavailable", "AI provider is not configured on the backend")

        async def check_live(stage: str) -> None:
            if self.is_cancelled(turn_id) or not self.sessions.is_current(session, turn_id):
                self._log(auth, "stale_response_rejected", detail=f"after_{stage}", **common)
                raise TurnError(409, "superseded", "Turn was cancelled or superseded")
            if await disconnected():
                self._log(auth, "interaction_cancelled", detail=f"client_disconnected_after_{stage}", **common)
                raise TurnError(409, "client_gone", "Device disconnected")

        def remaining() -> float:
            return max(0.5, self.s.turn_budget_s - (time.perf_counter() - t_turn))

        # ---- speech-to-text ---------------------------------------------------
        self._log(auth, "stt_started", **common)
        t0 = time.perf_counter()
        try:
            stt = await asyncio.wait_for(self.providers.stt.transcribe(pcm_to_wav(pcm)), timeout=min(self.s.stt_timeout_s, remaining()))
        except asyncio.TimeoutError:
            self._log(auth, "error", code="stt_timeout", **common)
            raise TurnError(504, "stt_timeout", "Speech recognition timed out")
        except ProviderError as e:
            self._log(auth, "error", code=f"stt_{e.code}", **common)
            raise TurnError(502, f"stt_{e.code}", "Speech recognition failed")
        stt_ms = (time.perf_counter() - t0) * 1000
        transcript = stt.text.strip()
        words = len(re.findall(r"[A-Za-z']+", transcript))
        self._log(auth, "stt_completed", duration_ms=round(stt_ms, 1), value=words, **common)
        if self._retain_audio(rc):
            self._save_audio(rc, sid, turn_id, pcm)
        pcm = b""   # raw audio is not kept after transcription (unless retention is enabled)

        silence_artifact = transcript.lower().strip(" .!?") in {a.strip(" .!?") for a in _STT_SILENCE_ARTIFACTS}
        if words == 0 or (silence_artifact and capture_quality == 2):
            self._log(auth, "stt_not_understood", value=words, detail="empty_or_silence", **common)
            await check_live("stt")
            return TurnResponse(turn_id=turn_id, session_id=sid, outcome="not_understood", ai_mode=ai_mode,
                                labels=[run_mode_for(auth.client_kind), ai_mode])
        await check_live("stt")

        # ---- language model + safety ------------------------------------------
        ctx = load_approved(self.db)
        self._log(auth, "llm_started", **common)
        try:
            result = await asyncio.wait_for(
                self.agent.respond(transcript, self.sessions.history(session), ctx,
                                   rc.get("emergency_text") or DEFAULT_EMERGENCY_TEXT,
                                   rc.get("self_harm_text") or DEFAULT_SELF_HARM_TEXT),
                timeout=min(self.s.llm_timeout_s, remaining()))
        except asyncio.TimeoutError:
            self._log(auth, "error", code="llm_timeout", **common)
            raise TurnError(504, "llm_timeout", "Language model timed out")
        except ProviderError as e:
            self._log(auth, "error", code=f"llm_{e.code}", **common)
            raise TurnError(502, f"llm_{e.code}", "Language model failed")
        reply, report = result.reply, result.report
        self._log(auth, "llm_completed", duration_ms=round(result.llm_ms, 1), **common, data={
            "model": result.llm.model if result.llm else None,
            "temperature_applied": result.llm.temperature_applied if result.llm else None,
            "incomplete": result.llm.incomplete_reason if result.llm else None,
            "usage": result.llm.usage if result.llm else None,
            "error": result.error_code, "topic": reply.topic, "topic_confidence": reply.topic_confidence,
            "model_topic": report.model_topic, "model_confidence": report.model_confidence,
            "safety_flag": reply.safety_flag, "input_flag": report.input_flag, "model_flag": report.model_flag,
            "replaced": report.replaced, "rules": report.rules, "trimmed_sentences": report.trimmed_sentences,
            "trimmed_questions": report.trimmed_questions, "uses_verified_context": reply.uses_verified_context,
            "model_uses_verified_context": report.model_uses_context, "voice_message": bool(reply.voice_message_id),
            "reply_chars": len(reply.reply_text)})
        if reply.safety_flag != "none":
            # Logged for the research team; NO automatic notification is sent (none is configured).
            self._log(auth, "safety_flag", code=reply.safety_flag, detail=report.input_flag, **common)
        await check_live("llm")
        if not self.sessions.commit(session, turn_id, transcript, reply.reply_text):
            self._log(auth, "stale_response_rejected", detail="commit", **common)
            raise TurnError(409, "superseded", "Turn was superseded")
        if rc.get("retain_transcripts"):
            self._save_transcript(rc, sid, turn_id, transcript, reply.reply_text)
        reply.latency_ms = LatencyMs(stt=round(stt_ms, 1), llm=round(result.llm_ms, 1), tts=None)

        # ---- text-to-speech (streamed to the device while it is generated) ------
        ta = TurnAudio(turn_id, auth.device_id)
        self.audio[turn_id] = ta
        ta.task = asyncio.create_task(self._tts(auth, ta, reply, common))
        self.db.execute("UPDATE devices SET last_interaction_at=datetime('now') WHERE device_id=?", (auth.device_id,))
        return TurnResponse(turn_id=turn_id, session_id=sid, outcome="reply", reply=reply,
                            audio=AudioInfo(url=f"/api/v1/device/turns/{turn_id}/audio"), ai_mode=ai_mode,
                            labels=[run_mode_for(auth.client_kind), ai_mode])

    async def _tts(self, auth: DeviceAuth, ta: TurnAudio, reply: AgentReply, common: dict) -> None:
        self._log(auth, "tts_started", **common)
        t0 = time.perf_counter()
        first_ms = None
        rs = StreamingResampler(self.providers.tts.source_rate, DEVICE_RATE, self.s.tts_gain)
        try:
            async with asyncio.timeout(self.s.tts_timeout_s):
                async for chunk in self.providers.tts.stream(reply.reply_text):
                    out = rs.process(chunk)
                    if out and first_ms is None:
                        first_ms = (time.perf_counter() - t0) * 1000
                        self._log(auth, "tts_first_audio", duration_ms=round(first_ms, 1), **common)
                    await ta.append(out)
                await ta.append(rs.flush())
            if reply.voice_message_id:
                await self._append_voice_message(auth, ta, reply.voice_message_id, common)
            self._log(auth, "tts_completed", duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      value=len(ta.buf), data={"first_audio_ms": first_ms, "model": self.providers.tts.model,
                                               "voice": self.providers.tts.voice}, **common)
            await ta.finish()
        except asyncio.CancelledError:
            await ta.finish(failed=True)
            raise
        except (ProviderError, TimeoutError) as e:
            code = f"tts_{e.code}" if isinstance(e, ProviderError) else "tts_timeout"
            self._log(auth, "error", code=code, **common)
            await ta.finish(failed=True)

    async def _append_voice_message(self, auth, ta: TurnAudio, vid: str, common: dict) -> None:
        row = self.db.one("SELECT filename FROM voice_messages WHERE id=? AND approved=1", (vid,))
        path = self.voice_dir / row["filename"] if row else None
        if not path or not path.exists():
            self._log(auth, "error", code="voice_message_missing", **common)
            return
        await ta.append(b"\x00\x00" * (DEVICE_RATE * 4 // 10))   # 400 ms pause
        data = path.read_bytes()
        for i in range(0, len(data), 8192):
            await ta.append(data[i:i + 8192])
        self._log(auth, "voice_message_played", detail=vid, duration_ms=len(data) / 2 / DEVICE_RATE * 1000, **common)

    async def _loopback(self, auth, turn_id, sid, pcm, common) -> TurnResponse:
        """Hardware audio-path test: plays the recording back. No AI is involved."""
        ta = TurnAudio(turn_id, auth.device_id)
        self.audio[turn_id] = ta
        await ta.append(pcm)
        await ta.finish()
        self._log(auth, "loopback_reply", value=len(pcm), **common)
        reply = AgentReply(reply_text="[loopback test - no AI]", topic="general", topic_confidence=0.0,
                           uses_verified_context=False, safety_flag="none", prompt_version=self.agent.prompt.version)
        return TurnResponse(turn_id=turn_id, session_id=sid, outcome="reply", reply=reply,
                            audio=AudioInfo(url=f"/api/v1/device/turns/{turn_id}/audio"), ai_mode="LOOPBACK",
                            labels=[run_mode_for(auth.client_kind), "LOOPBACK"])

    # ------------------------------------------------------------- retention --
    @staticmethod
    def _retain_audio(rc: dict) -> bool:
        return bool(rc.get("retain_audio")) and bool(rc.get("participant_code"))

    def _save_audio(self, rc: dict, sid: str, turn_id: str, pcm: bytes) -> None:
        d = self.retained_dir / rc["participant_code"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{turn_id}.wav").write_bytes(pcm_to_wav(pcm))

    def _save_transcript(self, rc: dict, sid: str, turn_id: str, user: str, assistant: str) -> None:
        for role, text in (("user", user), ("assistant", assistant)):
            self.db.execute("INSERT INTO transcripts(created_at, participant_code, session_id, turn_id, role, text) "
                            "VALUES(datetime('now'), ?, ?, ?, ?, ?)",
                            (rc.get("participant_code") or None, sid, turn_id, role, text))
