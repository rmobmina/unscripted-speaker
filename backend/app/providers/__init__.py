"""Speech-to-text, language model and text-to-speech providers.

Each stage is a separate object so its latency can be measured and the
implementation swapped without touching the rest of the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


class ProviderError(Exception):
    """A provider call failed. `code` is safe to log; the message never contains user content."""

    def __init__(self, stage: str, code: str, message: str = ""):
        super().__init__(f"{stage}:{code} {message}".strip())
        self.stage, self.code = stage, code


@dataclass
class STTResult:
    text: str
    model: str


@dataclass
class LLMResult:
    raw_json: str | None
    model: str
    temperature_applied: bool
    incomplete_reason: str | None = None
    usage: dict | None = None


class STTProvider(Protocol):
    model: str

    async def transcribe(self, wav: bytes) -> STTResult: ...


class LLMProvider(Protocol):
    model: str

    async def generate(self, instructions: str, messages: list[dict], schema: dict) -> LLMResult: ...


class TTSProvider(Protocol):
    model: str
    voice: str
    source_rate: int

    def stream(self, text: str) -> AsyncIterator[bytes]: ...


@dataclass
class Providers:
    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    label: str          # LIVE_AI | MOCK_PROVIDER


def build_providers(settings) -> Providers:
    if settings.ai_provider == "mock":
        from .mock import MockLLM, MockSTT, MockTTS
        return Providers(MockSTT(), MockLLM(), MockTTS(), "MOCK_PROVIDER")
    from .openai_provider import OpenAILLM, OpenAISTT, OpenAITTS, make_client
    client = make_client(settings)
    return Providers(OpenAISTT(client, settings), OpenAILLM(client, settings), OpenAITTS(client, settings), "LIVE_AI")
