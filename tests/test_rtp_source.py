import socket
import struct
import time

import numpy as np

from audio_agent.config import SourceCfg
from audio_agent.sources.rtp import RtpSource


def test_pcmu_stream_decodes_and_counts_loss():
    got = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    src = RtpSource(SourceCfg(name="r", role="far_end", type="rtp", listen=f"127.0.0.1:{port}"),
                    lambda f, t: got.append(f))
    src.start()
    assert src.health.state == "running"
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = bytes([0x00] * 160)                       # 20 ms of loud PCMU @ 8 kHz
    for seq in [1, 2, 3, 5, 6]:                         # seq 4 lost
        hdr = struct.pack("!BBHII", 0x80, 0, seq, seq * 160, 0xABCD)
        tx.sendto(hdr + payload, ("127.0.0.1", port))
    t0 = time.time()
    while src.health.extra["packets"] < 5 and time.time() - t0 < 3:
        time.sleep(0.02)
    src.stop()
    ex = src.health.extra
    assert ex["packets"] == 5 and ex["lost"] == 1 and ex["ssrc"] == 0xABCD
    audio = np.concatenate(got)
    assert abs(audio.size - 5 * 320) <= 4               # resampled 8 k -> 16 k
    assert np.abs(audio[50:]).mean() > 0.5
