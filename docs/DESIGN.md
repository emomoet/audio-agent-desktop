# TP Audio Agent — design

## Placement

The broker speaks in person beside the trader; the trader is on a wireless headset to the
risk manager through third-party (regulated) voice software. The agent taps the headset's
Windows audio endpoints **in parallel** with that software (WASAPI shared mode) — it is not in
the call path and cannot interrupt or degrade it. The regulated recorder remains the system of
record.

## Components

| Module | Role |
|---|---|
| `sources/` | Capture plugins behind one interface: `wasapi` (mic), `wasapi_loopback` (what the headset plays), `file` (WAV replay for testing), `rtp` (forked media from the voice platform). Each emits mono float32 @ 16 kHz tagged with a **role**. |
| `ringbuffer.py` | One RAM ring buffer per role (`broker`, `trader`, `far_end`), addressed by absolute sample index. |
| `segmenter.py` | Energy endpointing on the `broker` role with a minimum-statistics noise floor. Optional `trader` channel ends a broker turn when the trader speaks. |
| `asr/` | `faster_whisper` (local) or `none`. Prompt-biased with floor vocabulary and configured tickers. Per-word probabilities kept. |
| `extract/` | `rules` (local, deterministic) or `anthropic` (opt-in). Produces `OrderFields` with per-field confidence → threshold → draft cipher. |
| `events.py` | Pub/sub for SSE; in-memory proposal store with TTL. |
| `playback.py` | Replay to the headset earphone (shared mode, mixed with the live call). |
| `agent.py` | Wiring, source watchdog (restart on stall/unplug with backoff), hot reload, health. |
| `api.py` | Localhost HTTP + SSE for the TP UI. |

Everything downstream of a source is keyed on **role**, so changing which microphone is "the
broker" — headset, outward mic, or an RTP fork — is a config edit.

## Safety properties

- No endpoint can price, log, confirm, or send an order. "Load into KOE" fills the input text only.
- `my_qty` is never produced from speech; spoken size is `broker_qty` (Qty).
- `broker_side`: b = broker buys = trader sells (TP's convention, not inverted).
- Low-confidence fields are blanked, not guessed. Field confidence = rule confidence × lowest
  ASR word probability used. A cipher is produced only when ticker, expiry, strike(s) and c/p
  (or a structure that implies it) are all present.
- Ambiguities are surfaced as `notes` on the proposal ("read 15 as expiration day", "call
  spread expects 2 strikes, heard 1").
- Replay suppresses segmentation, so replayed audio leaking into the mic can't become a proposal.

## API (127.0.0.1:8765)

| Method | Path | Body / result |
|---|---|---|
| GET | `/health` | status `ok`/`degraded`, sources, buffers, replay, segmenter, ASR, extract, config error |
| GET | `/config` | effective config, token redacted |
| POST | `/replay` | `{"seconds": 30, "roles": ["broker"]}` or `{"target": "last_segment"}` → `{target, roles, seconds}`; 409 if nothing buffered / bad role / seconds > buffer |
| POST | `/replay/stop` | stops playback |
| GET | `/proposals` | `{"proposals": [Proposal]}` newest first |
| POST | `/proposals/{id}/ack` | `{"action": "loaded" \| "dismissed"}` |
| GET | `/events` | SSE: `hello` (snapshot), `proposal.new`, `proposal.closed`, `segment`, `transcript`, `replay.started`, `replay.stopped`, `health`, `source.restarted`, `config.applied` |

Browser access: `Origin` must be in `agent.allowed_origins`; preflights get
`Access-Control-Allow-Private-Network: true` (Chrome PNA). Optional `X-Agent-Token`
(or `?token=` on `/events`, since EventSource can't set headers).

### Proposal

```json
{
  "id": "3f9c1a2b7d4e", "created_ts": 1793856920.1, "expires_ts": 1793857040.1,
  "segment": {"id": 12, "role": "broker", "duration_s": 3.4, "reason": "silence"},
  "transcript": "Apple, May 180 190 call spread, 500 up, paying 2.10",
  "asr_conf": 0.93,
  "fields": {
    "ticker": {"value": "AAPL", "conf": 0.9, "evidence": "apple"},
    "expiries": {"value": [{"month": 5, "day": null, "year": null}], "conf": 0.9},
    "strikes": {"value": [180, 190], "conf": 0.86},
    "cp": {"value": "c", "conf": 0.9}, "spread_type": {"value": "cs", "conf": 0.9},
    "ratio": {"value": null, "conf": 0}, "broker_side": {"value": "b", "conf": 0.86},
    "broker_qty": {"value": 500, "conf": 0.86}, "price": {"value": 2.1, "conf": 0.86}
  },
  "missing": [], "cipher": "aapl y180;190cs z500 bb2.1", "cipher_status": "draft",
  "extractor": "rules", "my_qty": null, "notes": []
}
```

## TP integration (trading-ui)

1. Copy `ui/useAudioAgent.js` and `ui/AudioAgentPanel.jsx` into `trading-ui/src/`.
2. Mount on the HOE3-3 / KOE order-entry screen:
   ```jsx
   <AudioAgentPanel onLoadCipher={(cipher) => setKoeInput(cipher)} />
   ```
   where `setKoeInput` is whatever already sets the KOE input text (so the backend KOE parser
   sees it exactly as if typed). Do **not** call the Enter/LOG/TRADE handlers.
3. If the agent isn't running, the panel shows "not running" and TP is unaffected.

## Config

`config.example.toml` documents every key. Hot reload: the file is polled every second; a
valid change is applied live (sources restart only if their own settings changed; buffer depth
changes keep history); an invalid file is rejected whole and reported at `/health → config.error`.
`agent.host/port` changes need a restart.

## Phases

| Phase | Scope | State |
|---|---|---|
| 0 | Headset pickup test (`tools/`) | ready to run |
| 1 | Capture + ring buffer + replay + API + UI buttons | built, tested with file source |
| 2 | Segmenter + local ASR, scored against real takes (`score_take.py`) | built; ASR accuracy unmeasured |
| 3 | Draft-cipher proposals + UI panel | built; cipher syntax to verify vs current KOE |
| later | RTP/SIPREC source in production, LLM extractor, outward mic | RTP built, untested live |
