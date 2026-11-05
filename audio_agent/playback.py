"""Replay output.

Plays a snapshot of buffered audio to the headset output device in WASAPI shared mode,
so the voice software keeps playing the live call underneath — the trader hears both.
One replay at a time: a new replay request stops the current one.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np

from .dsp import Resampler
from .ringbuffer import RATE

log = logging.getLogger(__name__)


class Player:
    def __init__(self):
        self.on_start: Callable[[], None] | None = None
        self.on_end: Callable[[], None] | None = None
        self.state = {"playing": False, "started_ts": None, "duration_s": 0.0, "device": None,
                      "error": None, "count": 0}

    def play(self, audio: np.ndarray) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def set_device(self, name: str) -> None:
        pass

    def _started(self, duration: float):
        self.state.update(playing=True, started_ts=time.time(), duration_s=round(duration, 2), error=None)
        self.state["count"] += 1
        if self.on_start:
            self.on_start()

    def _ended(self):
        if self.state["playing"]:
            self.state["playing"] = False
            if self.on_end:
                self.on_end()


class SoundDevicePlayer(Player):
    def __init__(self, device: str):
        super().__init__()
        self.device_substr = device
        self._stream = None
        self._lock = threading.Lock()

    def set_device(self, name: str) -> None:
        self.device_substr = name

    def play(self, audio: np.ndarray) -> None:
        import sounddevice as sd

        from .sources.wasapi import find_device

        self.stop()
        with self._lock:
            idx, dev = find_device(sd, self.device_substr, "output")
            rate = int(dev["default_samplerate"])
            out = Resampler(RATE, rate).process(audio) if rate != RATE else audio
            channels = min(2, int(dev["max_output_channels"]))
            data = np.repeat(out[:, None], channels, axis=1).astype(np.float32)
            pos = {"i": 0}

            def cb(outdata, frames, t, status):
                i = pos["i"]
                chunk = data[i:i + frames]
                outdata[: len(chunk)] = chunk
                if len(chunk) < frames:
                    outdata[len(chunk):] = 0
                    raise sd.CallbackStop
                pos["i"] = i + frames

            self.state["device"] = dev["name"]
            self._stream = sd.OutputStream(device=idx, samplerate=rate, channels=channels,
                                           dtype="float32", callback=cb, finished_callback=self._ended)
            self._started(audio.size / RATE)
            self._stream.start()

    def stop(self) -> None:
        with self._lock:
            s, self._stream = self._stream, None
        if s is not None:
            try:
                s.abort()
                s.close()
            except Exception:
                log.exception("stopping playback")
        self._ended()


class NullPlayer(Player):
    """No audio device: records what would have played. Used in tests and headless runs."""

    def __init__(self, speed: float = 1.0):
        super().__init__()
        self.speed = speed
        self.last_audio: np.ndarray | None = None
        self._timer: threading.Timer | None = None
        self.state["device"] = "null"

    def play(self, audio: np.ndarray) -> None:
        self.stop()
        self.last_audio = audio
        self._started(audio.size / RATE)
        self._timer = threading.Timer(audio.size / RATE / self.speed, self._ended)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._ended()
