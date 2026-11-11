# [un]scripted 2.0

A research prototype: a one-button ESP32-S3 voice companion that gives short, emotionally supportive replies, plus a caregiver/researcher dashboard.

> **An experimental supportive companion. It is not a medical device, an emergency response system, or a replacement for human caregivers.** Do not begin testing with human participants without institutional ethics approval.

| Folder | What it is |
|---|---|
| `firmware/` | ESP-IDF 5.5 project `unscripted_device` (Waveshare ESP32-S3-AUDIO-Board) |
| `backend/` | FastAPI backend: OpenAI STT → LLM → TTS, safety layer, sessions, research events, dashboard (`app/static`) |
| `prompts/` | Versioned system prompt |
| `backend/eval/` | 78 conversation scenarios and the evaluation runner |
| `tools/` | Device simulator (Mac mic/speaker), LED simulator, offline clip generator |
| `scripts/` | setup, run, configure, flash, tests |
| `docs/` | SETUP, ARCHITECTURE, HARDWARE_AUDIT, WIRING, TESTING, RESEARCH_REPRODUCIBILITY, KNOWN_LIMITATIONS |

Quick start: see `docs/SETUP.md`. The earlier projects in this folder (root `main/` hello_world, `spotify_speaker/`) are unchanged.
