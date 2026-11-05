"""Speech-to-text backends.

Every backend returns an AsrResult: the text plus a confidence in [0, 1] and per-word
probabilities where available. Confidence feeds the per-field confidence downstream —
a low-confidence word cannot produce a high-confidence order field.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from ..config import AsrCfg

log = logging.getLogger(__name__)


@dataclass
class Word:
    text: str
    prob: float
    start: float = 0.0
    end: float = 0.0


@dataclass
class AsrResult:
    text: str
    confidence: float
    words: list[Word] = field(default_factory=list)
    backend: str = ""
    latency_s: float = 0.0


class AsrBackend:
    name = "base"

    def transcribe(self, audio: np.ndarray) -> AsrResult:
        raise NotImplementedError

    def warm(self) -> None:
        """Load models up front so the first order isn't slow."""


class NullAsr(AsrBackend):
    name = "none"

    def transcribe(self, audio: np.ndarray) -> AsrResult:
        return AsrResult(text="", confidence=0.0, backend=self.name)


class FasterWhisperAsr(AsrBackend):
    """Local Whisper (CTranslate2). Audio never leaves the trader PC."""

    name = "faster_whisper"

    def __init__(self, cfg: AsrCfg, extra_terms: list[str]):
        self.cfg = cfg
        self.terms = list(dict.fromkeys([*cfg.vocabulary, *extra_terms]))
        self._model = None

    def warm(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy, heavy

            log.info("loading whisper model %s (%s/%s)", self.cfg.model, self.cfg.device, self.cfg.compute_type)
            self._model = WhisperModel(self.cfg.model, device=self.cfg.device,
                                       compute_type=self.cfg.compute_type)

    def transcribe(self, audio: np.ndarray) -> AsrResult:
        import time

        self.warm()
        t0 = time.monotonic()
        # The initial prompt biases decoding toward floor vocabulary and ticker names.
        prompt = "Options order. " + ", ".join(self.terms[:200]) + "."
        segments, _info = self._model.transcribe(
            audio.astype(np.float32), language="en", beam_size=self.cfg.beam_size,
            initial_prompt=prompt, word_timestamps=True, vad_filter=False,
            condition_on_previous_text=False, temperature=0.0)
        words: list[Word] = []
        texts = []
        for s in segments:
            texts.append(s.text.strip())
            for w in (s.words or []):
                words.append(Word(w.word.strip(), float(w.probability), float(w.start), float(w.end)))
        conf = math.exp(sum(math.log(max(w.prob, 1e-6)) for w in words) / len(words)) if words else 0.0
        return AsrResult(text=" ".join(texts).strip(), confidence=round(conf, 3), words=words,
                         backend=self.name, latency_s=round(time.monotonic() - t0, 3))


def make_asr(cfg: AsrCfg, extra_terms: list[str]) -> AsrBackend:
    if cfg.backend == "faster_whisper":
        return FasterWhisperAsr(cfg, extra_terms)
    return NullAsr()
