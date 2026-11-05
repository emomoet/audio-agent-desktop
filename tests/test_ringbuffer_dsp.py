import numpy as np
import pytest

from audio_agent.dsp import Resampler, dbfs, mulaw_decode
from audio_agent.ringbuffer import RATE, BufferSet, RingBuffer


def test_wraparound_and_read_last():
    rb = RingBuffer(1.0)                       # 16000 samples
    x = np.arange(40000, dtype=np.float32)
    for i in range(0, x.size, 3000):           # writes straddle the wrap point repeatedly
        rb.write(x[i:i + 3000], t_last=100.0)
    last, _ = rb.read_last(1.0)
    assert np.array_equal(last, x[-16000:])
    assert rb.written == 40000
    assert rb.oldest_index == 24000
    assert np.array_equal(rb.read_range(30000, 30010), x[30000:30010])


def test_overwritten_range_raises():
    rb = RingBuffer(1.0)
    rb.write(np.zeros(20000, dtype=np.float32))
    with pytest.raises(IndexError):
        rb.read_range(0, 10)
    with pytest.raises(IndexError):
        rb.read_range(19990, 20010)            # not written yet


def test_single_write_larger_than_capacity():
    rb = RingBuffer(1.0)
    x = np.arange(50000, dtype=np.float32)
    rb.write(x)
    assert rb.written == 50000
    assert np.array_equal(rb.read_last(1.0)[0], x[-16000:])


def test_read_last_clips_to_available_and_reports_start_time():
    rb = RingBuffer(2.0)
    rb.write(np.ones(8000, dtype=np.float32), t_last=50.0)
    a, t0 = rb.read_last(30)
    assert a.size == 8000
    assert t0 == pytest.approx(50.0 - 0.5)


def test_resize_keeps_absolute_indices():
    rb = RingBuffer(2.0)
    x = np.arange(50000, dtype=np.float32)
    rb.write(x, t_last=10.0)
    big = rb.resized(5.0)
    assert big.written == 50000
    assert np.array_equal(big.read_range(49000, 50000), x[49000:50000])
    small = rb.resized(1.0)
    assert np.array_equal(small.read_last(1.0)[0], x[-16000:])
    small.write(np.full(100, -1, dtype=np.float32))
    assert np.array_equal(small.read_range(50000, 50100), np.full(100, -1, dtype=np.float32))


def test_bufferset_ensure_adds_roles_and_resizes():
    bs = BufferSet(10, {"broker"})
    bs.write("broker", np.ones(RATE, dtype=np.float32))
    bs.ensure({"broker", "far_end"}, 20)
    assert set(bs.roles()) == {"broker", "far_end"}
    assert bs.get("broker").capacity == 20 * RATE
    assert bs.get("broker").available_seconds() == pytest.approx(1.0)


def _tone(rate, f, secs=1.0):
    t = np.arange(int(rate * secs)) / rate
    return np.sin(2 * np.pi * f * t).astype(np.float32) * 0.5


def _peak_hz(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return np.argmax(spec) * rate / x.size


@pytest.mark.parametrize("src", [48000, 32000, 44100, 8000])
def test_resampler_streaming_preserves_tone(src):
    rs = Resampler(src, RATE)
    x = _tone(src, 1000, 2.0)
    out = np.concatenate([rs.process(x[i:i + 441]) for i in range(0, x.size, 441)])
    assert abs(out.size - 2 * RATE) <= 3
    assert abs(_peak_hz(out[1000:-1000], RATE) - 1000) < 10


def test_resampler_rejects_alias_above_nyquist():
    rs = Resampler(48000, RATE)
    out = rs.process(_tone(48000, 12000))      # above the 8 kHz output Nyquist
    assert dbfs(out[200:]) < dbfs(_tone(48000, 12000)) - 30


def test_mulaw_decode_known_points():
    v = mulaw_decode(bytes([0xFF, 0x7F, 0x00, 0x80]))
    assert v[0] == 0.0 and v[1] == 0.0
    assert v[2] < -0.9 and v[3] > 0.9
