"""Real OpenAI integration (official `openai` Python SDK).

Endpoints used (checked against developers.openai.com, Nov 2026):
  * POST /v1/audio/transcriptions   (STT; json response format)
  * POST /v1/responses              (LLM; strict json_schema structured output)
  * POST /v1/audio/speech           (TTS; response_format="pcm" = 24 kHz s16le mono, streamed)
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import openai
from openai import AsyncOpenAI

from . import LLMResult, ProviderError, STTResult

log = logging.getLogger("unscripted.openai")


def make_client(settings) -> AsyncOpenAI | None:
    if not settings.openai_api_key or not settings.openai_api_key.get_secret_value():
        return None
    return AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), base_url=settings.openai_base_url,
                       max_retries=1)   # one bounded SDK retry on connection errors / 5xx / 429


def _require(client: AsyncOpenAI | None, stage: str) -> AsyncOpenAI:
    if client is None:
        raise ProviderError(stage, "not_configured", "OPENAI_API_KEY is not set")
    return client


def _err(stage: str, e: Exception) -> ProviderError:
    if isinstance(e, openai.APITimeoutError):
        return ProviderError(stage, "timeout")
    if isinstance(e, openai.AuthenticationError):
        return ProviderError(stage, "auth")
    if isinstance(e, openai.RateLimitError):
        body = getattr(e, "body", None)
        code = body.get("code") or body.get("type") if isinstance(body, dict) else None
        if code in ("insufficient_quota", "credit_balance_exhausted"):
            return ProviderError(stage, "no_credits")   # billing, not load: retrying will not help
        return ProviderError(stage, "rate_limited")
    if isinstance(e, openai.BadRequestError):
        return ProviderError(stage, "bad_request", getattr(e, "message", "")[:200])
    if isinstance(e, openai.APIConnectionError):
        return ProviderError(stage, "connection")
    if isinstance(e, openai.APIStatusError):
        return ProviderError(stage, f"http_{e.status_code}")
    return ProviderError(stage, type(e).__name__)


class OpenAISTT:
    def __init__(self, client, settings):
        self.client, self.model, self.language = client, settings.stt_model, settings.stt_language
        self.timeout = settings.stt_timeout_s

    async def transcribe(self, wav: bytes) -> STTResult:
        c = _require(self.client, "stt")
        try:
            r = await c.audio.transcriptions.create(model=self.model, file=("speech.wav", wav, "audio/wav"),
                                                    language=self.language, response_format="json",
                                                    timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - mapped to a typed, content-free error
            raise _err("stt", e) from e
        return STTResult(text=(getattr(r, "text", "") or "").strip(), model=self.model)


class OpenAILLM:
    def __init__(self, client, settings):
        self.client, self.model = client, settings.llm_model
        self.effort = settings.llm_reasoning_effort
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_output_tokens
        self.timeout = settings.llm_timeout_s
        self._temperature_supported: bool | None = None   # learned once, then logged per call

    async def generate(self, instructions: str, messages: list[dict], schema: dict) -> LLMResult:
        c = _require(self.client, "llm")
        kwargs: dict = dict(
            model=self.model,
            instructions=instructions,
            input=messages,
            text={"format": {"type": "json_schema", "name": "companion_reply", "schema": schema, "strict": True}},
            max_output_tokens=self.max_tokens,
            store=False,                 # do not retain conversation state at the provider
            timeout=self.timeout,
        )
        if self.effort:
            kwargs["reasoning"] = {"effort": self.effort}
        use_temp = self.temperature is not None and self._temperature_supported is not False
        if use_temp:
            kwargs["temperature"] = self.temperature
        try:
            r = await c.responses.create(**kwargs)
        except openai.BadRequestError as e:
            msg = (getattr(e, "message", "") or str(e)).lower()
            if use_temp and "temperature" in msg:
                # Some reasoning models reject sampling parameters. Record it (reproducibility)
                # and retry once without temperature rather than failing the turn.
                log.warning("model %s rejected temperature; continuing without it", self.model)
                self._temperature_supported = False
                kwargs.pop("temperature", None)
                use_temp = False
                try:
                    r = await c.responses.create(**kwargs)
                except Exception as e2:  # noqa: BLE001
                    raise _err("llm", e2) from e2
            else:
                raise _err("llm", e) from e
        except Exception as e:  # noqa: BLE001
            raise _err("llm", e) from e
        if use_temp:
            self._temperature_supported = True
        incomplete = None
        if getattr(r, "status", None) == "incomplete":
            details = getattr(r, "incomplete_details", None)
            incomplete = getattr(details, "reason", "incomplete") if details else "incomplete"
        usage = None
        if getattr(r, "usage", None) is not None:
            u = r.usage
            usage = {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None)}
        return LLMResult(raw_json=getattr(r, "output_text", None) or None, model=getattr(r, "model", self.model),
                         temperature_applied=use_temp, incomplete_reason=incomplete, usage=usage)


class OpenAITTS:
    source_rate = 24000   # OpenAI `pcm` format: 24 kHz, 16-bit signed little-endian, mono

    def __init__(self, client, settings):
        self.client, self.model, self.voice = client, settings.tts_model, settings.tts_voice
        self.speed, self.instructions = settings.tts_speed, settings.tts_instructions
        self.timeout = settings.tts_timeout_s
        self.speed_supported: bool | None = None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        c = _require(self.client, "tts")
        kwargs = dict(model=self.model, voice=self.voice, input=text, response_format="pcm",
                      instructions=self.instructions, timeout=self.timeout)
        if self.speed_supported is not False:
            kwargs["speed"] = self.speed
        try:
            async with c.audio.speech.with_streaming_response.create(**kwargs) as resp:
                async for chunk in resp.iter_bytes(4096):
                    yield chunk
            if "speed" in kwargs:
                self.speed_supported = True
        except openai.BadRequestError as e:
            msg = (getattr(e, "message", "") or str(e)).lower()
            if "speed" in msg and "speed" in kwargs:
                log.warning("TTS model %s rejected speed; continuing at default speed", self.model)
                self.speed_supported = False
                kwargs.pop("speed")
                try:
                    async with c.audio.speech.with_streaming_response.create(**kwargs) as resp:
                        async for chunk in resp.iter_bytes(4096):
                            yield chunk
                except Exception as e2:  # noqa: BLE001
                    raise _err("tts", e2) from e2
            else:
                raise _err("tts", e) from e
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _err("tts", e) from e
