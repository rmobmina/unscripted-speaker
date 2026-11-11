"""Audio helpers: PCM/WAV conversion, resampling and level control.

Device format everywhere: PCM signed 16-bit little-endian, 16 kHz, mono.
OpenAI TTS `pcm` output: 24 kHz, 16-bit signed little-endian, mono (per the API docs).
"""
from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

DEVICE_RATE = 16000


def pcm_to_wav(pcm: bytes, rate: int = DEVICE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_stats(pcm: bytes) -> dict:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if x.size == 0:
        return {"seconds": 0.0, "rms_dbfs": -120.0, "peak": 0, "clipped_fraction": 0.0, "all_zero": True}
    rms = float(np.sqrt(np.mean(x * x)))
    return {
        "seconds": x.size / DEVICE_RATE,
        "rms_dbfs": 20 * np.log10(rms / 32768) if rms > 0 else -120.0,
        "peak": int(np.max(np.abs(x))),
        "clipped_fraction": float(np.mean(np.abs(x) >= 32700)),
        "all_zero": bool(not np.any(x)),
    }


def soft_limit(x: np.ndarray, gain: float = 1.0) -> np.ndarray:
    """Apply gain, then a smooth limiter above 0.85 FS so output never clips."""
    y = x.astype(np.float64) * gain / 32768.0
    knee = 0.85
    over = np.abs(y) > knee
    if np.any(over):
        s = np.sign(y[over])
        a = np.abs(y[over]) - knee
        y[over] = s * (knee + (1 - knee) * np.tanh(a / (1 - knee)))
    return np.clip(np.round(y * 32767), -32768, 32767).astype("<i2")


class StreamingResampler:
    """Stateful 24 kHz -> 16 kHz polyphase resampler (up 2, low-pass, down 3).

    Equivalent to scipy.signal.resample_poly(x, 2, 3) but chunk-safe, so TTS
    audio can be forwarded to the device while it is still being generated.
    """

    def __init__(self, in_rate: int = 24000, out_rate: int = DEVICE_RATE, gain: float = 1.0):
        g = gcd(in_rate, out_rate)
        self.up, self.down = out_rate // g, in_rate // g
        cutoff = 0.9 * min(in_rate, out_rate) / 2
        self.taps = firwin(97, cutoff, fs=in_rate * self.up) * self.up
        self.zi = np.zeros(len(self.taps) - 1)
        # Start at the filter's group delay so output is time-aligned with
        # resample_poly (verified in tests/test_audio_sessions.py).
        self.phase = (len(self.taps) - 1) // 2   # index in the upsampled stream of the next output sample
        self.gain = gain
        self._carry = b""
        self.n_in = 0
        self.n_out = 0
        self._flushing = False

    def process(self, chunk: bytes) -> bytes:
        data = self._carry + chunk
        usable = len(data) - (len(data) % 2)
        self._carry = data[usable:]
        if usable == 0:
            return b""
        x = np.frombuffer(data[:usable], dtype="<i2").astype(np.float64)
        if not self._flushing:
            self.n_in += x.size
        up = np.zeros(x.size * self.up)
        up[:: self.up] = x
        y, self.zi = lfilter(self.taps, 1.0, up, zi=self.zi)
        idx = np.arange(self.phase, y.size, self.down)
        self.phase = (self.phase + self.down * idx.size) - y.size if idx.size else self.phase - y.size
        out = y[idx]
        if self._flushing:
            out = out[: max(0, self._target - self.n_out)]
        self.n_out += out.size
        return soft_limit(out, self.gain).tobytes()

    def flush(self) -> bytes:
        """Drain the filter; total output length equals ceil(n_in * up / down) like resample_poly."""
        self._carry = b""
        self._flushing = True
        self._target = -(-self.n_in * self.up // self.down)
        tail = np.zeros(len(self.taps) // self.up + self.down, dtype="<i2").tobytes()
        return self.process(tail)


def decode_wav_to_device_pcm(data: bytes, max_seconds: float) -> tuple[bytes, float]:
    """Decode an uploaded WAV (8/16/24/32-bit PCM or 32-bit float, any rate,
    mono/stereo) into device PCM. Raises ValueError on unsupported input."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
            fmt_float = False
    except (wave.Error, EOFError):
        raw, ch, width, rate = _decode_float_wav(data)
        fmt_float = True
    if ch < 1 or rate < 8000 or rate > 192000:
        raise ValueError("unsupported WAV layout")
    if fmt_float:
        x = np.frombuffer(raw, dtype="<f4").astype(np.float64) * 32767
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) * 256
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        x = ((b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)) << 8 >> 8)
        x = x.astype(np.float64) / 256
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 65536
    else:
        raise ValueError("unsupported sample width")
    x = x.reshape(-1, ch).mean(axis=1)
    if x.size / rate > max_seconds:
        raise ValueError(f"recording longer than {max_seconds:.0f} s")
    if rate != DEVICE_RATE:
        g = gcd(rate, DEVICE_RATE)
        x = resample_poly(x, DEVICE_RATE // g, rate // g)
    peak = np.max(np.abs(x)) if x.size else 0
    if peak > 0:
        x = x * min(1.0, (10 ** (-3 / 20) * 32767) / peak) if peak > 32767 * 0.7 else x * ((10 ** (-6 / 20) * 32767) / peak)
    pcm = soft_limit(x).tobytes()
    return pcm, len(pcm) / 2 / DEVICE_RATE


def _decode_float_wav(data: bytes) -> tuple[bytes, int, int, int]:
    """Minimal RIFF parser for IEEE-float WAV (format tag 3), which `wave` rejects."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            raw = body
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise ValueError("WAV missing fmt/data")
    tag, ch, rate = int.from_bytes(fmt[0:2], "little"), int.from_bytes(fmt[2:4], "little"), int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if tag not in (3, 0xFFFE) or bits != 32:
        raise ValueError("unsupported WAV encoding")
    return raw, ch, 4, rate
