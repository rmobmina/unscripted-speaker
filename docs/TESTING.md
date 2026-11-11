# Testing

`scripts/run_tests.sh` runs everything that needs no hardware and no API key.

| Suite | Command | Result (11 Nov 2026) |
|---|---|---|
| Firmware core logic on host (state machine, LED engine, button, VAD, ring buffer; UBSan) | `make -C firmware/test test` | 28 groups, 142,301 checks, 0 failures |
| Backend (API, auth, roles, validation, sessions, cancel/stale, privacy, retention, resampler, safety, OpenAI request shapes via mock HTTP transport) | `cd backend && .venv/bin/python -m pytest -q` | 92 passed |
| Eval harness offline (scenario file, deterministic safety screen) | `cd backend && .venv/bin/python -m eval.run_eval --offline` | 78 scenarios, 78/78 screen matches |
| Firmware build | `scripts/flash.sh` | builds, 0x19b770 bytes, flashed and hash-verified |

Note: the deterministic screen was written alongside these scenarios, so 78/78 shows consistency, not generalisation. `emg09` and `sh04` are deliberately left to the model.

## Live tests (need OPENAI_API_KEY; cost money)
```bash
tools/device_sim.py say "I don't know where my daughter is"   # full STT→LLM→TTS round trip, plays on Mac
tools/device_sim.py bench --n 20 "Where am I?"               # simulator latency (labelled SOFTWARE_SIMULATOR)
cd backend && .venv/bin/python -m eval.run_eval --live --runs 3 --max-calls 250
```
The live eval writes `backend/eval/results/<stamp>/rubric_sheet.csv` for **human** scoring on the 8-criterion rubric (release gate: mean ≥ 14/16, no 0 on safety or grounding). The automated checks are a pre-screen only.

## Benchmarks measured on real hardware so far
| Benchmark | Target | Result | Notes |
|---|---|---|---|
| Condition change → device | ≤ 2 s | median 1510 ms, p95 1893 ms, max 1940 ms (n = 9) | Upper bound: measured when the device's *next* 1 Hz poll acknowledges the change. Passes, with little margin at the max. |
| Button → listening | ≤ 150 ms | not measured | Needs a button press. Firmware logs `button_pressed` → `state_changed …>LISTENING` |
| Release → first audio | median ≤ 3 s, p95 ≤ 6 s | not measured | Needs an API key and button presses |
| LED frame timing | ≤ 1 % late frames | reported every 60 s as `led_stats` | Check Data & metrics after 5+ minutes |
| Wi-Fi loss → offline | ≤ 5 s | not measured | Switch the router off or move the device out of range |

All metrics appear in the dashboard (Data & metrics), grouped by REAL_HARDWARE vs SOFTWARE_SIMULATOR and LIVE_AI / MOCK_PROVIDER / LOOPBACK / AI_NOT_CONFIGURED.

## Simulated LED preview
`tools/led_simulator.py` writes `docs/led_preview_SIMULATED.html`. It is rendered by the firmware's own `led_engine.c` via ctypes and labelled SIMULATED.
