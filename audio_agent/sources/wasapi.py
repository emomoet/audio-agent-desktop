"""Windows audio sources.

- WasapiSource: a capture endpoint (headset mic, outward mic) via sounddevice/PortAudio
  in WASAPI *shared* mode — it can never take the device away from the voice software.
- WasapiLoopbackSource: what a render endpoint is playing (headset earphone) via the
  `soundcard` library, which exposes WASAPI loopback. Note it also captures any replay
  this agent plays to the same device.

Devices are matched by case-insensitive substring of the Windows friendly name, because
indices change on reboot and USB re-plug.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from ..dsp import Resampler, to_mono
from ..ringbuffer import RATE
from .base import Source

log = logging.getLogger(__name__)


def find_device(sd, name_substr: str, kind: str) -> tuple[int, dict]:
    """Resolve a device by name substring. Prefers the WASAPI host API on Windows."""
    want = name_substr.lower()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    hostapis = sd.query_hostapis()
    matches = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and want in d["name"].lower():
            api = hostapis[d["hostapi"]]["name"]
            matches.append((0 if "WASAPI" in api else 1, i, d))
    if not matches:
        raise LookupError(f"no {kind} device matching {name_substr!r}")
    matches.sort(key=lambda m: m[0])
    _, idx, dev = matches[0]
    return idx, dev


class WasapiSource(Source):
    def _open(self) -> None:
        import sounddevice as sd  # lazy: PortAudio only exists on the desk machine

        idx, dev = find_device(sd, self.cfg.device, "input")
        rate = int(dev["default_samplerate"])
        channels = min(int(dev["max_input_channels"]), 2)
        self.health.device = dev["name"]
        self.health.native_rate = rate
        self._rs = Resampler(rate, RATE)

        def cb(indata, frames, t, status):
            if status and status.input_overflow:
                self.health.overflows += 1
            self.emit(self._rs.process(to_mono(indata)))

        extra = None
        try:
            if "WASAPI" in sd.query_hostapis(dev["hostapi"])["name"]:
                extra = sd.WasapiSettings(exclusive=False)
        except Exception:
            extra = None
        self._stream = sd.InputStream(device=idx, samplerate=rate, channels=channels,
                                      dtype="float32", blocksize=int(rate * 0.02),
                                      callback=cb, extra_settings=extra)
        self._stream.start()

    def _close(self) -> None:
        s = getattr(self, "_stream", None)
        if s is not None:
            s.stop()
            s.close()
            self._stream = None


class WasapiLoopbackSource(Source):
    def _open(self) -> None:
        import soundcard as scard  # lazy

        want = self.cfg.device.lower()
        mics = [m for m in scard.all_microphones(include_loopback=True)
                if getattr(m, "isloopback", False) and want in m.name.lower()]
        if not mics:
            raise LookupError(f"no loopback endpoint matching {self.cfg.device!r}")
        self._mic = mics[0]
        self.health.device = self._mic.name
        self.health.native_rate = 48000
        self._rs = Resampler(48000, RATE)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"loopback-{self.cfg.name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with self._mic.recorder(samplerate=48000, blocksize=960) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=960)          # 20 ms
                    self.emit(self._rs.process(to_mono(np.asarray(data))))
        except Exception as e:
            self.health.state = "error"
            self.health.error = f"{type(e).__name__}: {e}"
            log.error("loopback %s died: %s", self.cfg.name, self.health.error)

    def _close(self) -> None:
        ev = getattr(self, "_stop", None)
        if ev is not None:
            ev.set()
            self._thread.join(timeout=1.0)


def list_devices() -> list[dict]:
    """For `python -m audio_agent --list-devices`."""
    import sounddevice as sd

    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        out.append({"index": i, "name": d["name"], "hostapi": apis[d["hostapi"]]["name"],
                    "in": d["max_input_channels"], "out": d["max_output_channels"],
                    "rate": d["default_samplerate"]})
    try:
        import soundcard as scard
        for m in scard.all_microphones(include_loopback=True):
            if getattr(m, "isloopback", False):
                out.append({"index": None, "name": m.name, "hostapi": "WASAPI loopback (soundcard)",
                            "in": m.channels, "out": 0, "rate": 48000})
    except Exception:
        pass
    return out
