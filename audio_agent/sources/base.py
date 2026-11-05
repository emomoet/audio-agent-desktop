"""Capture source interface.

A source produces mono float32 frames at 16 kHz and hands them to a sink callback
`sink(frames, t_last)`, where t_last is the wall time of the final sample.
Sources run their own thread (or the audio library's callback thread) and must
never block the sink for long: the sink only copies into a ring buffer.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..config import SourceCfg
from ..dsp import dbfs

log = logging.getLogger(__name__)

Sink = Callable[[np.ndarray, float], None]


@dataclass
class SourceHealth:
    name: str
    role: str
    type: str
    state: str = "stopped"           # stopped | starting | running | error
    device: str | None = None        # resolved device name
    native_rate: int | None = None
    last_frame_ts: float | None = None
    level_dbfs: float = -120.0
    overflows: int = 0
    error: str | None = None
    restarts: int = 0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        now = time.time()
        d = dict(self.__dict__)
        d["age_s"] = None if self.last_frame_ts is None else round(now - self.last_frame_ts, 3)
        return d


class Source:
    """Base class. Subclasses implement _open() and _close()."""

    def __init__(self, cfg: SourceCfg, sink: Sink):
        self.cfg = cfg
        self._sink = sink
        self.health = SourceHealth(name=cfg.name, role=cfg.role, type=cfg.type)
        self._lock = threading.Lock()

    def emit(self, frames: np.ndarray, t_last: float | None = None) -> None:
        if frames.size == 0:
            return
        t = time.time() if t_last is None else t_last
        self.health.last_frame_ts = t
        # exponential smoothing so the meter doesn't flicker
        lvl = dbfs(frames)
        self.health.level_dbfs = round(0.7 * self.health.level_dbfs + 0.3 * lvl, 1)
        self._sink(frames, t)

    def start(self) -> None:
        with self._lock:
            self.health.state = "starting"
            self.health.error = None
            try:
                self._open()
                self.health.state = "running"
                log.info("source %s started (%s)", self.cfg.name, self.health.device or self.cfg.type)
            except Exception as e:
                self.health.state = "error"
                self.health.error = f"{type(e).__name__}: {e}"
                log.error("source %s failed to start: %s", self.cfg.name, self.health.error)

    def stop(self) -> None:
        with self._lock:
            try:
                self._close()
            except Exception:
                log.exception("source %s close error", self.cfg.name)
            self.health.state = "stopped"

    def _open(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError
