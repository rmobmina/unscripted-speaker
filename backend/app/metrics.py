"""Benchmark metrics computed from the research event log.

Every metric is grouped by (run_mode, ai_mode) so REAL_HARDWARE / SOFTWARE_SIMULATOR
and LIVE_AI / MOCK_PROVIDER / LOOPBACK results are never pooled.
Percentiles: numpy linear interpolation (method="linear").
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from .db import Database

TARGETS = {
    "button_to_listening_ms": ("<=", 150),
    "release_to_first_audio_ms": ("median<=", 3000),
    "release_to_first_audio_p95_ms": ("<=", 6000),
    "led_dropped_frame_pct": ("<=", 1.0),
    "wifi_loss_to_offline_ms": ("<=", 5000),
    "condition_update_ms": ("<=", 2000),
}


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "median": None, "p95": None, "min": None, "max": None}
    a = np.asarray(values, dtype=float)
    return {"n": int(a.size), "median": round(float(np.median(a)), 1),
            "p95": round(float(np.percentile(a, 95, method="linear")), 1),
            "min": round(float(a.min()), 1), "max": round(float(a.max()), 1)}


def _group(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    g: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        g[(r["run_mode"], r["ai_mode"])].append(r)
    return g


def compute(db: Database, limit: int = 50000) -> dict:
    rows = db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows.reverse()
    out: dict = {"groups": [], "targets": TARGETS, "percentile_method": "numpy linear"}
    for (run_mode, ai_mode), ev in _group(rows).items():
        if run_mode == "SYSTEM":
            continue   # dashboard/backend housekeeping; not a measurement group
        m: dict = {"run_mode": run_mode, "ai_mode": ai_mode}
        by_name: dict[str, list[dict]] = defaultdict(list)
        for e in ev:
            by_name[e["name"]].append(e)
        for stage, name in (("stt_ms", "stt_completed"), ("llm_ms", "llm_completed"),
                            ("tts_first_audio_ms", "tts_first_audio"), ("tts_total_ms", "tts_completed")):
            m[stage] = summarize([e["duration_ms"] for e in by_name[name] if e["duration_ms"] is not None])

        # Client-side latencies (client monotonic clock, same boot only). Groups keep hardware and simulator apart.
        dev = [e for e in ev if e["source"] in ("device", "simulator") and e["t_mono_ms"] is not None]
        ended = {}
        r2a = []
        b2l = []
        loss = []
        pending_press: dict[str, int] = {}
        pending_loss: dict[str, int] = {}
        for e in dev:
            boot = e["boot_id"] or ""
            if e["name"] == "listening_ended" and e["detail"] in ("release", "silence_endpoint", "max_length"):
                ended[(boot, e["turn_id"])] = e["t_mono_ms"]
            elif e["name"] == "playback_started" and (boot, e["turn_id"]) in ended:
                r2a.append(e["t_mono_ms"] - ended.pop((boot, e["turn_id"])))
            elif e["name"] == "button_pressed":
                pending_press[boot] = e["t_mono_ms"]
            elif e["name"] == "state_changed" and (e["detail"] or "").endswith(">LISTENING") and boot in pending_press:
                b2l.append(e["t_mono_ms"] - pending_press.pop(boot))
            elif e["name"] in ("wifi_disconnected",):
                pending_loss.setdefault(boot, e["t_mono_ms"])
            elif e["name"] == "state_changed" and (e["detail"] or "").endswith(">OFFLINE") and boot in pending_loss:
                loss.append(e["t_mono_ms"] - pending_loss.pop(boot))
        m["release_to_first_audio_ms"] = summarize(r2a)
        m["button_to_listening_state_ms"] = summarize(b2l)
        m["wifi_loss_to_offline_ms"] = summarize(loss)
        m["condition_update_ms"] = summarize([e["duration_ms"] for e in by_name["condition_applied_ack"]
                                              if e["duration_ms"] is not None])
        # Device LED counters are cumulative since boot: take the latest report per boot, then sum boots.
        per_boot: dict[str, tuple[int, int]] = {}
        for e in by_name["led_stats"]:
            d = e["detail"] or ""
            if d.startswith("frames="):
                per_boot[e["boot_id"] or ""] = (int(d.split("=")[1]), int(e["value"] or 0))
        frames = sum(f for f, _ in per_boot.values())
        late = sum(l for _, l in per_boot.values())
        m["led_frames"] = frames
        m["led_late_frames"] = late
        m["led_dropped_frame_pct"] = round(100 * late / frames, 3) if frames else None

        turns = {e["turn_id"] for e in ev if e["name"] == "audio_upload_completed" and e["turn_id"]}
        errors = [e for e in ev if e["name"] in ("error", "processing_timeout")]
        codes: dict[str, int] = defaultdict(int)
        for e in errors:
            codes[e["code"] or e["detail"] or e["name"]] += 1
        m["turns"] = len(turns)
        m["error_counts"] = dict(codes)
        failed_turns = {e["turn_id"] for e in errors if e["turn_id"]}
        m["turn_failure_rate"] = round(len(failed_turns & turns) / len(turns), 3) if turns else None
        m["safety_flags"] = len(by_name["safety_flag"])
        m["replies_replaced"] = sum(1 for e in by_name["llm_completed"]
                                    if e["data"] and json.loads(e["data"]).get("replaced"))
        out["groups"].append(m)
    return out
