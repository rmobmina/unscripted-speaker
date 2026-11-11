# Setup (macOS)

## 1. Backend
```bash
scripts/setup_backend.sh          # venv + backend/.env with new dashboard access codes
open -e backend/.env              # paste your key after OPENAI_API_KEY=
backend/.venv/bin/python -m app.cli check-config   # backend/: shows "OPENAI_API_KEY : set" (never the key)
scripts/run_backend.sh            # http://localhost:8000
```
The dashboard access codes are `RESEARCHER_TOKEN` (full access) and `CAREGIVER_TOKEN` (personal context and comfort settings only), both in `backend/.env`.

## 2. Device
```bash
scripts/configure_device.sh --wifi-from spotify_speaker/main/wifi_config.h
#   or: UNS_WIFI_SSID=... UNS_WIFI_PASSWORD=... scripts/configure_device.sh
scripts/flash.sh                  # build + flash + serial monitor (Ctrl-] to exit)
```
- The backend URL baked into the firmware is the Mac's current LAN IP. If the Mac's IP changes, re-run `configure_device.sh` and then flash again.
- Board and Mac must be on the same 2.4 GHz-capable network. The macOS firewall must allow incoming connections for Python/uvicorn.
- To restore the previous Wi-Fi speaker firmware: `spotify_speaker/flash.sh`.

## 3. Regenerate the device's offline voice clips in the study voice (recommended)
```bash
backend/.venv/bin/python tools/make_device_clips.py --engine openai && scripts/flash.sh
```
The current clips use the macOS voice "Samantha" as a development placeholder.
