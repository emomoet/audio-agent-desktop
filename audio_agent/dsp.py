"""Small DSP helpers: streaming resampler, level metering, mu-law decode."""
from __future__ import annotations

import math

import numpy as np


def dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return -120.0 if rms <= 1e-9 else 20.0 * math.log10(rms)


def _lowpass_taps(cutoff: float, n: int = 63) -> np.ndarray:
    """Windowed-sinc FIR; cutoff as a fraction of the input Nyquist (0..1)."""
    m = np.arange(n) - (n - 1) / 2
    h = cutoff * np.sinc(cutoff * m) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


class Resampler:
    """Streaming mono resampler to a target rate.

    Integer down-ratios (48k->16k, 32k->16k) use FIR + decimation with carried
    filter state, which is the common WASAPI case. Anything else uses linear
    interpolation with a carried phase — adequate for speech/ASR, not for music.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        self.src, self.dst = int(src_rate), int(dst_rate)
        self.passthrough = self.src == self.dst
        self.factor = self.src // self.dst if self.src % self.dst == 0 and self.src > self.dst else 0
        if self.factor:
            self.taps = _lowpass_taps(0.9 / self.factor)
            self._hist = np.zeros(len(self.taps) - 1, dtype=np.float32)
            self._phase = 0
        else:
            self._pos = 0.0          # fractional read position into the carried stream
            self._prev = np.zeros(1, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.passthrough or x.size == 0:
            return x
        if self.factor:
            buf = np.concatenate([self._hist, x])
            y = np.convolve(buf, self.taps, mode="valid")   # len == x.size
            self._hist = buf[-(len(self.taps) - 1):]
            out = y[self._phase::self.factor]
            self._phase = (self._phase - x.size) % self.factor
            return out.astype(np.float32)
        # linear interpolation
        buf = np.concatenate([self._prev, x])
        step = self.src / self.dst
        positions = np.arange(self._pos, buf.size - 1, step)
        out = np.interp(positions, np.arange(buf.size), buf).astype(np.float32)
        next_pos = (positions[-1] + step) if positions.size else self._pos
        self._pos = next_pos - (buf.size - 1)
        self._prev = buf[-1:]
        return out


def to_mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x.mean(axis=1) if x.ndim == 2 else x


# G.711 mu-law decode table (8-bit -> float32 in [-1, 1])
def _mulaw_table() -> np.ndarray:
    t = np.zeros(256, dtype=np.float32)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        t[i] = (-sample if sign else sample) / 32768.0
    return t


MULAW = _mulaw_table()


def mulaw_decode(b: bytes) -> np.ndarray:
    return MULAW[np.frombuffer(b, dtype=np.uint8)]
