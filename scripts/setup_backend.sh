#!/usr/bin/env bash
# Create the backend virtualenv and a backend/.env with fresh dashboard access codes.
# Does NOT create or ask for the OpenAI key: add OPENAI_API_KEY to backend/.env yourself.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY="${PYTHON:-python3.12}"
command -v "$PY" >/dev/null || PY=python3
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
if [ ! -f .env ]; then
  umask 077
  R=$(openssl rand -base64 24 | tr -d '/+=' ); C=$(openssl rand -base64 24 | tr -d '/+=')
  sed -e "s|^RESEARCHER_TOKEN=.*|RESEARCHER_TOKEN=$R|" -e "s|^CAREGIVER_TOKEN=.*|CAREGIVER_TOKEN=$C|" .env.example > .env
  echo "Created backend/.env with new dashboard access codes (see the file; they are not printed here)."
  echo "Next: edit backend/.env and set OPENAI_API_KEY."
fi
.venv/bin/python -m app.cli check-config || true
