"""Fixed-capacity per-role audio ring buffer.

Samples are mono float32 at RATE. Positions are absolute sample counts since the
buffer was created ("sample index"), so a segment can be referenced by index and
read back later as long as it has not been overwritten.

Time: each write records the wall-clock time of its LAST sample. Wall time of any
index is derived from the most recent write (index -> t_last - (last_idx - idx)/RATE).
"""
from __future__ import annotations

import threading
import time

import numpy as np

RATE = 16000


class RingBuffer:
    def __init__(self, seconds: float, rate: int = RATE):
        self.rate = rate
        self.capacity = int(seconds * rate)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._written = 0            # absolute samples written ever
        self._t_last = 0.0           # wall time of sample index _written-1
        self._lock = threading.Lock()

    # -- write --------------------------------------------------------------
    def write(self, frames: np.ndarray, t_last: float | None = None) -> None:
        x = np.asarray(frames, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        with self._lock:
            if x.size >= self.capacity:
                self._written += x.size - self.capacity
                x = x[-self.capacity:]
            start = self._written % self.capacity
            end = start + x.size
            if end <= self.capacity:
                self._buf[start:end] = x
            else:
                k = self.capacity - start
                self._buf[start:] = x[:k]
                self._buf[: end - self.capacity] = x[k:]
            self._written += x.size
            self._t_last = time.time() if t_last is None else t_last

    # -- read ---------------------------------------------------------------
    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    @property
    def oldest_index(self) -> int:
        with self._lock:
            return max(0, self._written - self.capacity)

    def available_seconds(self) -> float:
        with self._lock:
            return min(self._written, self.capacity) / self.rate

    def last_write_time(self) -> float:
        with self._lock:
            return self._t_last

    def read_range(self, start: int, end: int) -> np.ndarray:
        """Absolute indices [start, end). Raises if any part was overwritten or not yet written."""
        with self._lock:
            lo = max(0, self._written - self.capacity)
            if start < lo or end > self._written or start > end:
                raise IndexError(f"range [{start},{end}) not in buffer [{lo},{self._written})")
            n = end - start
            out = np.empty(n, dtype=np.float32)
            s = start % self.capacity
            if s + n <= self.capacity:
                out[:] = self._buf[s:s + n]
            else:
                k = self.capacity - s
                out[:k] = self._buf[s:]
                out[k:] = self._buf[: n - k]
            return out

    def read_last(self, seconds: float) -> tuple[np.ndarray, float]:
        """Most recent `seconds` (clipped to what exists). Returns (audio, wall time of first sample)."""
        with self._lock:
            n = min(int(seconds * self.rate), self._written, self.capacity)
            end = self._written
            t_last = self._t_last
        audio = self.read_range(end - n, end)
        return audio, t_last - n / self.rate

    def index_at(self, wall_time: float) -> int:
        with self._lock:
            return int(self._written - (self._t_last - wall_time) * self.rate)

    def time_of(self, index: int) -> float:
        with self._lock:
            return self._t_last - (self._written - index) / self.rate

    # -- resize (hot reload) ------------------------------------------------
    def resized(self, seconds: float) -> "RingBuffer":
        """New buffer of a different depth carrying over as much recent audio as fits."""
        nb = RingBuffer(seconds, self.rate)
        with self._lock:
            keep = min(self._written, self.capacity, nb.capacity)
            written, t_last = self._written, self._t_last
        # keep absolute indices stable for in-flight segments
        nb._written = written - keep
        if keep:
            nb.write(self.read_range(written - keep, written), t_last)
        else:
            nb._written = written
        return nb


class BufferSet:
    """Role -> RingBuffer, with an update hook for hot-reloaded depth."""

    def __init__(self, seconds: float, roles: set[str], rate: int = RATE):
        self.seconds = seconds
        self.rate = rate
        self._lock = threading.Lock()
        self._bufs: dict[str, RingBuffer] = {r: RingBuffer(seconds, rate) for r in roles}

    def get(self, role: str) -> RingBuffer | None:
        with self._lock:
            return self._bufs.get(role)

    def roles(self) -> list[str]:
        with self._lock:
            return sorted(self._bufs)

    def ensure(self, roles: set[str], seconds: float) -> None:
        with self._lock:
            for r in roles:
                if r not in self._bufs:
                    self._bufs[r] = RingBuffer(seconds, self.rate)
            if seconds != self.seconds:
                self._bufs = {r: b.resized(seconds) for r, b in self._bufs.items()}
                self.seconds = seconds

    def write(self, role: str, frames: np.ndarray, t_last: float | None = None) -> None:
        b = self.get(role)
        if b is not None:
            b.write(frames, t_last)
