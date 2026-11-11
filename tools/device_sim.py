#!/usr/bin/env python3
"""SOFTWARE SIMULATOR of the [un]scripted device (Mac microphone/speaker or WAV input).

Speaks the exact device HTTP protocol (same endpoints, headers, PCM format) to a
running backend, labelled X-Client-Kind: simulator, so every event and metric it
produces is tagged SOFTWARE_SIMULATOR and never pooled with REAL_HARDWARE data.

  tools/device_sim.py say "I miss my daughter"        # macOS `say` voice -> real STT/LLM/TTS round trip
  tools/device_sim.py wav recording.wav                # any WAV file
  tools/device_sim.py mic                              # press Enter to start/stop recording (Mac mic)
  tools/device_sim.py bench --n 20 "Where am I?"       # latency benchmark (simulator path)

Options: --backend http://localhost:8000  --no-play  --session-reuse
The simulator token is provisioned automatically into backend/data/ (git-ignored).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
FMT = "pcm_s16le;rate=16000;channels=1"
DEVICE_ID = "sim-01"
BANNER = "*** SOFTWARE_SIMULATOR - not real hardware ***"


def sim_token() -> str:
    from app.config import get_settings
    from app.db import Database
    from app.security import hash_token, new_token
    s = get_settings()
    path = s.data_dir / f"{DEVICE_ID}.token"
    db = Database(s.data_dir / "unscripted.db")
    if path.exists():
        tok = path.read_text().strip()
        row = db.one("SELECT token_hash FROM devices WHERE device_id=?", (DEVICE_ID,))
        if row and row["token_hash"] == hash_token(tok):
            return tok
    tok = new_token()
    db.execute("INSERT INTO devices(device_id, token_hash, label, created_at) VALUES(?,?,?,datetime('now')) "
               "ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash",
               (DEVICE_ID, hash_token(tok), "Mac software simulator"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def wav_to_pcm16k(data: bytes) -> bytes:
    from app.audio import decode_wav_to_device_pcm
    pcm, _ = decode_wav_to_device_pcm(data, 20)
    return pcm


def say_pcm(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "s.wav"
        subprocess.run(["say", "-o", str(out), "--data-format=LEI16@16000", text], check=True)
        return wav_to_pcm16k(out.read_bytes())


def mic_pcm() -> bytes:
    import sounddevice as sd
    frames: list[np.ndarray] = []
    input("Press Enter to START talking...")
    stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", callback=lambda d, *_: frames.append(d.copy()))
    with stream:
        input("Recording... press Enter to STOP.")
    return np.concatenate(frames).tobytes() if frames else b""


class Sim:
    def __init__(self, backend: str, play: bool):
        self.backend, self.play = backend.rstrip("/"), play
        self.boot = secrets.token_hex(4)
        self.n = 0
        self.session: str | None = None
        self.t0 = time.monotonic()
        self.h = {"Authorization": f"Bearer {sim_token()}", "X-Device-Id": DEVICE_ID, "X-Client-Kind": "simulator",
                  "X-Boot-Id": self.boot, "X-Firmware-Version": "simulator"}
        self.http = httpx.Client(timeout=30)
        self.events: list[dict] = []

    def mono(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def ev(self, name, turn_id="", detail=""):
        self.events.append({"name": name, "t_mono_ms": self.mono(), "wall_ms": int(time.time() * 1000),
                            "turn_id": turn_id, "detail": detail})

    def flush_events(self):
        if self.events:
            self.http.post(f"{self.backend}/api/v1/device/events", headers=self.h,
                           json={"boot_id": self.boot, "events": self.events})
            self.events = []

    def turn(self, pcm: bytes, reuse_session: bool = True) -> dict:
        self.n += 1
        tid = f"{self.boot}-{self.n}"
        cfg = self.http.get(f"{self.backend}/api/v1/device/config", headers=self.h).json()
        self.ev("listening_ended", tid, "release")
        t_rel = time.perf_counter()
        h = self.h | {"X-Turn-Id": tid, "X-Audio-Format": FMT, "X-Condition": cfg.get("condition", "B2"),
                      "X-Capture-Quality": "0"}
        if reuse_session and self.session:
            h["X-Session-Id"] = self.session
        r = self.http.post(f"{self.backend}/api/v1/device/turns", headers=h, content=pcm)
        t_resp = time.perf_counter()
        j = r.json()
        res = {"turn_id": tid, "status": r.status_code, "response_ms": round((t_resp - t_rel) * 1000)}
        if r.status_code != 200:
            res["error"] = j.get("error")
            self.flush_events()
            return res
        self.session = j["session_id"]
        res.update(outcome=j["outcome"], labels=j.get("labels"))
        if j["outcome"] != "reply":
            self.flush_events()
            return res
        rep = j["reply"]
        res.update(reply=rep["reply_text"], topic=rep["topic"], safety=rep["safety_flag"], stt_ms=rep["latency_ms"]["stt"],
                   llm_ms=rep["latency_ms"]["llm"])
        audio = bytearray()
        first = None
        with self.http.stream("GET", f"{self.backend}{j['audio']['url']}", headers=self.h) as s:
            for chunk in s.iter_bytes():
                if first is None and chunk:
                    first = time.perf_counter()
                    self.ev("playback_started", tid)
                audio += chunk
        res["release_to_first_audio_ms"] = round((first - t_rel) * 1000) if first else None
        res["audio_s"] = round(len(audio) / 32000, 2)
        self.ev("playback_completed", tid)
        self.flush_events()
        if self.play and audio:
            import sounddevice as sd
            sd.play(np.frombuffer(bytes(audio), dtype="<i2"), 16000, blocking=True)
        return res


def main() -> int:
    ap = argparse.ArgumentParser(description=BANNER)
    ap.add_argument("--backend", default="http://localhost:8000")
    ap.add_argument("--no-play", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("say"); s1.add_argument("text")
    s2 = sub.add_parser("wav"); s2.add_argument("path")
    sub.add_parser("mic")
    s4 = sub.add_parser("bench"); s4.add_argument("text"); s4.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    print(BANNER)
    sim = Sim(a.backend, play=not a.no_play and a.cmd != "bench")
    if a.cmd == "say":
        pcm = say_pcm(a.text)
    elif a.cmd == "wav":
        pcm = wav_to_pcm16k(Path(a.path).read_bytes())
    elif a.cmd == "mic":
        while True:
            pcm = mic_pcm()
            if not pcm:
                return 0
            print(json.dumps(sim.turn(pcm), indent=2))
    else:
        pcm = say_pcm(a.text)
        rows = []
        for i in range(a.n):
            r = sim.turn(pcm, reuse_session=False)
            rows.append(r)
            print(f"{i + 1:3d} status={r['status']} first_audio={r.get('release_to_first_audio_ms')} ms "
                  f"stt={r.get('stt_ms')} llm={r.get('llm_ms')}")
        ok = [r["release_to_first_audio_ms"] for r in rows if r.get("release_to_first_audio_ms")]
        if ok:
            ok.sort()
            p95 = float(np.percentile(ok, 95, method="linear"))
            print(json.dumps({"label": "SOFTWARE_SIMULATOR (Mac, localhost network) - not device latency",
                              "n": len(ok), "failures": len(rows) - len(ok), "median_ms": statistics.median(ok),
                              "p95_ms": round(p95), "target": "median <= 3000, p95 <= 6000"}, indent=2))
        return 0
    print(json.dumps(sim.turn(pcm), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
