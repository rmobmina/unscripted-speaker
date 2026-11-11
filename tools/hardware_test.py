#!/usr/bin/env python3
"""Guided physical test of the [un]scripted device (REAL_HARDWARE).

Walks a person through every hardware function, confirms what it can from the
device's own events (button, states, playback), and asks the tester about what
only a human can judge (what the lights look like, what the speaker sounds like).

  backend/.venv/bin/python tools/hardware_test.py            # full test (~10 min)
  backend/.venv/bin/python tools/hardware_test.py --only leds,button

Needs: backend running (scripts/run_backend.sh), device connected.
The live-AI steps make a few real OpenAI calls (a few cents).
Results are saved to backend/data/hw_tests/<time>.json (no audio, no words).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:8000"
STEPS = ["connect", "leds", "probes", "button", "loopback", "ai", "interrupt", "cancel", "conditions", "reduced"]

B, G, Y, R, D, X = "\x1b[1m", "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[2m", "\x1b[0m"


def token() -> str:
    for line in (ROOT / "backend" / ".env").read_text().splitlines():
        if line.startswith("RESEARCHER_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("RESEARCHER_TOKEN not found in backend/.env")


class Tester:
    def __init__(self):
        self.h = {"Authorization": f"Bearer {token()}"}
        self.http = httpx.Client(base_url=BASE, headers=self.h, timeout=10)
        self.results: list[dict] = []
        self.last_id = self._max_event_id()

    # ---------------------------------------------------------------- helpers --
    def _max_event_id(self) -> int:
        ev = self.http.get("/api/v1/admin/events", params={"limit": 1}).json()
        return ev[0]["id"] if ev else 0

    def mark(self) -> None:
        """Ignore all events before this point."""
        self.last_id = self._max_event_id()

    def events(self) -> list[dict]:
        ev = self.http.get("/api/v1/admin/events", params={"limit": 500, "since_id": self.last_id}).json()
        return [e for e in reversed(ev) if e["run_mode"] in ("REAL_HARDWARE", "SYSTEM")]

    def wait_for(self, pred, timeout: float, what: str):
        print(f"{D}  waiting for {what} (up to {int(timeout)} s)...{X}", flush=True)
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events():
                if pred(e):
                    return e
            time.sleep(0.4)
        return None

    def put(self, changes: dict) -> None:
        self.http.put("/api/v1/admin/device-config", json={"changes": changes}).raise_for_status()

    def research(self, changes: dict) -> None:
        self.http.put("/api/v1/admin/research-config", json={"changes": changes}).raise_for_status()

    def record(self, step: str, check: str, ok: bool | None, detail: str = "") -> None:
        tag = f"{G}PASS{X}" if ok else (f"{Y}SKIP{X}" if ok is None else f"{R}FAIL{X}")
        print(f"  [{tag}] {check}{(' - ' + detail) if detail else ''}")
        self.results.append({"step": step, "check": check, "ok": ok, "detail": detail})

    @staticmethod
    def ask(q: str) -> bool | None:
        while True:
            a = input(f"  {B}? {q} [y/n/s=skip] {X}").strip().lower()
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            if a in ("s", "skip"):
                return None

    @staticmethod
    def say(title: str, *lines: str) -> None:
        print(f"\n{B}=== {title} ==={X}")
        for l in lines:
            print("  " + l)

    @staticmethod
    def go(prompt: str = "Press Enter when ready") -> None:
        input(f"  {B}> {prompt}{X} ")

    def device(self) -> dict | None:
        s = self.http.get("/api/v1/admin/status").json()
        devs = [d for d in s["devices"] if d.get("client_kind") == "device"]
        return devs[0] if devs else None

    def turn_events(self, turn_id: str) -> list[dict]:
        return [e for e in self.events() if e["turn_id"] == turn_id]

    # ------------------------------------------------------------------ steps --
    def step_connect(self):
        self.say("1. Connection")
        s = self.http.get("/api/v1/admin/status").json()
        d = self.device()
        self.record("connect", "device connected to backend", bool(d and d["connected"]),
                    f"{d['device_id']} state={d['state']} rssi={d['rssi']}" if d else "no device seen")
        self.record("connect", "AI configured", s["ai"]["ready"], s["ai"]["mode"])
        if not (d and d["connected"]):
            sys.exit("Device not connected - check it is powered and on Wi-Fi, then re-run.")
        self.put({"condition": "B2", "reduced_motion": False, "lights_off": False, "max_brightness_pct": 40})
        self.research({"loopback_test": False})
        time.sleep(2)

    def step_leds(self):
        self.say("2. LED ring layout (each pattern shows for 8 s)",
                 "Hold the board so you know which side you call 'top' (12 o'clock).")
        self.go()
        self.http.post("/api/v1/admin/led-diag", json={"pattern": "all_white_low"})
        self.record("leds", "all 7 LEDs light dim white", self.ask("Do ALL the LEDs light up dim white?"))
        time.sleep(1)
        self.http.post("/api/v1/admin/led-diag", json={"pattern": "logical_zero"})
        self.record("leds", "exactly one LED lit (logical 0)", self.ask("Is exactly ONE LED lit white?"))
        top = self.ask("Is that LED at the TOP (12 o'clock) of the ring?")
        self.record("leds", "logical 0 is at the top", top,
                    "" if top else "fix later: Research tab > LED calibration > Top LED offset")
        time.sleep(1)
        self.http.post("/api/v1/admin/led-diag", json={"pattern": "rgb"})
        print("  Expected: the top LED RED, the next one GREEN, the next one BLUE.")
        rgb = self.ask("Are the colours red, green, blue in that order?")
        detail = ""
        if rgb is False:
            swapped = self.ask("Are red and green swapped (green first, then red, then blue)?")
            if swapped:
                self.put({"led_grb": True})
                detail = "set led_grb=true"
        self.record("leds", "colour order RGB", rgb, detail)
        cw = self.ask("Going red -> green -> blue, is that CLOCKWISE?")
        if cw is False:
            self.put({"led_reverse": True})
        self.record("leds", "ring direction clockwise", cw, "" if cw is not False else "set led_reverse=true")
        time.sleep(8)

    def step_probes(self):
        self.say("3. Status lights (condition B2, each shown for 6 s)",
                 "For each one, compare what you see with the description.")
        expect = {
            "IDLE": "very dim WARM WHITE, steady (10%)",
            "LISTENING": "BLUE, slow breathing (4 s cycle), 4 brighter anchor LEDs",
            "PROCESSING": "AMBER/ORANGE light circling the ring (one lap per 2 s)",
            "SPEAKING": "GREEN, gentle pulse (1.2 s)",
            "ERROR": "CORAL/RED, two slow pulses, then stays dim",
            "OFFLINE": "a single very dim GREY LED",
        }
        self.go()
        for st, desc in expect.items():
            print(f"\n  Showing {B}{st}{X}: expected {desc}")
            self.http.post("/api/v1/admin/probe", json={"status": st, "duration_ms": 6000})
            ok = self.ask("Does it look like that?")
            self.record("probes", f"{st} light", ok)
            time.sleep(0.5)

    def step_button(self):
        self.say("4. Button", "PRESS and HOLD the BOOT button for about 2 seconds, without speaking, then let go.",
                 "Expected: ring turns BLUE (breathing) while held, a short listening chime, then back to dim white.")
        self.mark()
        self.go("Press Enter, then hold BOOT for ~2 s and release")
        e = self.wait_for(lambda e: e["name"] == "button_pressed", 20, "a button press")
        self.record("button", "button press detected", e is not None)
        if not e:
            print(f"  {Y}No press seen. Watch the serial log (scripts/flash.sh monitor) and press each button;"
                  f" tell Claude which one logs 'button'.{X}")
            return
        boot = e["boot_id"]
        l = self.wait_for(lambda x: x["name"] == "state_changed" and (x["detail"] or "").endswith(">LISTENING")
                          and x["boot_id"] == boot, 5, "LISTENING state")
        if l:
            ms = l["t_mono_ms"] - e["t_mono_ms"]
            self.record("button", "press -> LISTENING state latency", ms <= 150, f"{ms} ms (target <= 150 ms)")
        self.wait_for(lambda x: x["name"] == "button_released", 10, "release")
        self.record("button", "ring turned blue while held", self.ask("Did the ring turn BLUE while you held it?"))
        self.record("button", "listening chime heard", self.ask("Did you hear a short chime when you pressed?"))

    def step_loopback(self):
        self.say("5. Microphone + speaker (loopback, no AI)",
                 "HOLD BOOT, say clearly from about 40 cm: 'Testing one, two, three. The garden is lovely today.'",
                 "Then RELEASE. Expected: you hear your own recording played back from the speaker.")
        self.research({"loopback_test": True})
        try:
            time.sleep(1.5)
            self.mark()
            self.go("Press Enter, then hold BOOT, speak, release")
            up = self.wait_for(lambda e: e["name"] == "audio_upload_completed" and e["source"] == "backend", 40, "the recording to upload")
            self.record("loopback", "recording uploaded", up is not None)
            if up:
                d = json.loads(up["data"] or "{}")
                level = d.get("rms_dbfs")
                ok = level is not None and level > -50 and d.get("clipped_fraction", 0) < 0.01
                self.record("loopback", "speech level OK", ok,
                            f"{(up['duration_ms'] or 0) / 1000:.1f} s, rms {level} dBFS, peak {d.get('peak')}, "
                            f"clipped {d.get('clipped_fraction')}")
                pb = self.wait_for(lambda e: e["name"] == "playback_started" and e["turn_id"] == up["turn_id"], 15,
                                   "playback")
                self.record("loopback", "playback started", pb is not None)
                time.sleep(3)
            self.record("loopback", "heard own voice clearly", self.ask("Did you hear your own voice played back clearly?"))
            self.record("loopback", "volume comfortable", self.ask("Was the volume comfortable at 40 cm?"))
        finally:
            self.research({"loopback_test": False})

    def _ai_turn(self, step: str, phrase: str, expect_topic: str | None = None) -> dict | None:
        self.mark()
        self.go(f"Press Enter, then HOLD BOOT, say \"{phrase}\", release")
        up = self.wait_for(lambda e: e["name"] == "audio_upload_completed" and e["source"] == "backend", 40, "your recording")
        if not up:
            self.record(step, "recording uploaded", False)
            return None
        tid = up["turn_id"]
        llm = self.wait_for(lambda e: e["name"] in ("llm_completed", "error", "stt_not_understood")
                            and e["turn_id"] == tid, 20, "the AI reply")
        if not llm or llm["name"] != "llm_completed":
            self.record(step, "AI reply", False, (llm or {}).get("code") or (llm or {}).get("name") or "timeout")
            return None
        d = json.loads(llm["data"] or "{}")
        pb = self.wait_for(lambda e: e["name"] == "playback_started" and e["turn_id"] == tid, 20, "playback")
        ended = next((e for e in self.events() if e["name"] == "listening_ended" and e["turn_id"] == tid), None)
        lat = pb["t_mono_ms"] - ended["t_mono_ms"] if pb and ended else None
        self.record(step, "reply played", pb is not None,
                    f"topic={d.get('topic')} ({d.get('topic_confidence')}), safety={d.get('safety_flag')}"
                    + (f", release->first audio {lat} ms" if lat is not None else ""))
        if expect_topic:
            self.record(step, f"topic is {expect_topic}", d.get("topic") == expect_topic, d.get("topic") or "")
        return {"turn_id": tid, "latency_ms": lat, **d}

    def step_ai(self):
        self.say("6. Live AI conversation",
                 "Watch the lights: BLUE while holding -> AMBER circling while thinking -> GREEN while it speaks"
                 " -> dim white.")
        r = self._ai_turn("ai", "I don't know where my daughter is")
        if r:
            self.record("ai", "lights went blue -> amber -> green -> white",
                        self.ask("Did the lights go BLUE -> AMBER circling -> GREEN -> dim white?"))
            self.record("ai", "reply sensible and kind", self.ask("Was the spoken reply sensible, calm and kind?"))
            print("\n  Follow-up: within 6 s of the reply ending you can just hold BOOT and keep talking.")
            r2 = self._ai_turn("ai", "Her name is Anna")
            if r2:
                self.record("ai", "follow-up remembered context", self.ask("Did the reply fit what you said before?"))
        lats = [x["latency_ms"] for x in [r] if x and x.get("latency_ms")]
        if lats:
            self.record("ai", "release -> first audio <= 3 s", lats[0] <= 3000, f"{lats[0]} ms (single sample)")

    def step_interrupt(self):
        self.say("7. Interrupting",
                 "Ask a question; WHILE it is speaking (green), press BOOT again.",
                 "Expected: speech stops within a moment and the ring goes BLUE (listening). Then just release.")
        self.mark()
        self.go("Press Enter, then HOLD BOOT, say \"Tell me something nice about gardens\", release, "
                "and press BOOT again while it talks")
        e = self.wait_for(lambda e: e["name"] == "playback_interrupted", 60, "an interruption")
        self.record("interrupt", "playback interrupted by button", e is not None)
        self.record("interrupt", "speech stopped quickly", self.ask("Did the speech stop right away?"))

    def step_cancel(self):
        self.say("8. Cancel (long press)",
                 "Hold BOOT for 3 seconds WITHOUT speaking, then release.",
                 "Expected: nothing is sent; ring returns to dim white.")
        self.mark()
        self.go("Press Enter, then hold BOOT 3 s silently and release")
        e = self.wait_for(lambda e: e["name"] in ("interaction_cancelled", "audio_upload_completed"), 20,
                          "cancel or upload")
        self.record("cancel", "long press cancelled (nothing uploaded)",
                    bool(e) and e["name"] == "interaction_cancelled", (e or {}).get("detail") or "")

    def step_conditions(self):
        self.say("9. Research conditions")
        self.put({"condition": "B1"})
        time.sleep(2)
        print("  Condition B1: every active state should be a STEADY colour at 70%, no motion.")
        self._ai_turn("conditions", "What time is lunch?")
        self.record("conditions", "B1 lights steady (no breathing/circling)", self.ask("Were the lights steady with no motion?"))
        self.put({"condition": "B3"})
        time.sleep(2)
        print("  Condition B3: like B2, but while SPEAKING the non-anchor LEDs show a topic colour"
              " (family = soft PINK), which lingers ~10 s afterwards.")
        self._ai_turn("conditions", "I miss my daughter", expect_topic="family")
        self.record("conditions", "B3 pink topic colour while speaking + afterglow",
                    self.ask("Did you see soft PINK mixed in while it spoke, fading after ~10 s?"))
        self.put({"condition": "B2"})

    def step_reduced(self):
        self.say("10. Accessibility")
        self.put({"reduced_motion": True})
        time.sleep(2)
        self.http.post("/api/v1/admin/probe", json={"status": "PROCESSING", "duration_ms": 5000})
        self.record("reduced", "reduced motion: PROCESSING is steady amber",
                    self.ask("With reduced motion, is the AMBER light now steady (not circling)?"))
        self.put({"reduced_motion": False, "lights_off": True})
        time.sleep(2)
        self.record("reduced", "lights off: ring dark", self.ask("Is the ring completely dark now?"))
        self.put({"lights_off": False})

    # ------------------------------------------------------------------- run --
    def run(self, only: list[str]) -> None:
        print(f"{B}[un]scripted hardware test{X}  (REAL_HARDWARE; answers y / n / s=skip; Ctrl-C to stop)")
        try:
            for s in STEPS:
                if s == "connect" or not only or s in only:
                    getattr(self, f"step_{s}")()
        except KeyboardInterrupt:
            print("\n  stopped early")
        finally:
            try:
                self.research({"loopback_test": False})
                self.put({"condition": "B2", "reduced_motion": False, "lights_off": False})
            except Exception:  # noqa: BLE001
                pass
            self.save()

    def save(self) -> None:
        out = ROOT / "backend" / "data" / "hw_tests"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = out / f"{stamp}.json"
        p.write_text(json.dumps({"label": "REAL_HARDWARE guided test", "time": stamp, "results": self.results}, indent=2))
        n = len(self.results)
        ok = sum(1 for r in self.results if r["ok"])
        fail = [r for r in self.results if r["ok"] is False]
        print(f"\n{B}Summary:{X} {ok}/{n} passed, {len(fail)} failed, {n - ok - len(fail)} skipped")
        for r in fail:
            print(f"  {R}FAIL{X} {r['step']}: {r['check']} {r['detail']}")
        print(f"Saved to {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma list of: " + ",".join(STEPS[1:]))
    a = ap.parse_args()
    Tester().run([x.strip() for x in a.only.split(",") if x.strip()])
    return 0


if __name__ == "__main__":
    sys.exit(main())
