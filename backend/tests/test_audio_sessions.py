import asyncio

import numpy as np
from scipy.signal import resample_poly

from app.audio import StreamingResampler, decode_wav_to_device_pcm, pcm_stats, pcm_to_wav, soft_limit
from app.sessions import SessionStore


def test_streaming_resampler_matches_resample_poly():
    rng = np.random.default_rng(1)
    t = np.arange(24000 * 2) / 24000
    x = (8000 * np.sin(2 * np.pi * 440 * t) + 500 * rng.standard_normal(t.size)).astype("<i2")
    rs = StreamingResampler(24000, 16000, 1.0)
    out = bytearray()
    raw = x.tobytes()
    i = 0
    for size in [1, 4095, 333, 4096, 7, 10000, 2] * 10:   # odd/unaligned chunk sizes
        out += rs.process(raw[i:i + size]); i += size
    out += rs.process(raw[i:]) + rs.flush()
    y = np.frombuffer(bytes(out), "<i2").astype(float)
    ref = resample_poly(x.astype(float), 2, 3, window=rs.taps / 2)
    n = min(len(y), len(ref))
    assert len(y) == len(ref)
    err = np.max(np.abs(y - ref))
    assert err <= 1.0, err   # rounding only


def test_soft_limit_never_clips():
    x = np.array([-32768, 32767, 0, 20000], dtype=np.int16)
    y = soft_limit(x, 2.0)
    assert y.max() <= 32767 and y.min() >= -32768 and y[2] == 0


def test_wav_roundtrip_and_resample():
    pcm = (np.sin(np.arange(44100) / 10) * 10000).astype("<i2").tobytes()
    out, secs = decode_wav_to_device_pcm(pcm_to_wav(pcm, 44100), 60)
    assert abs(secs - 1.0) < 0.01 and abs(len(out) / 2 - 16000) < 5


def test_pcm_stats():
    s = pcm_stats(b"\x00\x00" * 100)
    assert s["all_zero"]
    s = pcm_stats(np.full(1600, 32767, "<i2").tobytes())
    assert s["clipped_fraction"] == 1.0


def test_session_expiry_and_device_isolation():
    st = SessionStore(expiry_s=600)
    s, new = st.resolve("a", None, now=0)
    assert new
    s2, new2 = st.resolve("a", s.session_id, now=599)
    assert not new2 and s2 is s
    s3, new3 = st.resolve("b", s.session_id, now=599)            # other device cannot join
    assert new3 and s3.session_id != s.session_id
    s4, new4 = st.resolve("a", s.session_id, now=599 + 601)      # expired
    assert new4


def test_superseded_turn_cannot_commit():
    st = SessionStore()
    s, _ = st.resolve("a", None)
    st.begin_turn(s, "x-1")
    st.begin_turn(s, "x-2")
    assert not st.commit(s, "x-1", "u", "a")
    assert st.commit(s, "x-2", "u", "a")
    assert len(st.history(s)) == 1


def test_history_bounded():
    st = SessionStore(max_turns=3)
    s, _ = st.resolve("a", None)
    for i in range(10):
        st.begin_turn(s, f"t-{i}")
        st.commit(s, f"t-{i}", "u", "a")
    assert [e.turn_id for e in st.history(s)] == ["t-7", "t-8", "t-9"]


def test_concurrent_turns_only_latest_commits(app, client, dev, mocks):
    """Two overlapping turns on one session: the older one is rejected as stale."""
    from .conftest import post_turn
    ctx = app.state.ctx
    gate = asyncio.Event()
    orig = mocks[1].generate

    async def slow_generate(*a, **k):
        if not gate.is_set():
            gate.set()
            await asyncio.sleep(0.3)
        return await orig(*a, **k)
    mocks[1].generate = slow_generate
    import threading
    sid = post_turn(client, dev, "b00t-1").json()["session_id"]
    results = {}

    def first():
        results["a"] = post_turn(client, dev, "b00t-2", **{"X-Session-Id": sid})
    gate.clear()
    th = threading.Thread(target=first)
    th.start()
    import time
    time.sleep(0.1)
    results["b"] = post_turn(client, dev, "b00t-3", **{"X-Session-Id": sid})
    th.join()
    assert results["b"].status_code == 200
    assert results["a"].status_code == 409 and results["a"].json()["error"]["code"] == "superseded"
    assert [e.turn_id for e in ctx.sessions._s[sid].history] == ["b00t-1", "b00t-3"]
