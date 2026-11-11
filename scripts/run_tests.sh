#!/usr/bin/env bash
# All offline tests: firmware core logic (host C) + backend (pytest) + offline eval harness check.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "== firmware core (host, UBSan) =="; make -s -C "$ROOT/firmware/test" test
echo "== backend =="; ( cd "$ROOT/backend" && .venv/bin/python -m pytest -q )
echo "== eval harness (offline, no API calls) =="; ( cd "$ROOT/backend" && .venv/bin/python -m eval.run_eval --offline )
