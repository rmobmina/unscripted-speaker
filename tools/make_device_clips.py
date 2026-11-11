#!/usr/bin/env python3
"""Generate the short spoken system messages embedded in the firmware.

These clips play when the backend cannot (offline, errors, nothing heard), so
they must live on the device. Output: firmware/main/assets/*.pcm
(16 kHz, mono, signed 16-bit little-endian) plus clips.json recording the exact
engine, voice and text used (for the reproducibility record).

  --engine openai   same TTS model/voice as the live replies (needs OPENAI_API_KEY
                    in backend/.env); recommended before any study session so
                    every spoken message uses one consistent voice.
  --engine say      macOS `say` (offline placeholder for development only).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "firmware" / "main" / "assets"

CLIPS = {
    # Protocol Table 2: "Speech not understood"
    "clip_not_understood": "I'm sorry, I didn't catch that. Could you press the button and say it again?",
    "clip_no_speech": "I didn't hear anything. You can press the button and talk whenever you're ready.",
    # Protocol Table 3: error describes the device, never the person
    "clip_error": "Sorry, something went wrong on my end. Let's try again in a moment.",
    "clip_timeout": "Sorry, that took too long on my end. Let's try again in a moment.",
    # Protocol Table 3: offline, spoken once
    "clip_offline": "I can't connect right now.",
}


def to_16k_mono(samples: np.ndarray, rate: int) -> np.ndarray:
    from scipy.signal import resample_poly
    from math import gcd

    if rate != 16000:
        g = gcd(rate, 16000)
        samples = resample_poly(samples.astype(np.float64), 16000 // g, rate // g)
    x = samples.astype(np.float64)
    peak = np.max(np.abs(x)) or 1.0
    x = x / peak * (10 ** (-3 / 20) * 32767)   # normalise to -3 dBFS, no clipping
    return np.clip(np.round(x), -32768, 32767).astype("<i2")


def gen_say(text: str, voice: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "c.wav"
        subprocess.run(["say", "-v", voice, "-r", "165", "-o", str(wav), "--file-format=WAVE",
                        "--data-format=LEI16@16000", text], check=True)
        with wave.open(str(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            return to_16k_mono(pcm, w.getframerate())


def gen_openai(text: str) -> tuple[np.ndarray, dict]:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import get_settings  # noqa: E402
    from openai import OpenAI  # noqa: E402

    s = get_settings()
    if not s.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set in backend/.env")
    client = OpenAI(api_key=s.openai_api_key.get_secret_value())
    resp = client.audio.speech.create(model=s.tts_model, voice=s.tts_voice, input=text,
                                      instructions=s.tts_instructions, response_format="pcm",
                                      speed=s.tts_speed)
    pcm = np.frombuffer(resp.content, dtype="<i2")    # 24 kHz mono s16le per API docs
    return to_16k_mono(pcm, 24000), {"model": s.tts_model, "voice": s.tts_voice, "speed": s.tts_speed}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["say", "openai"], default="say")
    ap.add_argument("--voice", default="Samantha", help="macOS voice for --engine say")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"engine": args.engine, "format": "pcm_s16le 16000 Hz mono", "clips": {}}
    for name, text in CLIPS.items():
        if args.engine == "say":
            pcm, meta = gen_say(text, args.voice), {"voice": args.voice, "rate_wpm": 165}
        else:
            pcm, meta = gen_openai(text)
        (OUT / f"{name}.pcm").write_bytes(pcm.tobytes())
        manifest["clips"][name] = {"text": text, "seconds": round(len(pcm) / 16000, 2), **meta}
        print(f"{name}: {len(pcm) / 16000:.2f} s")
    (OUT / "clips.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.engine == "say":
        print("NOTE: development placeholder voice (macOS say). Regenerate with --engine openai "
              "before study sessions so system messages match the reply voice.")


if __name__ == "__main__":
    main()
