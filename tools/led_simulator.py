#!/usr/bin/env python3
"""SIMULATED LED ring preview, rendered by the real firmware LED engine (ctypes).

The firmware file main/core/led_engine.c is compiled into a shared library
(`make -C firmware/test libled`) and called frame by frame, so the colours and
timing shown are exactly what the firmware computes. It is NOT a photo of the
physical LEDs: real colour, diffusion and brightness must be checked on the device.

  tools/led_simulator.py --out led_preview.html          # all states x B1/B2/B3 (+ reduced motion)
  tools/led_simulator.py --terminal --status LISTENING --condition B2
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "firmware" / "test" / "build" / "libled.dylib"
STATUSES = ["BOOTING", "IDLE", "LISTENING", "PROCESSING", "SPEAKING", "ERROR", "OFFLINE"]
TOPICS = ["general", "reassurance", "family", "routine", "relaxation"]
MAXP = 32


class RGB(C.Structure):
    _fields_ = [("r", C.c_uint8), ("g", C.c_uint8), ("b", C.c_uint8)]


class Cfg(C.Structure):
    _fields_ = [("count", C.c_uint8), ("offset", C.c_uint8), ("reverse", C.c_bool), ("condition", C.c_int),
                ("reduced_motion", C.c_bool), ("lights_off", C.c_bool), ("max_brightness_pct", C.c_uint8),
                ("afterglow_ms", C.c_uint32)]


class Eng(C.Structure):
    _fields_ = [("status", C.c_int), ("status_since_ms", C.c_uint32), ("topic", C.c_int), ("topic_valid", C.c_bool),
                ("afterglow_until_ms", C.c_uint32), ("transitioning", C.c_bool), ("transition_start_ms", C.c_uint32),
                ("prev_frame", RGB * MAXP), ("last_frame", RGB * MAXP)]


def lib():
    if not LIB.exists():
        subprocess.run(["make", "-s", "-C", str(ROOT / "firmware" / "test"), "libled"], check=True)
    L = C.CDLL(str(LIB))
    L.led_engine_init.argtypes = [C.POINTER(Eng), C.c_uint32]
    L.led_engine_set_status.argtypes = [C.POINTER(Eng), C.c_int, C.c_uint32]
    L.led_engine_set_topic.argtypes = [C.POINTER(Eng), C.c_int, C.c_float]
    L.led_engine_render.argtypes = [C.POINTER(Eng), C.POINTER(Cfg), C.c_uint32, C.POINTER(RGB)]
    return L


def render_sequence(L, status: str, cond: int, count: int, reduced: bool, topic: str | None, seconds: float,
                    brightness: int = 40) -> list[list[list[int]]]:
    e = Eng()
    cfg = Cfg(count=count, offset=0, reverse=False, condition=cond, reduced_motion=reduced, lights_off=False,
              max_brightness_pct=brightness, afterglow_ms=10000)
    L.led_engine_init(C.byref(e), 0)
    L.led_engine_set_status(C.byref(e), STATUSES.index("IDLE"), 0)
    out = (RGB * count)()
    for t in range(0, 200, 20):          # render idle first, as the device does continuously
        L.led_engine_render(C.byref(e), C.byref(cfg), t, out)
    t0 = 200
    if topic:
        L.led_engine_set_topic(C.byref(e), TOPICS.index(topic), C.c_float(0.9))
    L.led_engine_set_status(C.byref(e), STATUSES.index(status), t0)
    frames = []
    for i in range(int(seconds * 50)):
        L.led_engine_render(C.byref(e), C.byref(cfg), t0 + i * 20, out)
        frames.append([[p.r, p.g, p.b] for p in out])
    return frames


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>LED preview (simulated)</title>
<style>body{font:14px system-ui;background:#1b1d1b;color:#ddd;margin:16px}h1{font-size:18px}
.b{background:#a33;color:#fff;padding:6px 10px;border-radius:6px;display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.cell{background:#111;border-radius:10px;padding:8px;text-align:center}canvas{width:150px;height:150px}</style></head>
<body><h1>[un]scripted LED engine preview</h1><p class="b">SIMULATED - rendered by firmware led_engine.c via ctypes, not the physical ring.
Brightness is scaled for screen visibility; real LED colour and diffusion differ.</p>
<p>LED count: __COUNT__ (board ring). 50 Hz frames. Brightness cap 40% shown x2.5 for visibility.</p>
<div class="grid" id="g"></div><script>
const D=__DATA__;const g=document.getElementById('g');
for(const s of D){const c=document.createElement('div');c.className='cell';
c.innerHTML=`<canvas width=150 height=150></canvas><div>${s.label}</div>`;g.appendChild(c);s.ctx=c.querySelector('canvas').getContext('2d');}
let f=0;setInterval(()=>{for(const s of D){const fr=s.frames[f%s.frames.length],x=s.ctx,n=fr.length;x.fillStyle='#111';x.fillRect(0,0,150,150);
fr.forEach((p,i)=>{const a=-Math.PI/2+2*Math.PI*i/n;x.beginPath();x.arc(75+55*Math.cos(a),75+55*Math.sin(a),11,0,7);
x.fillStyle=`rgb(${p.map(v=>Math.min(255,v*2.5)).join(',')})`;x.fill();x.strokeStyle='#333';x.stroke();});}f++;},20);
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--terminal", action="store_true")
    ap.add_argument("--status", default="LISTENING", choices=STATUSES)
    ap.add_argument("--condition", default="B2", choices=["B1", "B2", "B3"])
    ap.add_argument("--count", type=int, default=7)
    a = ap.parse_args()
    L = lib()
    if a.terminal:
        print("SIMULATED (firmware led_engine.c)")
        frames = render_sequence(L, a.status, int(a.condition[1]), a.count, False, "family", 6)
        for fr in frames[::2]:
            line = "".join(f"\x1b[48;2;{min(255, r * 3)};{min(255, g * 3)};{min(255, b * 3)}m   \x1b[0m " for r, g, b in fr)
            print("\r" + line, end="", flush=True)
            time.sleep(0.04)
        print()
        return 0
    data = []
    for cond in (1, 2, 3):
        for st in STATUSES[1:]:
            topic = "family" if (cond == 3 and st == "SPEAKING") else None
            data.append({"label": f"B{cond} {st}{' +topic family' if topic else ''}",
                         "frames": render_sequence(L, st, cond, a.count, False, topic, 4.8)})
    for st in ("LISTENING", "PROCESSING", "SPEAKING"):
        data.append({"label": f"B2 {st} reduced motion", "frames": render_sequence(L, st, 2, a.count, True, None, 4.8)})
    out = Path(a.out or (ROOT / "docs" / "led_preview_SIMULATED.html"))
    out.write_text(HTML.replace("__DATA__", json.dumps(data)).replace("__COUNT__", str(a.count)))
    print(f"SIMULATED preview written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
