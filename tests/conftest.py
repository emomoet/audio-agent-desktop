import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audio_agent.asr import AsrBackend, AsrResult  # noqa: E402
from audio_agent.ringbuffer import RATE  # noqa: E402


def synth_take(bursts, gap_s=1.2, lead_s=1.0, tail_s=1.5, noise_db=-50.0, speech_db=-18.0, seed=0):
    """Pit-noise background with 'speech' bursts (amplitude-modulated noise + harmonics).

    bursts: list of burst durations in seconds. Returns (audio, [(start_s, end_s), ...]).
    """
    rng = np.random.default_rng(seed)
    total = lead_s + sum(bursts) + gap_s * (len(bursts) - 1) + tail_s
    n = int(total * RATE)
    noise_amp = 10 ** (noise_db / 20) * np.sqrt(2)
    audio = (rng.standard_normal(n) * noise_amp / np.sqrt(2)).astype(np.float32)
    spans = []
    t = lead_s
    for d in bursts:
        s, e = int(t * RATE), int((t + d) * RATE)
        tt = np.arange(e - s) / RATE
        syl = 0.5 * (1 + np.sin(2 * np.pi * 4 * tt))            # ~4 syllables/s envelope
        voiced = sum(np.sin(2 * np.pi * f * tt) for f in (140, 280, 420, 900)) / 4
        amp = 10 ** (speech_db / 20) * np.sqrt(2)
        audio[s:e] += (amp * (0.3 + 0.7 * syl) * voiced).astype(np.float32)
        spans.append((t, t + d))
        t += d + gap_s
    return audio, spans


class ScriptedAsr(AsrBackend):
    """Returns scripted transcripts in order, one per segment."""

    name = "scripted"

    def __init__(self, texts, conf=0.95):
        self.texts = list(texts)
        self.conf = conf
        self.calls = []

    def transcribe(self, audio):
        self.calls.append(audio.size)
        text = self.texts.pop(0) if self.texts else ""
        return AsrResult(text=text, confidence=self.conf, backend=self.name)


@pytest.fixture
def example_config_path():
    return os.path.join(os.path.dirname(__file__), "..", "config.example.toml")
