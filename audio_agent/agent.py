"""The agent: wires sources -> ring buffers -> segmenter -> ASR -> extractor -> proposals,
and replay. Everything TP-facing goes through the methods here (the HTTP API is a thin
layer over them).
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from .asr import AsrBackend, AsrResult, make_asr
from .config import Config
from .events import EventBus, ProposalStore
from .extract import Extractor
from .playback import Player
from .ringbuffer import RATE, BufferSet
from .segmenter import Segment, Segmenter
from .sources import Source, make_source

log = logging.getLogger(__name__)

STALL_S = 3.0           # a live source with no frames for this long is restarted
MAX_BACKOFF_S = 30.0


class Agent:
    def __init__(self, cfg: Config, player: Player, asr: AsrBackend | None = None):
        self.cfg = cfg
        self.started_ts = time.time()
        self.bus = EventBus()
        self.proposals = ProposalStore(self.bus)
        self.buffers = BufferSet(cfg.buffer_seconds, cfg.roles())
        self.sources: dict[str, Source] = {}
        self._restart_at: dict[str, float] = {}
        self.player = player
        self.player.on_start = self._replay_started
        self.player.on_end = self._replay_ended
        self.segmenter = Segmenter(cfg.segmenter, self._on_segment)
        self._asr_override = asr is not None
        self.asr: AsrBackend = asr or make_asr(cfg.asr, sorted(set(cfg.symbol_aliases.values())))
        self.extractor = Extractor(cfg.extract, cfg.symbol_aliases)
        self._segq: queue.Queue[Segment] = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.config_error: str | None = None
        self.last_segment: Segment | None = None
        self.asr_state = {"backend": self.asr.name, "ready": False, "error": None,
                          "last_latency_s": None, "processed": 0, "dropped": 0}
        self._threads: list[threading.Thread] = []

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        for sc in self.cfg.enabled_sources():
            self._start_source(sc)
        for target, name in ((self._asr_worker, "asr-worker"), (self._housekeeping, "housekeeping"),
                             (self._warm_asr, "asr-warm")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self.player.stop()
        for s in list(self.sources.values()):
            s.stop()

    def _warm_asr(self) -> None:
        try:
            self.asr.warm()
            self.asr_state.update(ready=True, error=None)
        except Exception as e:
            self.asr_state.update(ready=False, error=f"{type(e).__name__}: {e}")
            log.error("ASR backend %s unavailable: %s", self.asr.name, e)

    # ---- sources -----------------------------------------------------------
    def _make_sink(self, role: str):
        def sink(frames: np.ndarray, t_last: float) -> None:
            buf = self.buffers.get(role)
            if buf is None:
                return
            buf.write(frames, t_last)
            seg_cfg = self.segmenter.cfg
            if role == seg_cfg.role:
                self.segmenter.feed(frames, buf.written, t_last)
            elif seg_cfg.trader_role and role == seg_cfg.trader_role:
                self.segmenter.trader_feed(frames)
        return sink

    def _start_source(self, sc) -> None:
        src = make_source(sc, self._make_sink(sc.role))
        self.sources[sc.name] = src
        src.start()

    def _restart_source(self, name: str) -> None:
        src = self.sources.get(name)
        if src is None:
            return
        restarts = src.health.restarts
        src.stop()
        new = make_source(src.cfg, self._make_sink(src.cfg.role))
        new.health.restarts = restarts + 1
        self.sources[name] = new
        new.start()
        backoff = min(MAX_BACKOFF_S, 2 ** min(new.health.restarts, 5))
        self._restart_at[name] = time.time() + backoff
        log.warning("source %s restarted (#%d)", name, new.health.restarts)
        self.bus.publish("source.restarted", {"name": name, "restarts": new.health.restarts})

    # ---- segments -> ASR -> proposals ---------------------------------------
    def _on_segment(self, seg: Segment) -> None:
        self.last_segment = seg
        self.bus.publish("segment", {"id": seg.id, "role": seg.role, "duration_s": round(seg.duration_s, 2),
                                     "reason": seg.reason, "start_ts": seg.start_ts, "end_ts": seg.end_ts})
        try:
            self._segq.put_nowait(seg)
        except queue.Full:
            self.asr_state["dropped"] += 1
            log.warning("ASR queue full, dropped segment %d", seg.id)

    def _asr_worker(self) -> None:
        while not self._stop.is_set():
            try:
                seg = self._segq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_segment(seg)
            except Exception:
                log.exception("segment %d processing failed", seg.id)

    def process_segment(self, seg: Segment) -> None:
        buf = self.buffers.get(seg.role)
        if buf is None:
            return
        try:
            audio = buf.read_range(seg.start_idx, seg.end_idx)
        except IndexError:
            log.warning("segment %d already overwritten", seg.id)
            return
        t0 = time.monotonic()
        result: AsrResult = self.asr.transcribe(audio)
        self.asr_state["processed"] += 1
        self.asr_state["last_latency_s"] = round(time.monotonic() - t0, 3)
        if self.cfg.agent.log_transcripts:
            log.info("segment %d transcript (conf %.2f): %s", seg.id, result.confidence, result.text)
        segd = {"id": seg.id, "role": seg.role, "start_ts": seg.start_ts, "end_ts": seg.end_ts,
                "duration_s": round(seg.duration_s, 2), "reason": seg.reason}
        self.bus.publish("transcript", {"segment": segd, "text": result.text, "conf": result.confidence})
        p = self.extractor.run(result, segd)
        if p is not None:
            self.proposals.add(p)

    # ---- replay --------------------------------------------------------------
    def replay(self, seconds: float | None = None, roles: list[str] | None = None,
               target: str = "last_seconds") -> dict:
        rc = self.cfg.replay
        if target == "last_segment":
            seg = self.last_segment
            if seg is None:
                raise LookupError("no segment yet")
            buf = self.buffers.get(seg.role)
            try:
                audio = buf.read_range(seg.start_idx, seg.end_idx)
            except IndexError:
                raise LookupError("last segment no longer buffered") from None
            mixed, used_roles, secs = audio * rc.gain, [seg.role], seg.duration_s
        else:
            secs = float(seconds if seconds is not None else rc.default_seconds)
            if secs <= 0 or secs > self.cfg.buffer_seconds:
                raise ValueError(f"seconds must be in (0, {self.cfg.buffer_seconds}]")
            roles = list(roles or rc.default_roles)
            parts = []
            for r in roles:
                b = self.buffers.get(r)
                if b is None:
                    raise LookupError(f"no buffer for role {r!r}")
                parts.append(b.read_last(secs)[0])
            n = max((p.size for p in parts), default=0)
            if n == 0:
                raise LookupError("nothing buffered yet")
            mixed = np.zeros(n, dtype=np.float32)
            for p in parts:                       # right-align: every role ends 'now'
                mixed[n - p.size:] += p
            mixed = np.clip(mixed * rc.gain, -1.0, 1.0)
            used_roles = roles
            secs = n / RATE
        self.player.play(mixed.astype(np.float32))
        info = {"target": target, "roles": used_roles, "seconds": round(secs, 2)}
        self.bus.publish("replay.started", info)
        return info

    def stop_replay(self) -> None:
        self.player.stop()

    def _replay_started(self) -> None:
        if self.cfg.segmenter.suppress_during_replay:
            self.segmenter.set_suppressed(True)

    def _replay_ended(self) -> None:
        self.segmenter.set_suppressed(False)
        self.bus.publish("replay.stopped", {})

    # ---- config hot reload ---------------------------------------------------
    def apply_config(self, new: Config) -> None:
        with self._lock:
            old = self.cfg
            self.cfg = new
            self.buffers.ensure(new.roles(), new.buffer_seconds)
            old_src = {s.name: s for s in old.enabled_sources()}
            new_src = {s.name: s for s in new.enabled_sources()}
            for name in set(old_src) - set(new_src):
                self.sources.pop(name).stop()
            for name, sc in new_src.items():
                if name not in old_src:
                    self._start_source(sc)
                elif sc != old_src[name]:
                    self.sources.pop(name).stop()
                    self._start_source(sc)
            if new.segmenter != old.segmenter:
                if new.segmenter.role != old.segmenter.role:
                    self.segmenter = Segmenter(new.segmenter, self._on_segment)
                else:
                    self.segmenter.update_cfg(new.segmenter)
            if not self._asr_override and new.asr != old.asr:
                self.asr = make_asr(new.asr, sorted(set(new.symbol_aliases.values())))
                self.asr_state.update(backend=self.asr.name, ready=False)
                threading.Thread(target=self._warm_asr, daemon=True).start()
            self.extractor = Extractor(new.extract, new.symbol_aliases)
            if new.replay.output_device != old.replay.output_device:
                self.player.set_device(new.replay.output_device)
            self.config_error = None
        self.bus.publish("config.applied", {})

    # ---- housekeeping: proposal TTL + source watchdog --------------------------
    def _housekeeping(self) -> None:
        last_health = 0.0
        while not self._stop.wait(0.5):
            self.proposals.sweep()
            now = time.time()
            for name, src in list(self.sources.items()):
                h = src.health
                if src.cfg.type == "file":
                    continue
                if now < self._restart_at.get(name, 0):
                    continue
                stalled = h.state == "running" and h.last_frame_ts is not None and now - h.last_frame_ts > STALL_S
                if h.state == "error" or stalled:
                    self._restart_source(name)
            if now - last_health > 2.0 and self.bus.subscriber_count:
                self.bus.publish("health", self.health())
                last_health = now

    # ---- health ---------------------------------------------------------------
    def health(self) -> dict:
        srcs = [s.health.as_dict() for s in self.sources.values()]
        buffers = {r: round(self.buffers.get(r).available_seconds(), 1) for r in self.buffers.roles()}
        broker_ok = any(s["role"] == self.cfg.segmenter.role and s["state"] == "running"
                        and s["age_s"] is not None and s["age_s"] < STALL_S for s in srcs)
        status = "ok" if broker_ok and not self.config_error else "degraded"
        return {
            "status": status,
            "uptime_s": round(time.time() - self.started_ts, 1),
            "sources": srcs,
            "buffers_s": buffers,
            "replay": {**self.player.state, "options": list(self.cfg.replay.options),
                       "default_seconds": self.cfg.replay.default_seconds},
            "segmenter": {**self.segmenter.stats, "role": self.cfg.segmenter.role,
                          "noise_floor_db": round(self.segmenter._vad.floor_db, 1)},
            "asr": dict(self.asr_state),
            "extract": {"backend": self.cfg.extract.backend, **self.proposals.stats},
            "config": {"error": self.config_error},
        }
