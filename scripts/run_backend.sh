#!/usr/bin/env bash
# Run the backend on all interfaces so the ESP32 can reach it over the LAN.
# HTTP on a trusted lab network only (see docs/KNOWN_LIMITATIONS.md).
set -euo pipefail
cd "$(dirname "$0")/../backend"
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<your-mac-ip>")
echo "Dashboard: http://localhost:8000   (device should use http://$IP:8000)"
exec .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log
