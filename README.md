# TP Audio Agent

A small Windows process on the trader's PC that:

1. **Records** the headset audio into a RAM ring buffer (90 s; nothing written to disk).
2. **Replays** the last 30/60 s — or just the broker's last order — to the headset, from a button in TP.
3. **Hears orders**: cuts the broker's speech into utterances, transcribes locally (Whisper),
   and turns them into **draft KOE ciphers** the trader can load into order entry with one click.

It never prices, logs, or sends anything, and never touches the regulated voice recorder.
See `ASSUMPTIONS.md` for everything decided without a ruling.

```
headset mic ─┐                                   ┌─► replay ─► headset earphone
(outward mic)├─► source ─► ring buffer (per role)┤
loopback/RTP ┘                     │             └─► GET/POST localhost:8765 ◄── TP UI
                                   ▼
                     segmenter ─► Whisper ─► extractor ─► draft cipher proposal ─► SSE ─► TP UI
```

## Layout

| Path | What |
|---|---|
| `audio_agent/` | the agent (Python 3.11+, numpy, sounddevice) |
| `audio_agent/sources/` | `wasapi`, `wasapi_loopback`, `file`, `rtp` capture sources |
| `audio_agent/extract/` | floor-speak → fields → draft cipher (rules; optional LLM) |
| `ui/` | `useAudioAgent.js` hook + `AudioAgentPanel.jsx` for trading-ui |
| `tools/headset_capture_test.py` | the pickup test recorder |
| `tools/score_take.py` | score a take field-by-field against what was read |
| `scripts/` | `build.ps1`, `install.ps1`, `uninstall.ps1` |
| `docs/DESIGN.md` | architecture, API contract, TP integration |
| `docs/DESK_TEST.md` | headset pickup test procedure |

## Run from source (dev)

```
python -m pip install -e ".[dev]"
python -m pytest
python -m audio_agent --list-devices
python -m audio_agent --config config.example.toml --check
python -m audio_agent --config config.toml
```

## Install on the desk

```
REM RUN ON THE WINDOWS DESKTOP (build machine)
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -WithModel
```
Copy `dist\TPAudioAgent\` to the trader PC, then from inside it:
```
REM RUN ON THE WINDOWS DESKTOP
powershell -ExecutionPolicy Bypass -File install.ps1
```
Edit `%LOCALAPPDATA%\TPAudioAgent\config.toml` device names to match
`TPAudioAgent.exe --list-devices`; changes apply live. Remove completely with
`uninstall.ps1` (deletes `%LOCALAPPDATA%\TPAudioAgent` and the logon task — nothing else was changed).

## Health

`http://127.0.0.1:8765/health` — per-source state, resolved device, input level, frame age,
buffer depth, ASR status/latency, proposal counts, config error.
