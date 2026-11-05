"""In-process pub/sub feeding the SSE endpoint, plus the in-memory proposal store."""
from __future__ import annotations

import queue
import threading
import time

from .extract.model import Proposal


class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: str, data: dict) -> None:
        msg = {"event": event, "ts": time.time(), "data": data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:  # a stalled browser tab must never block audio
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class ProposalStore:
    """Proposals (and their transcripts) live only in memory and only until TTL / ack.

    Nothing here is ever written to disk (assumption pending counsel's ruling on whether
    transcripts are records).
    """

    def __init__(self, bus: EventBus, include_transcript: bool = True):
        self.bus = bus
        self._items: dict[str, Proposal] = {}
        self._lock = threading.Lock()
        self.include_transcript = include_transcript
        self.stats = {"created": 0, "loaded": 0, "dismissed": 0, "expired": 0}

    def add(self, p: Proposal) -> None:
        with self._lock:
            self._items[p.id] = p
            self.stats["created"] += 1
        self.bus.publish("proposal.new", p.to_dict())

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda p: p.created_ts, reverse=True)
        return [p.to_dict() for p in items]

    def get(self, pid: str) -> Proposal | None:
        with self._lock:
            return self._items.get(pid)

    def close(self, pid: str, action: str) -> bool:
        with self._lock:
            p = self._items.pop(pid, None)
            if p is None:
                return False
            self.stats[action] = self.stats.get(action, 0) + 1
        self.bus.publish("proposal.closed", {"id": pid, "action": action})
        return True

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            dead = [pid for pid, p in self._items.items() if p.expires_ts <= now]
            for pid in dead:
                del self._items[pid]
                self.stats["expired"] += 1
        for pid in dead:
            self.bus.publish("proposal.closed", {"id": pid, "action": "expired"})
        return len(dead)
