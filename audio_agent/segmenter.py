"""Energy-based endpointing: cuts the broker role's audio into utterances (candidate orders).

Assumption (from the desk): the trader is silent while the broker gives the order, so an
order is bounded by silence — or by the trader starting to talk, when a separate trader
channel exists. The detector tracks an adaptive noise floor so pit noise level changes
over the day don't break it.

Segments are emitted as absolute ring-buffer sample indices; the consumer reads the
audio back from the buffer, so nothing is copied twice.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .config import SegmenterCfg
from .dsp import dbfs
from .ringbuffer import RATE

log = logging.getLogger(__name__)

FRAME = int(RATE * 0.02)          # 20 ms analysis frames
PREROLL = int(RATE * 0.25)        # keep 250 ms before first speech frame (soft onsets)
POSTROLL = int(RATE * 0.20)
ABS_FLOOR_DB = -60.0              # never call anything quieter than this speech


@dataclass
class Segment:
    id: int
    role: str
    start_idx: int
    end_idx: int
    start_ts: float
    end_ts: float
    reason: str                    # silence | max_length | trader_spoke | replay

    @property
    def duration_s(self) -> float:
        return (self.end_idx - self.start_idx) / RATE


class Vad:
    """Frame-level speech/non-speech against an adaptive noise floor.

    The floor is a low percentile of frame levels over the last few seconds
    ("minimum statistics"): speech has gaps between syllables and words, steady pit noise
    doesn't, so a step change in crowd noise lifts the floor within ~8 s instead of
    being mistaken for one endless utterance.
    """

    WINDOW = 400          # frames (8 s): longer than any gap-free stretch of real speech
    PERCENTILE = 5

    def __init__(self, threshold_db: float):
        self.threshold_db = threshold_db
        self.floor_db = -50.0
        self._levels: deque[float] = deque(maxlen=self.WINDOW)

    def is_speech(self, frame: np.ndarray) -> bool:
        lvl = dbfs(frame)
        self._levels.append(lvl)
        if len(self._levels) >= 10:
            self.floor_db = float(np.percentile(self._levels, self.PERCENTILE))
        else:
            self.floor_db = min(self._levels)
        return lvl > max(self.floor_db + self.threshold_db, ABS_FLOOR_DB)


class Segmenter:
    def __init__(self, cfg: SegmenterCfg, on_segment: Callable[[Segment], None]):
        self.cfg = cfg
        self.on_segment = on_segment
        self._vad = Vad(cfg.threshold_db_above_floor)
        self._trader_vad = Vad(cfg.threshold_db_above_floor)
        self._lock = threading.Lock()
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_start = 0        # absolute index of _pending[0]
        self._seq = 0
        self._suppressed = False
        self._reset_state()
        self.stats = {"segments": 0, "dropped_short": 0}

    def _reset_state(self):
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._seg_start = None        # absolute index of first speech frame
        self._last_speech_end = None
        self._t_ref = (0, 0.0)        # (abs index, wall time) for index->time

    # -- configuration -------------------------------------------------------
    def update_cfg(self, cfg: SegmenterCfg):
        with self._lock:
            self.cfg = cfg
            self._vad.threshold_db = cfg.threshold_db_above_floor
            self._trader_vad.threshold_db = cfg.threshold_db_above_floor

    def set_suppressed(self, on: bool, now_idx: int | None = None, now_ts: float | None = None):
        """While a replay plays, ignore input (it may be our own audio leaking into the mic)."""
        with self._lock:
            if on and self._in_speech and self._last_speech_end is not None:
                self._close("replay")
            self._suppressed = on
            if on:
                self._reset_state()

    # -- input ---------------------------------------------------------------
    def feed(self, frames: np.ndarray, end_idx: int, t_last: float) -> None:
        """frames end at absolute index end_idx (exclusive); t_last = wall time of last sample."""
        with self._lock:
            if self._pending.size == 0:
                self._pending_start = end_idx - frames.size
            self._pending = np.concatenate([self._pending, frames])
            self._t_ref = (end_idx, t_last)
            while self._pending.size >= FRAME:
                f = self._pending[:FRAME]
                fidx = self._pending_start
                self._pending = self._pending[FRAME:]
                self._pending_start += FRAME
                self._step(f, fidx)

    def trader_feed(self, frames: np.ndarray) -> None:
        """Trader-channel audio: if the trader starts speaking, the broker's turn is over."""
        if frames.size < FRAME:
            return
        with self._lock:
            speaking = any(self._trader_vad.is_speech(frames[i:i + FRAME])
                           for i in range(0, frames.size - FRAME + 1, FRAME))
            if speaking and self._in_speech:
                self._close("trader_spoke")

    # -- state machine ---------------------------------------------------------
    def _step(self, frame: np.ndarray, fidx: int) -> None:
        if self._suppressed:
            return
        speech = self._vad.is_speech(frame)
        cfg = self.cfg
        if speech:
            if self._seg_start is None:
                self._seg_start = fidx
            self._speech_frames += 1
            self._silence_frames = 0
            self._last_speech_end = fidx + FRAME
            if not self._in_speech and self._speech_frames * 20 >= cfg.min_speech_ms:
                self._in_speech = True
        else:
            self._silence_frames += 1
            if not self._in_speech:
                # isolated blips (a shout across the pit, a click) never become segments
                if self._seg_start is not None and self._silence_frames * 20 >= cfg.end_silence_ms:
                    self.stats["dropped_short"] += 1
                    self._seg_start, self._speech_frames = None, 0
                return
            if self._silence_frames * 20 >= cfg.end_silence_ms:
                self._close("silence")
                return
        if self._in_speech and self._seg_start is not None:
            if (fidx + FRAME - self._seg_start) / RATE >= cfg.max_segment_s:
                self._close("max_length")

    def _close(self, reason: str) -> None:
        start = max(0, self._seg_start - PREROLL)
        end = min(self._last_speech_end + POSTROLL, self._t_ref[0])
        ref_idx, ref_t = self._t_ref
        seg = Segment(id=self._seq + 1, role=self.cfg.role, start_idx=start, end_idx=end,
                      start_ts=ref_t - (ref_idx - start) / RATE,
                      end_ts=ref_t - (ref_idx - end) / RATE, reason=reason)
        self._seq += 1
        self.stats["segments"] += 1
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._seg_start = None
        self._last_speech_end = None
        try:
            self.on_segment(seg)
        except Exception:
            log.exception("segment consumer failed")
