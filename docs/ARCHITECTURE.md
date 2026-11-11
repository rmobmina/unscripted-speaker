# Architecture

```
 ESP32-S3 device                          Mac backend (FastAPI)                         OpenAI
 ┌───────────────────────┐   HTTP (LAN)   ┌──────────────────────────────────┐   HTTPS  ┌──────────────┐
 │ button → state machine│──POST turns───▶│ turns.py  STT → agent → TTS stream│────────▶│ transcriptions│
 │ mic → VAD → PCM 16 kHz│◀─GET audio─────│ agent.py  prompt + safety.py      │────────▶│ responses     │
 │ LED engine 50 Hz      │◀─GET config 1Hz│ device_config / sessions / events │────────▶│ audio/speech  │
 │ earcons + local clips │──POST events──▶│ SQLite (data/) · dashboard (static)│          └──────────────┘
 └───────────────────────┘                └──────────────────────────────────┘
```

The OpenAI key exists only in `backend/.env`. The device holds only its own revocable token, and the backend stores only a SHA-256 hash of that token.

## Button semantics (one button)
| Gesture | Effect |
|---|---|
| Press and hold, speak, release | Records while held and submits on release ("hold to talk") |
| Short tap (< 0.7 s, no speech) | Hands-free: listens until 1.5 s of silence or until the next press |
| Press during THINKING or SPEAKING | Interrupts. The old turn is cancelled (DELETE), and its late audio is rejected as stale |
| Long press (2 s) with no speech | Cancels and returns to idle |
| Speech within 6 s after a reply | Treated as a follow-up in the same session |
| 10 min with no activity | Conversation memory cleared |

## Turn protocol
1. `POST /api/v1/device/turns`: raw PCM s16le, 16 kHz, mono. Headers: `X-Turn-Id`, `X-Session-Id`, `X-Condition`, `X-Capture-Quality`, `X-Audio-Format`.
2. The backend runs STT, then the deterministic input screen, then the LLM (strict JSON schema), then reply validation. It returns JSON: reply metadata, topic and confidence, safety flag, `session_id`.
3. TTS starts immediately and streams through a 24→16 kHz streaming resampler into a per-turn buffer. The device reads `GET .../audio` as a chunked stream, so playback starts before synthesis finishes.
4. Any newer turn supersedes the older one, and the backend rejects stale commits (409 `superseded`).

## Safety layering
1. `safety.classify_input()`: regex screen of the transcript for emergency, self-harm and leaving. It is independent of the model.
2. The model's structured `safety_flag` can **raise** the flag, never lower it.
3. For an emergency, the fixed, pre-approved text replaces the model's text.
4. `validate_reply()` replaces replies that contain banned patterns: "already asked", pet names, claims to be human or family, claims that someone was contacted or is on the way, diagnoses, memory quizzes, or names that are neither in the approved context nor spoken by the person. It also trims replies to 3 sentences and 1 question.
5. Safety flags are **logged only**. No caregiver notification system exists, and the companion never says one does.

## Privacy defaults
- No audio or transcript is retained unless a researcher enables retention **and** confirms recorded opt-in consent.
- Research events hold timings and categories only. Personal context changes are logged by table name, not content.
- `store=false` on every Responses API call.
