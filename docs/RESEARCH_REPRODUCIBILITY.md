# Research reproducibility record

| Component | Value | Where set |
|---|---|---|
| Firmware | 2.0.0-dev.1 | `firmware/main/app_config.h` |
| Backend | 2.0.0-dev.1 | `backend/app/__init__.py` |
| System prompt | v1.0 (sha256 prefix in every `/healthz` and status call) | `prompts/unscripted_system_v1.0.md` |
| STT | `gpt-transcribe`, language `en`, JSON response | `.env` STT_MODEL |
| LLM | `gpt-6-luna` via Responses API, strict JSON schema, reasoning effort `none`, temperature 0.3, max_output_tokens 160, `store=false` | `.env` LLM_* |
| TTS | `gpt-4o-mini-tts-2025-12-15`, voice `marin`, speed 0.9, calm-delivery instructions, `pcm` 24 kHz → resampled to 16 kHz | `.env` TTS_* |

Every research event records the firmware version, backend version, prompt version, all three model ids, run_mode and ai_mode.

## Deviations from the protocol and reasons
- **max_output_tokens 160 instead of 120.** The structured JSON wrapper (topic, flags, confidence) uses about 40 tokens. Reply length is still enforced at ≤ 3 sentences by the validator.
- **Temperature fallback.** If the model rejects `temperature`, the request is retried without it once. The rejection is logged, and `temperature_applied=false` is recorded for each turn. Report this if it occurs.
- **TTS speed fallback.** The same approach applies to `speed`.
- **7 LEDs instead of 16.** The anchors are mapped to the nearest pixels to 0/90/180/270°.

## Known model lifecycle risk
OpenAI's deprecations page lists **all `/v1/audio/speech` models (including `gpt-4o-mini-tts-2025-12-15`) for shutdown on 2027-01-06**. The suggested replacement uses the Realtime API, which this backend does not implement. Studies running after that date need a TTS migration and a new prompt/voice record. `gpt-4o-mini-transcribe` is listed for 2027-02-26, which is why `gpt-transcribe` was chosen.

## Changing anything
Never edit a released prompt in place. Copy it to a new version, update `PROMPT_PATH`, and add a row here.
