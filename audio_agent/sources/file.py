"""WAV file source — for replay-testing recorded takes against the pipeline.

Streams a 16-bit PCM WAV in 20 ms blocks, in real time by default (so segment timing
behaves as on the desk) or as fast as possible with realtime=false.
"""
from __future__ import annotations

import threading
import time
import wave

import numpy as np

from ..dsp import Resampler
from ..ringbuffer import RATE
from .base import Source


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        rate, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def write_wav(path: str, audio: np.ndarray, rate: int = RATE) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def wav_bytes(audio: np.ndarray, rate: int = RATE) -> bytes:
    import io

    bio = io.BytesIO()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return bio.getvalue()


class FileSource(Source):
    BLOCK_S = 0.02

    def _open(self) -> None:
        data, rate = read_wav(self.cfg.path)
        self._audio = Resampler(rate, RATE).process(data)
        self.health.device = self.cfg.path
        self.health.native_rate = rate
        self.health.extra["duration_s"] = round(self._audio.size / RATE, 2)
        self.health.extra["finished"] = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"file-{self.cfg.name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        block = int(RATE * self.BLOCK_S)
        while not self._stop.is_set():
            t0 = time.monotonic()
            for i in range(0, self._audio.size, block):
                if self._stop.is_set():
                    return
                self.emit(self._audio[i:i + block])
                if self.cfg.realtime:
                    due = t0 + (i + block) / RATE
                    delay = due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            if not self.cfg.loop:
                break
        self.health.extra["finished"] = True

    def _close(self) -> None:
        ev = getattr(self, "_stop", None)
        if ev is not None:
            ev.set()
            self._thread.join(timeout=1.0)

    def wait_finished(self, timeout: float | None = None) -> bool:
        t = getattr(self, "_thread", None)
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()
