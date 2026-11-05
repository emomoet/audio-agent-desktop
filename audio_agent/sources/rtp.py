"""RTP receiver source — for a forked media stream (e.g. a SIPREC fork) from the voice platform.

Scope: media only. It listens on a UDP host:port for RTP and decodes
payload type 0 (PCMU, 8 kHz) or 11 (L16 mono, 44.1 kHz) / dynamic L16 at 16 kHz.
SIP/SIPREC signalling (INVITE, metadata, stream labels) is out of scope: the vendor
or an SBC must be configured to send the fork here.
"""
from __future__ import annotations

import socket
import struct
import threading

import numpy as np

from ..dsp import Resampler, mulaw_decode
from ..ringbuffer import RATE
from .base import Source

PT_PCMU = 0
PT_L16_44K = 11


class RtpSource(Source):
    def _open(self) -> None:
        host, port = self.cfg.listen.rsplit(":", 1)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, int(port)))
        self._sock.settimeout(0.5)
        self.health.device = f"udp://{self.cfg.listen}"
        self.health.extra.update(packets=0, bad=0, ssrc=None, last_seq=None, lost=0)
        self._resamplers: dict[int, Resampler] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"rtp-{self.cfg.name}", daemon=True)
        self._thread.start()

    def _decode(self, pt: int, payload: bytes) -> tuple[np.ndarray, int] | None:
        if pt == PT_PCMU:
            return mulaw_decode(payload), 8000
        if pt == PT_L16_44K:
            return np.frombuffer(payload, dtype=">i2").astype(np.float32) / 32768.0, 44100
        if 96 <= pt <= 127:  # assume dynamic L16/16000 (configure the fork accordingly)
            return np.frombuffer(payload, dtype=">i2").astype(np.float32) / 32768.0, 16000
        return None

    def _run(self) -> None:
        ex = self.health.extra
        while not self._stop.is_set():
            try:
                pkt, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(pkt) < 12 or (pkt[0] >> 6) != 2:
                ex["bad"] += 1
                continue
            cc = pkt[0] & 0x0F
            pt = pkt[1] & 0x7F
            seq, _ts, ssrc = struct.unpack("!HII", pkt[2:12])
            hdr = 12 + 4 * cc
            if pkt[0] & 0x10 and len(pkt) >= hdr + 4:  # header extension
                ext_len = struct.unpack("!H", pkt[hdr + 2:hdr + 4])[0]
                hdr += 4 + 4 * ext_len
            payload = pkt[hdr:]
            if pkt[0] & 0x20 and payload:  # padding
                payload = payload[: -payload[-1]]
            dec = self._decode(pt, payload)
            if dec is None:
                ex["bad"] += 1
                continue
            if ex["last_seq"] is not None:
                gap = (seq - ex["last_seq"]) & 0xFFFF
                if 1 < gap < 1000:
                    ex["lost"] += gap - 1
            ex["last_seq"], ex["ssrc"] = seq, ssrc
            ex["packets"] += 1
            audio, rate = dec
            rs = self._resamplers.setdefault(rate, Resampler(rate, RATE))
            self.emit(rs.process(audio))

    def _close(self) -> None:
        ev = getattr(self, "_stop", None)
        if ev is not None:
            ev.set()
            self._sock.close()
            self._thread.join(timeout=1.0)
