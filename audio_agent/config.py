"""Config loading, validation and hot reload.

The config is plain TOML (stdlib tomllib). Validation is strict: an invalid file is
rejected as a whole and the previous config stays in force.
"""
from __future__ import annotations

import logging
import os
import threading
import tomllib
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

SOURCE_TYPES = {"wasapi", "wasapi_loopback", "file", "rtp"}
ROLES = {"broker", "trader", "far_end"}
ASR_BACKENDS = {"faster_whisper", "none"}
EXTRACT_BACKENDS = {"rules", "anthropic", "none"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceCfg:
    name: str
    role: str
    type: str
    device: str = ""
    path: str = ""
    listen: str = ""
    loop: bool = False
    realtime: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class AgentCfg:
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: tuple[str, ...] = ()
    token: str = ""
    log_level: str = "INFO"
    log_transcripts: bool = False


@dataclass(frozen=True)
class ReplayCfg:
    output_device: str = ""
    default_seconds: int = 30
    options: tuple[int, ...] = (30, 60)
    default_roles: tuple[str, ...] = ("broker",)
    gain: float = 1.0


@dataclass(frozen=True)
class SegmenterCfg:
    role: str = "broker"
    trader_role: str = ""
    min_speech_ms: int = 300
    end_silence_ms: int = 700
    max_segment_s: float = 15.0
    threshold_db_above_floor: float = 10.0
    suppress_during_replay: bool = True


@dataclass(frozen=True)
class AsrCfg:
    backend: str = "faster_whisper"
    model: str = "medium.en"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    vocabulary: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractCfg:
    backend: str = "rules"
    min_field_conf: float = 0.6
    proposal_ttl_s: float = 120.0
    anthropic_model: str = "claude-haiku-5-5"


@dataclass(frozen=True)
class Config:
    agent: AgentCfg
    buffer_seconds: float
    sources: tuple[SourceCfg, ...]
    replay: ReplayCfg
    segmenter: SegmenterCfg
    asr: AsrCfg
    extract: ExtractCfg
    symbol_aliases: dict = field(default_factory=dict)

    def enabled_sources(self) -> list[SourceCfg]:
        return [s for s in self.sources if s.enabled]

    def roles(self) -> set[str]:
        return {s.role for s in self.enabled_sources()}

    def redacted(self) -> dict:
        """Config view safe to return over the API (token removed)."""
        a = self.agent
        return {
            "agent": {"host": a.host, "port": a.port, "allowed_origins": list(a.allowed_origins),
                      "token_set": bool(a.token), "log_transcripts": a.log_transcripts},
            "buffer_seconds": self.buffer_seconds,
            "sources": [s.__dict__ for s in self.sources],
            "replay": {**self.replay.__dict__, "options": list(self.replay.options),
                       "default_roles": list(self.replay.default_roles)},
            "segmenter": self.segmenter.__dict__,
            "asr": {**self.asr.__dict__, "vocabulary": list(self.asr.vocabulary)},
            "extract": self.extract.__dict__,
            "symbols": dict(self.symbol_aliases),
        }


def _sub(d: dict, key: str) -> dict:
    v = d.get(key, {})
    if not isinstance(v, dict):
        raise ConfigError(f"[{key}] must be a table")
    return v


def _known(table: dict, cls, where: str) -> dict:
    allowed = set(cls.__dataclass_fields__)
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"[{where}] unknown keys: {sorted(unknown)}")
    return table


