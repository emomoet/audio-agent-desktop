import numpy as np
import pytest

from audio_agent.config import SegmenterCfg
from audio_agent.ringbuffer import RATE, RingBuffer
from audio_agent.segmenter import Segmenter

from conftest import synth_take


def run(audio, cfg=SegmenterCfg(), trader=None, block=320):
    rb = RingBuffer(len(audio) / RATE + 5)
    segs = []
    sg = Segmenter(cfg, segs.append)
    for i in range(0, audio.size, block):
        ch = audio[i:i + block]
        rb.write(ch, t_last=(i + ch.size) / RATE)
        sg.feed(ch, rb.written, (i + ch.size) / RATE)
        if trader is not None:
            sg.trader_feed(trader[i:i + block])
    return segs, sg


def test_one_segment_per_burst_with_correct_bounds():
    audio, spans = synth_take([2.0, 3.5, 1.5])
    segs, _ = run(audio)
    assert len(segs) == 3
    for s, (a, b) in zip(segs, spans):
        assert s.reason == "silence"
        assert s.start_idx / RATE == pytest.approx(a, abs=0.3)      # includes preroll
        assert s.end_idx / RATE == pytest.approx(b, abs=0.3)
        assert s.start_ts < s.end_ts


def test_short_blip_is_not_a_segment():
    audio, _ = synth_take([0.12, 2.0])
    segs, sg = run(audio)
    assert len(segs) == 1
    assert sg.stats["dropped_short"] == 1


def test_pause_shorter_than_end_silence_keeps_one_order():
    audio, _ = synth_take([1.5, 1.5], gap_s=0.4)            # broker draws breath mid-order
    segs, _ = run(audio)
    assert len(segs) == 1
    assert segs[0].duration_s > 3.0


def test_max_length_splits():
    audio, _ = synth_take([7.0])
    segs, _ = run(audio, SegmenterCfg(max_segment_s=3))
    assert [s.reason for s in segs][:2] == ["max_length", "max_length"]


def test_adapts_to_louder_pit_noise():
    quiet, _ = synth_take([2.0], noise_db=-55, speech_db=-20)
    # crowd gets 23 dB louder; orders given once the floor has re-settled (~8 s)
    loud, _ = synth_take([2.0, 2.0], lead_s=9.0, noise_db=-32, speech_db=-12, seed=3)
    segs, _ = run(np.concatenate([quiet, loud]))
    # quiet order, one junk stretch while the floor re-settles, then both loud orders cleanly
    assert len(segs) == 4
    for s, (a, b) in zip(segs[2:], [(13.5, 15.5), (16.7, 18.7)]):
        assert s.start_idx / RATE == pytest.approx(a, abs=0.3)
        assert s.end_idx / RATE == pytest.approx(b, abs=0.3)


def test_trader_speech_ends_broker_turn():
    audio, _ = synth_take([4.0], tail_s=0.3)
    rng = np.random.default_rng(9)
    trader = (rng.standard_normal(audio.size) * 10 ** (-60 / 20)).astype(np.float32)
    burst, _ = synth_take([0.8], lead_s=0, tail_s=0, noise_db=-60, seed=5)
    start = int(3.0 * RATE)                                    # trader talks 2 s into the order
    trader[start:start + burst.size] += burst
    segs, _ = run(audio, SegmenterCfg(trader_role="trader"), trader=trader)
    assert segs and segs[0].reason == "trader_spoke"
    assert segs[0].end_idx / RATE == pytest.approx(3.0, abs=0.35)


def test_suppressed_input_produces_nothing():
    audio, _ = synth_take([2.0, 2.0])
    rb = RingBuffer(20)
    segs = []
    sg = Segmenter(SegmenterCfg(), segs.append)
    sg.set_suppressed(True)
    for i in range(0, audio.size, 320):
        ch = audio[i:i + 320]
        rb.write(ch)
        sg.feed(ch, rb.written, 0.0)
    assert segs == []


def test_long_continuous_order_is_not_chopped():
    audio, _ = synth_take([7.0])
    segs, _ = run(audio)
    assert len(segs) == 1 and segs[0].duration_s > 6.5


def test_step_up_in_pit_noise_does_not_lock_in_speech():
    quiet, _ = synth_take([1.0], noise_db=-55)
    rng = np.random.default_rng(2)
    roar = (rng.standard_normal(RATE * 20) * 10 ** (-30 / 20)).astype(np.float32)   # crowd gets loud
    segs, _ = run(np.concatenate([quiet, roar]), SegmenterCfg(max_segment_s=15))
    # at most one junk stretch while the floor catches up, never a lock-up
    assert all(s.reason != "max_length" for s in segs)
    assert sum(s.duration_s for s in segs) < 10.0
