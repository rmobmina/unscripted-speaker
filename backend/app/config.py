"""Centralised, validated configuration (environment variables / backend/.env).

Secrets are only read from the environment; nothing secret is ever written to
logs, research events, the device, or the dashboard.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_DIR / ".env"), env_file_encoding="utf-8",
                                      extra="ignore")

    # ---- AI provider ------------------------------------------------------------
    # "openai" is the real integration. "mock" exists ONLY for automated tests and
    # offline harness checks; every event produced with it is labelled MOCK_PROVIDER
    # and the dashboard shows a banner. It is never selected automatically.
    ai_provider: Literal["openai", "mock"] = Field("openai", alias="UNSCRIPTED_AI_PROVIDER")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(None, alias="OPENAI_BASE_URL")

    # Pinned model identifiers (see docs/RESEARCH_REPRODUCIBILITY.md for why each was chosen).
    stt_model: str = Field("gpt-transcribe", alias="STT_MODEL")
    stt_language: str = Field("en", alias="STT_LANGUAGE")
    llm_model: str = Field("gpt-6-luna", alias="LLM_MODEL")
    llm_reasoning_effort: Literal["none", "low", "medium", "high"] | None = Field("none", alias="LLM_REASONING_EFFORT")
    llm_temperature: float | None = Field(0.3, alias="LLM_TEMPERATURE")
    llm_max_output_tokens: int = Field(160, alias="LLM_MAX_OUTPUT_TOKENS")
    tts_model: str = Field("gpt-4o-mini-tts-2025-12-15", alias="TTS_MODEL")
    tts_voice: str = Field("marin", alias="TTS_VOICE")
    tts_speed: float = Field(0.9, alias="TTS_SPEED")
    tts_instructions: str = Field(
        "Speak calmly, warmly and clearly at a relaxed, unhurried pace, as one respectful adult to another. "
        "Do not sound excited, cheerful or theatrical.", alias="TTS_INSTRUCTIONS")
    tts_gain: float = Field(1.0, alias="TTS_GAIN")

    prompt_path: Path = Field(REPO_DIR / "prompts" / "unscripted_system_v1.0.md", alias="PROMPT_PATH")
    history_turns: int = Field(6, alias="HISTORY_TURNS")

    # ---- timing budgets (device gives up after 12 s) ------------------------------
    stt_timeout_s: float = Field(6.0, alias="STT_TIMEOUT_S")
    llm_timeout_s: float = Field(6.0, alias="LLM_TIMEOUT_S")
    tts_timeout_s: float = Field(10.0, alias="TTS_TIMEOUT_S")
    turn_budget_s: float = Field(11.0, alias="TURN_BUDGET_S")
    session_expiry_s: int = Field(600, alias="SESSION_EXPIRY_S")
    audio_ttl_s: int = Field(120, alias="AUDIO_TTL_S")

    # ---- limits ----------------------------------------------------------------------
    max_upload_bytes: int = Field(16000 * 2 * 21, alias="MAX_UPLOAD_BYTES")   # 21 s of 16 kHz mono
    min_upload_bytes: int = Field(16000 * 2 * 3 // 10, alias="MIN_UPLOAD_BYTES")
    max_voice_message_s: int = Field(60, alias="MAX_VOICE_MESSAGE_S")

    # ---- dashboard access ------------------------------------------------------------
    researcher_token: SecretStr | None = Field(None, alias="RESEARCHER_TOKEN")
    caregiver_token: SecretStr | None = Field(None, alias="CAREGIVER_TOKEN")

    data_dir: Path = Field(BACKEND_DIR / "data", alias="DATA_DIR")

    @field_validator("tts_speed")
    @classmethod
    def _speed(cls, v: float) -> float:
        if not 0.25 <= v <= 4.0:
            raise ValueError("TTS_SPEED must be within 0.25-4.0 (OpenAI API range)")
        return v

    @field_validator("llm_temperature")
    @classmethod
    def _temp(cls, v: float | None) -> float | None:
        if v is not None and not 0.0 <= v <= 2.0:
            raise ValueError("LLM_TEMPERATURE must be within 0-2")
        return v

    @field_validator("openai_api_key", "researcher_token", "caregiver_token", mode="before")
    @classmethod
    def _placeholder_is_unset(cls, v):
        # Template placeholders (".env.example") must never count as configured secrets.
        if v is None or (isinstance(v, str) and (not v.strip() or "REPLACE" in v.upper())):
            return None
        return v

    @property
    def ai_ready(self) -> bool:
        return self.ai_provider == "mock" or bool(self.openai_api_key and self.openai_api_key.get_secret_value())

    @property
    def ai_mode_label(self) -> str:
        return "MOCK_PROVIDER" if self.ai_provider == "mock" else "LIVE_AI"


@lru_cache
def get_settings() -> Settings:
    return Settings()
