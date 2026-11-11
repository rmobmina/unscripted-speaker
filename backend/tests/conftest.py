import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers import Providers
from app.providers.mock import MockLLM, MockSTT, MockTTS

FMT = "pcm_s16le;rate=16000;channels=1"


def speech_pcm(seconds=1.0, amp=3000, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(16000 * seconds)) * amp).astype("<i2").tobytes()


@pytest.fixture
def mocks():
    return MockSTT(), MockLLM(), MockTTS()


@pytest.fixture
def settings(tmp_path):
    return Settings(UNSCRIPTED_AI_PROVIDER="mock", DATA_DIR=tmp_path, RESEARCHER_TOKEN="res-token",
                    CAREGIVER_TOKEN="care-token", OPENAI_API_KEY=None, _env_file=None)


@pytest.fixture
def app(settings, mocks):
    stt, llm, tts = mocks
    return create_app(settings, Providers(stt, llm, tts, "MOCK_PROVIDER"))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def dev(app, client):
    tok = app.state.ctx.provision_device("dev1")
    return {"Authorization": f"Bearer {tok}", "X-Device-Id": "dev1", "X-Client-Kind": "simulator", "X-Boot-Id": "b00t"}


RES = {"Authorization": "Bearer res-token"}
CARE = {"Authorization": "Bearer care-token"}


def post_turn(client, dev, turn_id="b00t-1", pcm=None, **hdr):
    pcm = speech_pcm() if pcm is None else pcm
    h = dev | {"X-Turn-Id": turn_id, "X-Audio-Format": FMT} | hdr
    return client.post("/api/v1/device/turns", content=pcm, headers=h)
