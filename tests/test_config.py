import os
import shutil
import time

import pytest

from audio_agent.config import ConfigError, ConfigWatcher, load


def test_example_config_is_valid(example_config_path):
    cfg = load(example_config_path)
    assert cfg.agent.host == "127.0.0.1"
    assert [s.name for s in cfg.enabled_sources()] == ["headset_mic"]
    assert cfg.symbol_aliases["apple"] == "AAPL"
    assert "token" not in str(cfg.redacted()["agent"].keys() - {"token_set"})


def write(tmp_path, text, bom=False):
    p = tmp_path / "c.toml"
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode())
    return str(p)


BASE = """
[[source]]
name = "m"
role = "broker"
type = "file"
path = "x.wav"
"""


def test_bom_is_accepted(tmp_path):
    assert load(write(tmp_path, BASE, bom=True)).sources[0].name == "m"


@pytest.mark.parametrize("extra,msg", [
    ('[agent]\nhost = "0.0.0.0"\n', "loopback"),
    ('[[source]]\nname = "n"\nrole = "broker"\ntype = "file"\npath = "y.wav"\n', "more than one enabled source"),
    ('[replay]\noptions = [30, 600]\n', "replay.options"),
    ('[asr]\nbackend = "cloud"\n', "asr.backend"),
    ('[agent]\nbogus = 1\n', "unknown keys"),
    ('[[source]]\nname = "w"\nrole = "far_end"\ntype = "wasapi"\n', "device"),
])
def test_rejects_bad_config(tmp_path, extra, msg):
    with pytest.raises(ConfigError, match=msg):
        load(write(tmp_path, BASE + extra))


def test_watcher_applies_valid_and_keeps_previous_on_invalid(tmp_path, example_config_path):
    p = tmp_path / "config.toml"
    shutil.copy(example_config_path, p)
    seen = []
    w = ConfigWatcher(str(p), seen.append)
    text = p.read_text()
    p.write_text(text.replace("seconds = 90", "seconds = 120"))
    os.utime(p, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert w.check_now() and seen[-1].buffer_seconds == 120
    p.write_text(text.replace('port = 8765', 'port = "nope'))
    os.utime(p, ns=(time.time_ns(), time.time_ns() + 2_000_000))
    assert not w.check_now()
    assert w.last_error and len(seen) == 1
