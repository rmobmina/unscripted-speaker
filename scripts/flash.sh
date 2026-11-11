#!/usr/bin/env bash
# Build and flash the [un]scripted firmware, then open the serial monitor.
# This REPLACES whatever firmware is on the board. To go back to the Wi-Fi speaker: spotify_speaker/flash.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
[ -n "$PORT" ] || { echo "No /dev/cu.usbmodem* port found. Is the board plugged in?"; exit 1; }
[ -f "$ROOT/firmware/main/secrets.h" ] || { echo "Run scripts/configure_device.sh first"; exit 1; }
. "$ROOT/scripts/idf_env.sh"
cd "$ROOT/firmware"
if [ -n "${NO_MONITOR:-}" ]; then idf.py -p "$PORT" build flash; else idf.py -p "$PORT" build flash monitor; fi
