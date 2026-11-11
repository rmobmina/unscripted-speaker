"""Typed, validated data shapes shared by the API, agent and tests."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Topic = Literal["reassurance", "family", "routine", "relaxation", "general"]
SafetyFlag = Literal["none", "notify_caregiver", "emergency_guidance"]
Condition = Literal["B1", "B2", "B3"]
TOPICS: tuple[str, ...] = ("reassurance", "family", "routine", "relaxation", "general")
SAFETY_ORDER = {"none": 0, "notify_caregiver": 1, "emergency_guidance": 2}
TOPIC_MIN_CONFIDENCE = 0.60

# JSON Schema sent to the model (strict structured output). Everything the model
# returns is re-validated by LLMOutput below and then by the safety layer.
LLM_OUTPUT_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply_text": {"type": "string", "description": "What the companion says aloud. At most three short sentences and at most one question."},
        "topic": {"type": "string", "enum": list(TOPICS)},
        "topic_confidence": {"type": "number", "description": "0 to 1"},
        "uses_verified_context": {"type": "boolean"},
        "safety_flag": {"type": "string", "enum": ["none", "notify_caregiver", "emergency_guidance"]},
        "voice_message_id": {"type": ["string", "null"], "description": "ID of an approved familiar voice message to play after the reply, only if the person accepted an offer to hear it. Otherwise null."},
    },
    "required": ["reply_text", "topic", "topic_confidence", "uses_verified_context", "safety_flag", "voice_message_id"],
}


class LLMOutput(BaseModel):
    """Raw model output after JSON parsing (not yet trusted)."""

    model_config = ConfigDict(extra="forbid")
    reply_text: str = Field(min_length=1, max_length=600)
    topic: Topic
    topic_confidence: float
    uses_verified_context: bool
    safety_flag: SafetyFlag
    voice_message_id: str | None = Field(default=None, max_length=64)

    @field_validator("topic_confidence")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return min(1.0, max(0.0, v))


class LatencyMs(BaseModel):
    stt: float | None = None
    llm: float | None = None
    tts: float | None = None   # time to first TTS audio; null when not yet measured


class AgentReply(BaseModel):
    """Validated reply that is allowed to leave the backend."""

    reply_text: str = Field(min_length=1, max_length=400)
    topic: Topic
    topic_confidence: float = Field(ge=0.0, le=1.0)
    uses_verified_context: bool
    safety_flag: SafetyFlag
    prompt_version: str
    latency_ms: LatencyMs = Field(default_factory=LatencyMs)
    voice_message_id: str | None = None


class AudioInfo(BaseModel):
    url: str
    format: Literal["pcm_s16le;rate=16000;channels=1"] = "pcm_s16le;rate=16000;channels=1"


class ErrorInfo(BaseModel):
    code: str
    message: str


class TurnResponse(BaseModel):
    turn_id: str
    session_id: str
    outcome: Literal["reply", "not_understood"]
    reply: AgentReply | None = None
    audio: AudioInfo | None = None
    error: ErrorInfo | None = None
    ai_mode: str
    labels: list[str] = Field(default_factory=list)


class DeviceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(max_length=48, pattern=r"^[a-z_]+$")
    t_mono_ms: int | None = Field(None, ge=0)
    wall_ms: int | None = Field(None, ge=0)
    turn_id: str = Field("", max_length=48)
    state: str = Field("", max_length=16)
    condition: str = Field("", max_length=4)
    value: float | None = None
    code: int | None = None
    detail: str = Field("", max_length=40)


class DeviceEventBatch(BaseModel):
    boot_id: str = Field(max_length=16)
    dropped: int = 0
    events: list[DeviceEvent] = Field(max_length=64)
