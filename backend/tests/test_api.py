import json

from .conftest import CARE, FMT, RES, post_turn, speech_pcm


def test_health(client):
    r = client.get("/healthz").json()
    assert r["ok"] and r["ai_mode"] == "MOCK_PROVIDER"


def test_device_auth_required(client, dev):
    assert client.get("/api/v1/device/config").status_code == 401
    bad = dev | {"Authorization": "Bearer nope"}
    assert client.get("/api/v1/device/config", headers=bad).status_code == 401
    other = dev | {"X-Device-Id": "dev2"}
    assert client.get("/api/v1/device/config", headers=other).status_code == 401
    assert client.get("/api/v1/device/config", headers=dev).status_code == 200


def test_token_stored_hashed(app, dev):
    row = app.state.ctx.db.one("SELECT token_hash FROM devices WHERE device_id='dev1'")
    assert dev["Authorization"][7:] not in row["token_hash"]


def test_config_etag(client, dev):
    r = client.get("/api/v1/device/config", headers=dev)
    etag = r.headers["etag"]
    assert client.get("/api/v1/device/config", headers=dev | {"If-None-Match": etag}).status_code == 304


def test_turn_roundtrip_and_labels(client, dev):
    r = post_turn(client, dev)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["outcome"] == "reply" and j["ai_mode"] == "MOCK_PROVIDER"
    assert "SOFTWARE_SIMULATOR" in j["labels"]
    a = client.get(j["audio"]["url"], headers=dev)
    assert a.status_code == 200 and len(a.content) > 16000 and len(a.content) % 2 == 0
    # audio is released after delivery
    assert client.get(j["audio"]["url"], headers=dev).status_code == 404


def test_real_hardware_label(client, dev):
    j = post_turn(client, dev | {"X-Client-Kind": "device"}).json()
    assert "REAL_HARDWARE" in j["labels"]


def test_upload_validation(client, dev):
    assert post_turn(client, dev, pcm=speech_pcm(0.1)).status_code == 422          # too short
    assert post_turn(client, dev, pcm=speech_pcm(22)).status_code == 413           # too long
    assert post_turn(client, dev, pcm=b"\x01" * 9999).status_code == 400           # odd byte count
    assert post_turn(client, dev, turn_id="bad id!").status_code == 400
    assert post_turn(client, dev, **{"X-Audio-Format": "mp3"}).status_code == 415
    r = post_turn(client, dev, pcm=b"\x00" * 32000)                                  # all-zero = mic fault
    assert r.status_code == 422 and r.json()["error"]["code"] == "capture_no_signal"


def test_audio_other_device_forbidden(app, client, dev):
    j = post_turn(client, dev).json()
    tok2 = app.state.ctx.provision_device("dev2")
    h2 = {"Authorization": f"Bearer {tok2}", "X-Device-Id": "dev2"}
    assert client.get(j["audio"]["url"], headers=h2).status_code == 404


def test_cancel_rolls_back_history(app, client, dev):
    j = post_turn(client, dev, "b00t-1").json()
    sid = j["session_id"]
    assert len(app.state.ctx.sessions._s[sid].history) == 1
    assert client.delete("/api/v1/device/turns/b00t-1", headers=dev).status_code == 200
    assert len(app.state.ctx.sessions._s[sid].history) == 0
    assert client.get(j["audio"]["url"], headers=dev).status_code in (404, 409)


def test_session_continuity_and_history_sent(client, dev, mocks):
    _, llm, _ = mocks
    sid = post_turn(client, dev, "b00t-1").json()["session_id"]
    j2 = post_turn(client, dev, "b00t-2", **{"X-Session-Id": sid}).json()
    assert j2["session_id"] == sid
    msgs = llm.calls[-1]["messages"]
    assert len(msgs) == 3 and msgs[1]["role"] == "assistant"


def test_session_reset_from_dashboard(client, dev):
    sid = post_turn(client, dev, "b00t-1").json()["session_id"]
    client.post("/api/v1/admin/session/reset", headers=CARE)
    j = post_turn(client, dev, "b00t-2", **{"X-Session-Id": sid}).json()
    assert j["session_id"] != sid


