"""MOCK providers - for automated tests and offline harness checks ONLY.

Selected only by UNSCRIPTED_AI_PROVIDER=mock. Every event produced while these
are active is labelled ai_mode=MOCK_PROVIDER and the dashboard shows a banner.
They do not understand speech or language; they must never be presented as
the working AI integration.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import numpy as np

from . import LLMResult, STTResult


class MockSTT:
    model = "mock-stt"

    def __init__(self, text: str = "I don't know where my daughter is."):
        self.text = text
        self.calls = 0

    async def transcribe(self, wav: bytes) -> STTResult:
        self.calls += 1
        return STTResult(text=self.text, model=self.model)


class MockLLM:
    model = "mock-llm"

    def __init__(self, reply: dict | None = None):
        self.reply = reply or {
            "reply_text": "It sounds like you're missing her. I'm not sure where she is right now, but I'm here with you.",
            "topic": "family", "topic_confidence": 0.9, "uses_verified_context": False,
            "safety_flag": "none", "voice_message_id": None,
        }
        self.calls: list[dict] = []

    async def generate(self, instructions: str, messages: list[dict], schema: dict) -> LLMResult:
        self.calls.append({"instructions": instructions, "messages": messages})
        raw = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return LLMResult(raw_json=raw, model=self.model, temperature_applied=False)


class MockTTS:
    model = "mock-tts"
    voice = "mock-tone"
    source_rate = 24000

    def __init__(self, seconds: float = 0.6):
        self.seconds = seconds

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        # A quiet tone, clearly not speech, so nobody mistakes it for a real reply.
        n = int(self.source_rate * self.seconds)
        t = np.arange(n) / self.source_rate
        pcm = (0.1 * 32767 * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes()
        for i in range(0, len(pcm), 4096):
            yield pcm[i:i + 4096]
