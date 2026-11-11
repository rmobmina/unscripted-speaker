"""Exercises the REAL OpenAI provider code against a local mock HTTP transport.

This verifies request construction (endpoints, model ids, parameters) and the
fallback paths. It does not prove the live API accepts them; see tools/live_roundtrip.py.
"""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.providers import ProviderError
from app.providers.openai_provider import OpenAILLM, OpenAISTT, OpenAITTS
from app.schemas import LLM_OUTPUT_JSON_SCHEMA

GOOD = {"reply_text": "I'm here with you.", "topic": "general", "topic_confidence": 0.8,
        "uses_verified_context": False, "safety_flag": "none", "voice_message_id": None}


def response_obj(text):
    return {"id": "resp_1", "object": "response", "created_at": 0, "status": "completed", "model": "gpt-6-luna",
            "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}


class Recorder:
    def __init__(self, handler):
        self.requests, self.handler = [], handler

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        return self.handler(request, len(self.requests))


def client_for(rec):
    return AsyncOpenAI(api_key="test-key", base_url="https://api.test/v1", max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec)))


S = Settings(_env_file=None, OPENAI_API_KEY="test-key")


def test_stt_request():
    rec = Recorder(lambda r, n: httpx.Response(200, json={"text": " hello there "}))
    res = asyncio.run(OpenAISTT(client_for(rec), S).transcribe(b"RIFF....WAVE"))
    assert res.text == "hello there"
    r = rec.requests[0]
    assert r.url.path == "/v1/audio/transcriptions"
    body = r.content.decode("latin1")
    assert 'name="model"' in body and "gpt-transcribe" in body and 'name="response_format"' in body and "json" in body
    assert 'name="language"' in body


def test_llm_request_shape():
    rec = Recorder(lambda r, n: httpx.Response(200, json=response_obj(json.dumps(GOOD))))
    llm = OpenAILLM(client_for(rec), S)
    res = asyncio.run(llm.generate("INSTR", [{"role": "user", "content": "hi"}], LLM_OUTPUT_JSON_SCHEMA))
    assert json.loads(res.raw_json) == GOOD and res.temperature_applied and res.usage["output_tokens"] == 20
    b = json.loads(rec.requests[0].content)
    assert rec.requests[0].url.path == "/v1/responses"
    assert b["model"] == "gpt-6-luna" and b["store"] is False and b["temperature"] == 0.3
    assert b["max_output_tokens"] == 160 and b["reasoning"] == {"effort": "none"}
    assert b["text"]["format"]["type"] == "json_schema" and b["text"]["format"]["strict"] is True
    assert b["instructions"] == "INSTR"


def test_llm_temperature_rejected_retries_without():
    def h(r, n):
        if "temperature" in json.loads(r.content):
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature'",
                                                       "type": "invalid_request_error", "param": "temperature"}})
        return httpx.Response(200, json=response_obj(json.dumps(GOOD)))
    rec = Recorder(h)
    llm = OpenAILLM(client_for(rec), S)
    res = asyncio.run(llm.generate("i", [], LLM_OUTPUT_JSON_SCHEMA))
    assert not res.temperature_applied and len(rec.requests) == 2
    asyncio.run(llm.generate("i", [], LLM_OUTPUT_JSON_SCHEMA))
    assert len(rec.requests) == 3   # learned: no second rejected attempt


def test_llm_errors_mapped():
    rec = Recorder(lambda r, n: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(ProviderError) as e:
        asyncio.run(OpenAILLM(client_for(rec), S).generate("i", [], {}))
    assert e.value.code == "auth"
    rec = Recorder(lambda r, n: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(ProviderError) as e:
        asyncio.run(OpenAILLM(client_for(rec), S).generate("i", [], {}))
    assert e.value.code == "rate_limited"


def test_missing_key_is_typed_error():
    with pytest.raises(ProviderError) as e:
        asyncio.run(OpenAISTT(None, S).transcribe(b""))
    assert e.value.code == "not_configured"


def test_tts_streams_pcm():
    pcm = b"\x01\x00" * 24000
    rec = Recorder(lambda r, n: httpx.Response(200, content=pcm))
    tts = OpenAITTS(client_for(rec), S)

    async def collect():
        return b"".join([c async for c in tts.stream("Hello.")])
    assert asyncio.run(collect()) == pcm
    b = json.loads(rec.requests[0].content)
    assert rec.requests[0].url.path == "/v1/audio/speech"
    assert b["model"] == "gpt-4o-mini-tts-2025-12-15" and b["voice"] == "marin" and b["response_format"] == "pcm"
    assert b["speed"] == 0.9 and "calmly" in b["instructions"]


def test_tts_speed_rejected_retries():
    def h(r, n):
        if "speed" in json.loads(r.content):
            return httpx.Response(400, json={"error": {"message": "speed is not supported for this model"}})
        return httpx.Response(200, content=b"\x00\x00" * 100)
    rec = Recorder(h)
    tts = OpenAITTS(client_for(rec), S)

    async def collect():
        return b"".join([c async for c in tts.stream("Hi.")])
    assert len(asyncio.run(collect())) == 200 and tts.speed_supported is False