def test_not_understood(client, dev, mocks):
    mocks[0].text = ""
    j = post_turn(client, dev).json()
    assert j["outcome"] == "not_understood" and j["audio"] is None
    mocks[0].text = "Thank you."
    assert post_turn(client, dev, "b00t-2", **{"X-Capture-Quality": "2"}).json()["outcome"] == "not_understood"
    assert post_turn(client, dev, "b00t-3", **{"X-Capture-Quality": "0"}).json()["outcome"] == "reply"


def test_ai_not_configured_returns_503(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings(UNSCRIPTED_AI_PROVIDER="openai", OPENAI_API_KEY=None, DATA_DIR=tmp_path, _env_file=None)
    app = create_app(s)
    with TestClient(app) as c:
        tok = app.state.ctx.provision_device("d")
        h = {"Authorization": f"Bearer {tok}", "X-Device-Id": "d", "X-Turn-Id": "a-1", "X-Audio-Format": FMT}
        r = c.post("/api/v1/device/turns", content=speech_pcm(), headers=h)
        assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable"
        assert c.get("/healthz").json()["ai_ready"] is False


def test_no_transcript_or_reply_in_events(app, client, dev, mocks):
    mocks[0].text = "My secret phrase is pineapple"
    post_turn(client, dev)
    blob = json.dumps(app.state.ctx.db.query("SELECT * FROM events"))
    assert "pineapple" not in blob and "missing her" not in blob
    assert app.state.ctx.db.query("SELECT * FROM transcripts") == []
    assert not (app.state.ctx.settings.data_dir / "retained_audio").exists()


def test_retention_requires_consent(client, dev, app, mocks):
    r = client.put("/api/v1/admin/research-config", headers=RES,
                   json={"changes": {"retain_transcripts": True}})
    assert r.status_code == 422
    r = client.put("/api/v1/admin/research-config", headers=RES,
                   json={"changes": {"retain_transcripts": True, "participant_code": "P01"}, "consent_recorded": True})
    assert r.status_code == 200
    mocks[0].text = "hello there"
    post_turn(client, dev)
    rows = app.state.ctx.db.query("SELECT role, participant_code FROM transcripts")
    assert {r["role"] for r in rows} == {"user", "assistant"} and rows[0]["participant_code"] == "P01"


def test_loopback_mode(client, dev, mocks):
    client.put("/api/v1/admin/research-config", headers=RES, json={"changes": {"loopback_test": True}})
    pcm = speech_pcm()
    j = post_turn(client, dev, pcm=pcm).json()
    assert j["ai_mode"] == "LOOPBACK"
    assert client.get(j["audio"]["url"], headers=dev).content == pcm
    assert mocks[1].calls == []


def test_device_events_and_metrics(client, dev):
    ev = [{"name": "button_pressed", "t_mono_ms": 1000, "turn_id": "b00t-1"},
          {"name": "state_changed", "t_mono_ms": 1040, "detail": "IDLE>LISTENING"},
          {"name": "listening_ended", "t_mono_ms": 3000, "turn_id": "b00t-1", "detail": "release"},
          {"name": "playback_started", "t_mono_ms": 5200, "turn_id": "b00t-1"},
          {"name": "led_stats", "t_mono_ms": 60000, "value": 3, "detail": "frames=3000"},
          {"name": "led_stats", "t_mono_ms": 120000, "value": 5, "detail": "frames=6000"}]
    d = dev | {"X-Client-Kind": "device"}
    assert client.post("/api/v1/device/events", headers=d, json={"boot_id": "b00t", "events": ev}).status_code == 200
    m = client.get("/api/v1/admin/metrics", headers=RES).json()
    g = [g for g in m["groups"] if g["run_mode"] == "REAL_HARDWARE"][0]
    assert g["button_to_listening_state_ms"]["median"] == 40
    assert g["release_to_first_audio_ms"]["median"] == 2200
    assert g["led_frames"] == 6000 and g["led_dropped_frame_pct"] == round(100 * 5 / 6000, 3)


def test_bad_event_rejected(client, dev):
    r = client.post("/api/v1/device/events", headers=dev, json={"boot_id": "b", "events": [{"name": "DROP TABLE"}]})
    assert r.status_code == 422


def test_roles(client):
    assert client.get("/api/v1/admin/status").status_code == 401
    assert client.get("/api/v1/admin/status", headers=CARE).status_code == 200
    assert client.post("/api/v1/admin/condition", headers=CARE, json={"condition": "B1"}).status_code == 403
    assert client.get("/api/v1/admin/export", headers=CARE).status_code == 403
    assert client.put("/api/v1/admin/device-config", headers=CARE, json={"changes": {"volume": 50}}).status_code == 200
    assert client.put("/api/v1/admin/device-config", headers=CARE, json={"changes": {"led_offset": 2}}).status_code == 403


def test_config_validation(client):
    for bad in ({"volume": 101}, {"max_brightness_pct": 80}, {"condition": "B4"}, {"lights_off": "yes"}, {"nope": 1}):
        assert client.put("/api/v1/admin/device-config", headers=RES, json={"changes": bad}).status_code in (403, 422), bad


def test_condition_ack_latency(client, dev):
    v = client.post("/api/v1/admin/condition", headers=RES, json={"condition": "B1"}).json()["device_config"]["version"]
    client.get("/api/v1/device/config", headers=dev | {"X-Applied-Version": str(v)})
    m = client.get("/api/v1/admin/metrics", headers=RES).json()
    assert any(g["condition_update_ms"]["n"] == 1 for g in m["groups"])


def test_context_crud_and_approval(client, app):
    r = client.post("/api/v1/admin/context/people", headers=CARE,
                    json={"name": "Anna", "relationship": "daughter", "status": "living", "approved": False})
    pid = r.json()["id"]
    assert "Anna" not in client.get("/api/v1/admin/context", headers=CARE).json()["prompt_preview"]
    client.put(f"/api/v1/admin/context/people/{pid}", headers=CARE,
               json={"name": "Anna", "relationship": "daughter", "status": "living", "approved": True})
    assert "Anna" in client.get("/api/v1/admin/context", headers=CARE).json()["prompt_preview"]
    blob = json.dumps(app.state.ctx.db.query("SELECT * FROM events"))
    assert "Anna" not in blob
    assert client.delete("/api/v1/admin/context?confirm=DELETE", headers=CARE).json()["deleted_items"] >= 1
    assert client.get("/api/v1/admin/context", headers=CARE).json()["people"] == []


def test_voice_message_upload(client, app):
    from app.audio import pcm_to_wav
    wav = pcm_to_wav(speech_pcm(2.0), 16000)
    r = client.post("/api/v1/admin/voice-messages?label=Hello&approved=true", headers=CARE,
                    files={"file": ("m.wav", wav, "audio/wav")})
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    assert client.get(f"/api/v1/admin/voice-messages/{vid}/audio", headers=CARE).status_code == 200
    bad = client.post("/api/v1/admin/voice-messages?label=x", headers=CARE, files={"file": ("m.wav", b"junk", "audio/wav")})
    assert bad.status_code == 422
    assert client.delete(f"/api/v1/admin/voice-messages/{vid}", headers=CARE).json()["deleted"] == 1


def test_export_has_no_secrets(client, dev):
    post_turn(client, dev)
    csv = client.get("/api/v1/admin/export?fmt=csv", headers=RES).text
    assert dev["Authorization"][7:] not in csv and "res-token" not in csv
    assert "audio_upload_completed" in csv


def test_validation_error_does_not_echo(client):
    r = client.post("/api/v1/admin/context/people", headers=CARE, json={"name": "SecretName" * 20, "relationship": "x"})
    assert r.status_code == 422 and "SecretName" not in r.text


def test_placeholder_key_not_ready_and_labelled(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings(UNSCRIPTED_AI_PROVIDER="openai", OPENAI_API_KEY="sk-REPLACE_ME", DATA_DIR=tmp_path, _env_file=None)
    assert not s.ai_ready
    app = create_app(s)
    with TestClient(app):
        assert app.state.ctx.events.ai_mode == "AI_NOT_CONFIGURED"
