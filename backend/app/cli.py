"""Command-line helpers.

  python -m app.cli provision-device <device_id> [--label L] [--quiet-token-file PATH]
  python -m app.cli check-config
  python -m app.cli list-devices
"""
from __future__ import annotations

import argparse
import os
import sys

from .config import get_settings
from .db import Database
from .security import hash_token, new_token


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("provision-device", help="Create/rotate a device token (printed once)")
    pv.add_argument("device_id")
    pv.add_argument("--label", default="")
    pv.add_argument("--token-file", help="Write the token to this file (mode 600) instead of printing it")
    sub.add_parser("check-config", help="Validate backend/.env without printing secrets")
    sub.add_parser("list-devices")
    a = p.parse_args(argv)
    s = get_settings()

    if a.cmd == "check-config":
        key = s.openai_api_key.get_secret_value() if s.openai_api_key else ""
        print(f"provider            : {s.ai_provider}")
        print(f"OPENAI_API_KEY      : {'set (' + str(len(key)) + ' chars)' if key else 'NOT SET'}")
        print(f"models              : stt={s.stt_model} llm={s.llm_model} tts={s.tts_model} voice={s.tts_voice}")
        print(f"RESEARCHER_TOKEN    : {'set' if s.researcher_token else 'NOT SET'}")
        print(f"CAREGIVER_TOKEN     : {'set' if s.caregiver_token else 'NOT SET'}")
        print(f"prompt              : {s.prompt_path} ({'ok' if s.prompt_path.exists() else 'MISSING'})")
        print(f"data_dir            : {s.data_dir}")
        print(f"ai_ready            : {s.ai_ready}")
        return 0 if s.prompt_path.exists() else 1

    db = Database(s.data_dir / "unscripted.db")
    if a.cmd == "list-devices":
        for d in db.query("SELECT device_id, label, created_at, last_seen_at, firmware FROM devices"):
            print(d)
        return 0

    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,30}", a.device_id):
        print("device_id must be lowercase letters, digits and dashes", file=sys.stderr)
        return 2
    token = new_token()
    db.execute("INSERT INTO devices(device_id, token_hash, label, created_at) VALUES(?,?,?,datetime('now')) "
               "ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash, label=excluded.label",
               (a.device_id, hash_token(token), a.label))
    if a.token_file:
        fd = os.open(a.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        print(f"Provisioned {a.device_id}; token written to {a.token_file}")
    else:
        print(f"Provisioned {a.device_id}. Token (shown once, only its hash is stored):\n{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