def parse(raw: dict) -> Config:
    unknown_top = set(raw) - {"agent", "buffer", "source", "replay", "segmenter", "asr", "extract", "symbols"}
    if unknown_top:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown_top)}")

    a = _known(dict(_sub(raw, "agent")), AgentCfg, "agent")
    if "allowed_origins" in a:
        a["allowed_origins"] = tuple(a["allowed_origins"])
    agent = AgentCfg(**a)
    if agent.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("agent.host must be loopback (127.0.0.1); the agent is desk-local only")
    if not (0 <= int(agent.port) < 65536):
        raise ConfigError("agent.port out of range")

    buf = _sub(raw, "buffer")
    buffer_seconds = float(buf.get("seconds", 90))
    if not (10 <= buffer_seconds <= 600):
        raise ConfigError("buffer.seconds must be between 10 and 600")

    srcs_raw = raw.get("source", [])
    if not isinstance(srcs_raw, list):
        raise ConfigError("[[source]] must be an array of tables")
    sources = []
    names = set()
    for i, s in enumerate(srcs_raw):
        s = _known(dict(s), SourceCfg, f"source #{i}")
        try:
            sc = SourceCfg(**s)
        except TypeError as e:
            raise ConfigError(f"source #{i}: {e}") from None
        if sc.name in names:
            raise ConfigError(f"duplicate source name {sc.name!r}")
        names.add(sc.name)
        if sc.type not in SOURCE_TYPES:
            raise ConfigError(f"source {sc.name}: type must be one of {sorted(SOURCE_TYPES)}")
        if sc.role not in ROLES:
            raise ConfigError(f"source {sc.name}: role must be one of {sorted(ROLES)}")
        if sc.type in ("wasapi", "wasapi_loopback") and not sc.device:
            raise ConfigError(f"source {sc.name}: device (name substring) required")
        if sc.type == "file" and not sc.path:
            raise ConfigError(f"source {sc.name}: path required")
        if sc.type == "rtp" and not sc.listen:
            raise ConfigError(f"source {sc.name}: listen host:port required")
        sources.append(sc)
    enabled_roles = [s.role for s in sources if s.enabled]
    dup_roles = {r for r in enabled_roles if enabled_roles.count(r) > 1}
    if dup_roles:
        raise ConfigError(f"more than one enabled source for role(s) {sorted(dup_roles)}")

    r = _known(dict(_sub(raw, "replay")), ReplayCfg, "replay")
    for k in ("options", "default_roles"):
        if k in r:
            r[k] = tuple(r[k])
    replay = ReplayCfg(**r)
    if any(o <= 0 or o > buffer_seconds for o in replay.options):
        raise ConfigError("replay.options must be > 0 and <= buffer.seconds")
    if replay.default_seconds not in replay.options:
        raise ConfigError("replay.default_seconds must be one of replay.options")
    if not set(replay.default_roles) <= ROLES:
        raise ConfigError("replay.default_roles contains an unknown role")
    if not (0.0 < replay.gain <= 4.0):
        raise ConfigError("replay.gain must be in (0, 4]")

    seg = SegmenterCfg(**_known(dict(_sub(raw, "segmenter")), SegmenterCfg, "segmenter"))
    if seg.role not in ROLES or (seg.trader_role and seg.trader_role not in ROLES):
        raise ConfigError("segmenter.role / trader_role must be a known role")
    if seg.end_silence_ms < 100 or seg.min_speech_ms < 50 or seg.max_segment_s < 2:
        raise ConfigError("segmenter timings too small")

    a2 = _known(dict(_sub(raw, "asr")), AsrCfg, "asr")
    if "vocabulary" in a2:
        a2["vocabulary"] = tuple(a2["vocabulary"])
    asr = AsrCfg(**a2)
    if asr.backend not in ASR_BACKENDS:
        raise ConfigError(f"asr.backend must be one of {sorted(ASR_BACKENDS)}")

    ex = ExtractCfg(**_known(dict(_sub(raw, "extract")), ExtractCfg, "extract"))
    if ex.backend not in EXTRACT_BACKENDS:
        raise ConfigError(f"extract.backend must be one of {sorted(EXTRACT_BACKENDS)}")
    if not (0.0 <= ex.min_field_conf <= 1.0):
        raise ConfigError("extract.min_field_conf must be in [0, 1]")

    sym = _sub(raw, "symbols")
    aliases = {str(k).lower(): str(v).upper() for k, v in sym.get("aliases", {}).items()}

    return Config(agent=agent, buffer_seconds=buffer_seconds, sources=tuple(sources),
                  replay=replay, segmenter=seg, asr=asr, extract=ex, symbol_aliases=aliases)


def load(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            data = f.read()
        # Windows PowerShell 5 / Notepad write a UTF-8 BOM, which TOML rejects
        raw = tomllib.loads(data.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML syntax: {e}") from None
    except TypeError as e:  # bad value types inside dataclass construction
        raise ConfigError(str(e)) from None
    try:
        return parse(raw)
    except TypeError as e:
        raise ConfigError(str(e)) from None


class ConfigWatcher:
    """Polls the config file's mtime and applies valid changes via a callback.

    Polling (not filesystem events) keeps this stdlib-only and robust to editors
    that replace files atomically.
    """

    def __init__(self, path: str, on_change: Callable[[Config], None], interval_s: float = 1.0):
        self.path = path
        self.on_change = on_change
        self.interval_s = interval_s
        self.last_error: str | None = None
        self._mtime = self._stat()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="config-watcher", daemon=True)

    def _stat(self) -> float:
        try:
            return os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return 0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def check_now(self) -> bool:
        m = self._stat()
        if m == self._mtime:
            return False
        self._mtime = m
        try:
            cfg = load(self.path)
        except ConfigError as e:
            self.last_error = str(e)
            log.error("config reload rejected, keeping previous config: %s", e)
            return False
        self.last_error = None
        log.info("config reloaded")
        self.on_change(cfg)
        return True

    def _run(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.check_now()
            except Exception:  # never let the watcher die
                log.exception("config watcher error")
