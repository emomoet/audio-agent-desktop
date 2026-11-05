from ..config import SourceCfg
from .base import Sink, Source, SourceHealth
from .file import FileSource
from .rtp import RtpSource
from .wasapi import WasapiLoopbackSource, WasapiSource

_TYPES = {
    "wasapi": WasapiSource,
    "wasapi_loopback": WasapiLoopbackSource,
    "file": FileSource,
    "rtp": RtpSource,
}


def make_source(cfg: SourceCfg, sink: Sink) -> Source:
    return _TYPES[cfg.type](cfg, sink)


__all__ = ["Source", "SourceHealth", "make_source", "FileSource"]
