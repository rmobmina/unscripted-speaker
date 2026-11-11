"""Runtime device configuration (versioned) and research configuration.

The device polls GET /api/v1/device/config every second; any change bumps
`version`. The device acknowledges the version it applied in the next poll,
which lets the backend measure condition-change latency end to end.
"""
from __future__ import annotations

import time
from typing import Any

from .agent import DEFAULT_EMERGENCY_TEXT, DEFAULT_SELF_HARM_TEXT
from .db import Database

DEVICE_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "condition": "B2",
    "reduced_motion": False,
    "lights_off": False,
    "max_brightness_pct": 40,
    "volume": 70,
    "led_offset": 0,
    "led_reverse": False,
    "led_grb": False,
    "afterglow_ms": 10000,
    "silence_endpoint_ms": 1500,
    "endpoint_while_held": False,
    "vad_abs_dbfs": -48.0,
    "vad_snr_db": 9.0,
    "session_reset_seq": 0,
    "probe": None,
    "diag": None,
}

# Validation: (type, min, max) or allowed values.
DEVICE_FIELDS: dict[str, tuple] = {
    "condition": ("enum", ("B1", "B2", "B3")),
    "reduced_motion": ("bool",),
    "lights_off": ("bool",),
    "max_brightness_pct": ("int", 0, 60),
    "volume": ("int", 0, 100),
    "led_offset": ("int", 0, 63),
    "led_reverse": ("bool",),
    "led_grb": ("bool",),
    "afterglow_ms": ("int", 0, 60000),
    "silence_endpoint_ms": ("int", 500, 5000),
    "endpoint_while_held": ("bool",),
    "vad_abs_dbfs": ("float", -90.0, -10.0),
    "vad_snr_db": ("float", 0.0, 40.0),
}
# Caregivers may change comfort/accessibility settings only; never the research condition.
CAREGIVER_FIELDS = {"reduced_motion", "lights_off", "max_brightness_pct", "volume"}

RESEARCH_DEFAULTS: dict[str, Any] = {
    "participant_code": "",
    "session_number": None,
    "retain_transcripts": False,
    "retain_audio": False,
    "loopback_test": False,
    "emergency_text": DEFAULT_EMERGENCY_TEXT,
    "self_harm_text": DEFAULT_SELF_HARM_TEXT,
}


class ConfigError(ValueError):
    pass


def _coerce(field: str, value: Any) -> Any:
    spec = DEVICE_FIELDS.get(field)
    if spec is None:
        raise ConfigError(f"unknown setting: {field}")
    kind = spec[0]
    if kind == "enum":
        if value not in spec[1]:
            raise ConfigError(f"{field} must be one of {spec[1]}")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{field} must be true/false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    if not spec[1] <= value <= spec[2]:
        raise ConfigError(f"{field} must be within {spec[1]}..{spec[2]}")
    return int(value) if kind == "int" else float(value)


class ConfigStore:
    def __init__(self, db: Database):
        self.db = db
        cfg = db.kv_get("device_config")
        if cfg is None:
            db.kv_set("device_config", dict(DEVICE_DEFAULTS))
        else:
            merged = {**DEVICE_DEFAULTS, **cfg}
            db.kv_set("device_config", merged)
        rc = db.kv_get("research_config")
        db.kv_set("research_config", {**RESEARCH_DEFAULTS, **(rc or {})})

    def device(self) -> dict[str, Any]:
        cfg = self.db.kv_get("device_config")
        return {**cfg, "server_time_ms": int(time.time() * 1000)}

    def research(self) -> dict[str, Any]:
        return self.db.kv_get("research_config")

    def update_device(self, changes: dict[str, Any], allowed: set[str] | None = None) -> tuple[dict, dict]:
        cfg = self.db.kv_get("device_config")
        applied: dict[str, Any] = {}
        for k, v in changes.items():
            if allowed is not None and k not in allowed:
                raise ConfigError(f"not permitted to change {k}")
            nv = _coerce(k, v)
            if cfg.get(k) != nv:
                applied[k] = {"from": cfg.get(k), "to": nv}
                cfg[k] = nv
        if applied:
            cfg["version"] = int(cfg["version"]) + 1
            if "condition" in applied:
                self.db.kv_set("pending_condition", {"version": cfg["version"], "condition": cfg["condition"],
                                                     "changed_at": time.time()})
            self.db.kv_set("device_config", cfg)
        return cfg, applied

    def _bump(self, key: str, value: Any) -> dict:
        cfg = self.db.kv_get("device_config")
        cfg[key] = value
        cfg["version"] = int(cfg["version"]) + 1
        self.db.kv_set("device_config", cfg)
        return cfg

    def probe(self, status: str, duration_ms: int) -> dict:
        cfg = self.db.kv_get("device_config")
        seq = int((cfg.get("probe") or {}).get("seq", 0)) + 1
        return self._bump("probe", {"seq": seq, "status": status, "duration_ms": duration_ms})

    def led_diag(self, pattern: str) -> dict:
        cfg = self.db.kv_get("device_config")
        seq = int((cfg.get("diag") or {}).get("seq", 0)) + 1
        return self._bump("diag", {"seq": seq, "pattern": pattern})

    def reset_session(self) -> dict:
        cfg = self.db.kv_get("device_config")
        return self._bump("session_reset_seq", int(cfg.get("session_reset_seq", 0)) + 1)

    def update_research(self, changes: dict[str, Any], consent_recorded: bool) -> tuple[dict, dict]:
        rc = self.db.kv_get("research_config")
        applied = {}
        for k, v in changes.items():
            if k not in RESEARCH_DEFAULTS:
                raise ConfigError(f"unknown research setting: {k}")
            if k in ("retain_transcripts", "retain_audio"):
                if not isinstance(v, bool):
                    raise ConfigError(f"{k} must be true/false")
                if v and not consent_recorded:
                    raise ConfigError("Enabling retention requires confirming that opt-in consent is recorded")
            if k == "loopback_test" and not isinstance(v, bool):
                raise ConfigError("loopback_test must be true/false")
            if k == "participant_code" and v and not (isinstance(v, str) and len(v) <= 12 and v.replace("-", "").isalnum()):
                raise ConfigError("participant_code must be a short anonymous code such as P01")
            if k in ("emergency_text", "self_harm_text") and not (isinstance(v, str) and 10 <= len(v) <= 200):
                raise ConfigError(f"{k} must be 10-200 characters")
            if rc.get(k) != v:
                applied[k] = {"from": rc.get(k) if k not in ("emergency_text", "self_harm_text") else "...", "to": v}
                rc[k] = v
        self.db.kv_set("research_config", rc)
        return rc, applied
