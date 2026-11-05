"""Entry point.

  python -m audio_agent --config config.toml           run the agent
  python -m audio_agent --list-devices                 show audio devices (for config)
  python -m audio_agent --config config.toml --check   validate config and exit
  python -m audio_agent --config config.toml --headless   no audio output (replay is a no-op)
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, ConfigWatcher, load


def default_home() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "TPAudioAgent")


def setup_logging(level: str, log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "agent.log"),
                                              maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # pythonw / windowed exe has no stderr
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    home = default_home()
    ap = argparse.ArgumentParser(prog="audio_agent")
    ap.add_argument("--config", default=os.path.join(home, "config.toml"))
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--headless", action="store_true", help="no audio output device")
    ap.add_argument("--log-dir", default=os.path.join(home, "logs"))
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    if a.list_devices:
        from .sources.wasapi import list_devices
        for d in list_devices():
            print(json.dumps(d))
        return 0

    try:
        cfg = load(a.config)
    except (ConfigError, FileNotFoundError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.check:
        print("config OK")
        return 0

    setup_logging(cfg.agent.log_level, a.log_dir)
    log = logging.getLogger("audio_agent")
    log.info("TP Audio Agent %s starting, config %s", __version__, a.config)

    from .agent import Agent
    from .api import ApiServer
    from .playback import NullPlayer, SoundDevicePlayer

    player = NullPlayer() if a.headless else SoundDevicePlayer(cfg.replay.output_device)
    agent = Agent(cfg, player)
    try:
        api = ApiServer(agent)
    except OSError as e:
        log.error("cannot bind %s:%d (%s) — is another agent already running?",
                  cfg.agent.host, cfg.agent.port, e)
        return 3

    def on_change(new_cfg):
        if (new_cfg.agent.host, new_cfg.agent.port) != (agent.cfg.agent.host, agent.cfg.agent.port):
            log.warning("agent.host/port changes need a restart; other changes applied")
        agent.apply_config(new_cfg)

    watcher = ConfigWatcher(a.config, on_change)
    agent.start()
    api.start()
    watcher.start()

    done = threading.Event()

    def _sig(*_):
        done.set()

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)
    try:
        while not done.wait(1.0):
            agent.config_error = watcher.last_error
    finally:
        log.info("shutting down")
        watcher.stop()
        api.stop()
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
