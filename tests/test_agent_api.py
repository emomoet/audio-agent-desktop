"""End-to-end: WAV file source -> buffer -> segmenter -> (scripted) ASR -> proposals -> HTTP API."""
import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from audio_agent.agent import Agent
from audio_agent.api import ApiServer
from audio_agent.config import parse
from audio_agent.playback import NullPlayer
from audio_agent.ringbuffer import RATE
from audio_agent.sources.file import write_wav

from conftest import ScriptedAsr, synth_take

ORIGIN = "https://tp.percev.com"


def make_cfg(wav, token="", realtime=False):
    return parse({
        "agent": {"port": 0, "allowed_origins": [ORIGIN], "token": token},
        "buffer": {"seconds": 60},
        "source": [{"name": "f", "role": "broker", "type": "file", "path": wav, "realtime": realtime}],
        "replay": {"options": [10, 30], "default_seconds": 10},
        "asr": {"backend": "none"},
        "extract": {"proposal_ttl_s": 30},
        "symbols": {"aliases": {"apple": "AAPL", "intel": "INTC"}},
    })


def req(port, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def wait_for(pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def running(tmp_path):
    audio, _ = synth_take([2.0, 2.5, 1.5])
    wav = str(tmp_path / "take.wav")
    write_wav(wav, audio)
    asr = ScriptedAsr(["Apple, May 180 190 call spread, 500 up, paying 2.10",
                       "thanks, you're done",
                       "Intel July 30 35 put spread, 400 up, 1.10 offer"])
    player = NullPlayer(speed=50)
    agent = Agent(make_cfg(wav), player, asr=asr)
    api = ApiServer(agent)
    agent.start()
    api.start()
    yield agent, api, asr, player
    api.stop()
    agent.stop()


def test_pipeline_produces_proposals_and_drops_chatter(running):
    agent, api, asr, _ = running
    assert wait_for(lambda: len(asr.calls) == 3)
    assert wait_for(lambda: len(agent.proposals.list()) == 2)
    code, _, body = req(api.port, "/proposals")
    assert code == 200
    ciphers = sorted(p["cipher"] for p in body["proposals"])
    assert ciphers == ["aapl y180;190cs z500 bb2.1", "intc l30;35ps z400 ss1.1"]
    for p in body["proposals"]:
        assert p["my_qty"] is None


def test_ack_removes_proposal(running):
    agent, api, *_ = running
    assert wait_for(lambda: len(agent.proposals.list()) == 2)
    pid = agent.proposals.list()[0]["id"]
    assert req(api.port, f"/proposals/{pid}/ack", "POST", {"action": "loaded"})[0] == 200
    assert req(api.port, f"/proposals/{pid}/ack", "POST", {"action": "loaded"})[0] == 404
    assert req(api.port, f"/proposals/{pid}/ack", "POST", {"action": "send"})[0] == 400
    assert agent.proposals.stats["loaded"] == 1


def test_replay_last_seconds_and_last_segment(running):
    agent, api, asr, player = running
    assert wait_for(lambda: len(asr.calls) == 3)
    code, _, info = req(api.port, "/replay", "POST", {"seconds": 10})
    assert code == 200 and info["roles"] == ["broker"]
    assert player.last_audio.size == 10 * RATE
    code, _, info = req(api.port, "/replay", "POST", {"target": "last_segment"})
    assert code == 200 and 1.0 < info["seconds"] < 3.0
    assert req(api.port, "/replay", "POST", {"seconds": 999})[0] == 409
    assert req(api.port, "/replay", "POST", {"roles": ["far_end"]})[0] == 409
    assert req(api.port, "/replay", "POST", {"seconds": "ten"})[0] == 400
    assert req(api.port, "/replay/stop", "POST", {})[0] == 200


def test_replay_suppresses_segmenter_until_done(running):
    agent, api, asr, player = running
    assert wait_for(lambda: len(asr.calls) == 3)
    player.speed = 1.0
    req(api.port, "/replay", "POST", {"seconds": 10})
    assert agent.segmenter._suppressed
    req(api.port, "/replay/stop", "POST", {})
    assert not agent.segmenter._suppressed


def test_origin_allowlist_and_private_network_preflight(running):
    _, api, *_ = running
    assert req(api.port, "/health", headers={"Origin": "https://evil.example"})[0] == 403
    code, headers, _ = req(api.port, "/health", headers={"Origin": ORIGIN})
    assert code == 200 and headers["Access-Control-Allow-Origin"] == ORIGIN
    r = urllib.request.Request(f"http://127.0.0.1:{api.port}/replay", method="OPTIONS",
                               headers={"Origin": ORIGIN, "Access-Control-Request-Private-Network": "true"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Private-Network"] == "true"


def test_health_shape(running):
    agent, api, asr, _ = running
    assert wait_for(lambda: len(asr.calls) == 3)
    code, _, h = req(api.port, "/health")
    assert code == 200
    assert h["sources"][0]["name"] == "f" and h["sources"][0]["device"].endswith("take.wav")
    assert h["buffers_s"]["broker"] > 5
    assert h["asr"]["processed"] == 3
    assert h["segmenter"]["segments"] == 3


def test_sse_hello_then_events(tmp_path):
    audio, _ = synth_take([1.5])
    wav = str(tmp_path / "t.wav")
    write_wav(wav, audio)
    asr = ScriptedAsr(["Apple May 180 calls 100 up paying 1.05"])
    agent = Agent(make_cfg(wav, realtime=True), NullPlayer(speed=50), asr=asr)
    api = ApiServer(agent)
    api.start()
    events = []

    def listen():
        with urllib.request.urlopen(f"http://127.0.0.1:{api.port}/events", timeout=10) as resp:
            ev = None
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("event:"):
                    ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and ev:
                    events.append((ev, json.loads(line.split(":", 1)[1])))
                    if ev == "proposal.new":
                        return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    assert wait_for(lambda: events)                 # hello arrives before audio starts
    agent.start()
    t.join(timeout=10)
    api.stop()
    agent.stop()
    kinds = [e for e, _ in events]
    assert kinds[0] == "hello" and "segment" in kinds and "transcript" in kinds and kinds[-1] == "proposal.new"
    assert events[-1][1]["cipher"] == "aapl y180c z100 bb1.05"


def test_token_required_when_configured(tmp_path):
    audio, _ = synth_take([1.0])
    wav = str(tmp_path / "t.wav")
    write_wav(wav, audio)
    agent = Agent(make_cfg(wav, token="s3cret"), NullPlayer(), asr=ScriptedAsr([]))
    api = ApiServer(agent)
    api.start()
    try:
        assert req(api.port, "/health")[0] == 401
        assert req(api.port, "/health", headers={"X-Agent-Token": "s3cret"})[0] == 200
    finally:
        api.stop()
        agent.stop()


def test_hot_reload_changes_buffer_and_adds_role(running, tmp_path):
    agent, api, asr, _ = running
    assert wait_for(lambda: len(asr.calls) == 3)
    before = agent.buffers.get("broker").written
    new = parse({**{"agent": {"port": 0, "allowed_origins": [ORIGIN]}},
                 "buffer": {"seconds": 90},
                 "source": [{"name": "f", "role": "broker", "type": "file",
                             "path": agent.cfg.sources[0].path, "realtime": False}],
                 "replay": {"options": [10, 30], "default_seconds": 30},
                 "asr": {"backend": "none"}})
    agent.apply_config(new)
    assert agent.buffers.get("broker").capacity == 90 * RATE
    assert agent.buffers.get("broker").written >= before          # history carried over
    assert agent.cfg.replay.default_seconds == 30


def test_proposals_expire(running):
    agent, *_ = running
    assert wait_for(lambda: len(agent.proposals.list()) == 2)
    agent.proposals.sweep(now=time.time() + 31)
    assert agent.proposals.list() == []
    assert agent.proposals.stats["expired"] == 2


def test_silence_only_makes_no_segments(tmp_path):
    wav = str(tmp_path / "s.wav")
    write_wav(wav, (np.random.default_rng(1).standard_normal(RATE * 5) * 0.003).astype(np.float32))
    asr = ScriptedAsr(["should never be used"])
    agent = Agent(make_cfg(wav), NullPlayer(), asr=asr)
    agent.start()
    try:
        assert agent.sources["f"].wait_finished(5)
        time.sleep(0.3)
        assert asr.calls == [] and agent.proposals.list() == []
    finally:
        agent.stop()
