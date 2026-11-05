# Assumptions and decisions (append-only)

Built 2026-11-05 on the instruction "build the whole thing and do it making assumptions".
Each entry is something decided without a ruling. Supersede by appending, don't edit.
Tags: **MEASURED** (verified in this build), **ASSUMED** (needs a ruling or a desk check).

## Hardware / capture

- A1 **ASSUMED** — The headset base is USB to the trader PC and appears as Windows endpoints
  named like "Headset Microphone" / "Headset Earphone". Device matching is by name substring,
  so only `config.toml` changes if the names differ.
- A2 **ASSUMED** — The headset mic can hear the broker well enough. This is exactly what the
  pickup test decides; if it fails, add an outward mic as a second `[[source]]` with
  `role = "broker"` and disable the headset one. No code change.
- A3 **ASSUMED** — Capture in WASAPI *shared* mode only. The agent can never take a device
  away from the voice software; if the voice software holds the mic exclusively, the agent's
  source shows `error` in `/health` and the call is unaffected.
- A4 **ASSUMED** — The risk-manager side (far end) is not needed for order extraction; its
  loopback source exists but is **off by default** because loopback also records our own replays.
- A5 **ASSUMED** — Desk network blocks HuggingFace, so the Whisper model is bundled at build
  time (`build.ps1 -WithModel`) rather than downloaded on the desk.

## Regulatory

- A6 **ASSUMED** — The third-party voice recorder remains the system of record. The agent is a
  parallel listener; it never sits in the call path.
- A7 **ASSUMED (pending counsel)** — Nothing is persisted: audio lives only in the RAM ring
  buffer (90 s default); transcripts and proposals live in memory until loaded/dismissed or
  120 s TTL. Transcripts are not written to logs unless `agent.log_transcripts = true`.
- A8 **ASSUMED** — Cloud processing is off by default. ASR is local (faster-whisper). The
  optional LLM extractor sends transcript *text* (never audio) and is opt-in via config.

## Order extraction

- A9 **ASSUMED** — Speech → *draft KOE cipher*, not a new order schema. TP's backend KOE parser
  stays the sole source of truth and re-validates whatever the trader loads.
- A10 **ASSUMED** — Cipher syntax follows the KOE keystroke logic (month chars `jqmayulgewnp`,
  `;` strike separator / after a day, `z` size, `bb`/`ss`/`r` price, `cs ps cbf pbf ic st sr rr
  cv bx jr cts pts`). **Must be checked against the current `KOE_Order_Entry_Syntax`** — the
  KOE grammar has moved on (Buckets 1–5). Every cipher is labelled `draft`.
- A11 **MEASURED** — Invariants enforced in code and tests: `my_qty` is always null; spoken size
  is `broker_qty` (Qty); `broker_side` uses b = broker buys = trader sells; a field below
  `min_field_conf` (0.6) is blanked, never guessed; no proposal without option content.
- A12 **ASSUMED** — "Load into KOE" only fills the KOE input text. It never presses Enter,
  prices, logs, or sends. Pricing still happens only through TP's normal gated path.
- A13 **ASSUMED** — An explicit year that isn't the nearest occurrence of that month cannot be
  written in cipher, so no cipher is produced (`expiry_year_not_encodable`) rather than one
  that silently means a different year.
- A14 **ASSUMED** — Spoken "one twenty five" after a price word = 1.25 at reduced confidence;
  "125" as digits after "at" is left blank (could be 1.25 or a strike).

## Segmentation

- A15 **MEASURED** (synthetic audio) — Orders are cut on 700 ms of silence, min 300 ms speech,
  max 15 s. Noise floor = 5th percentile over 8 s, so a step up in crowd noise costs at most
  one ~8 s junk segment (which yields no proposal) instead of locking up.
- A16 **ASSUMED** — Gap-free speech longer than ~7 s is rare; past that the floor starts to rise
  under it. Revisit with real takes.

## Integration

- A17 **ASSUMED** — TP UI runs in Chrome at `https://tp.percev.com` (or Vite dev on :5173) and
  calls `http://127.0.0.1:8765` directly. Requires the agent's CORS + Private Network Access
  headers (implemented) and the origin in `agent.allowed_origins`.
- A18 **ASSUMED** — Hotkeys F9 (replay default), Shift+F9 (last order), Esc (stop) don't
  collide with existing TP keys. Disable with `hotkeys={false}`.
- A19 **ASSUMED** — Install per-user without admin (scheduled task at logon, Startup-shortcut
  fallback). Uninstall deletes one folder and the task.

## Not verified in this build

- Real WASAPI capture/playback and loopback (built and type-checked; needs the Windows desk).
- faster-whisper accuracy on floor speech (no speech audio or model download available here).
- PyInstaller build and install scripts (Windows-only).
- RTP source against a real SIPREC fork.

## 2026-11-05 — cipher syntax verified against the live parser

- A20 **MEASURED** — supersedes **A10** and **A13**. The draft cipher is now the syntax TP's KOE
  parser reads TODAY, verified by driving every shape through `parse_order`
  (`trading-system-v2/backend/order_parser.py:812`, the function `keystroke_endpoint._order_from_msg`
  at `keystroke_endpoint.py:2604` calls for a KOE keystroke) with `tools/verify_ciphers.py`:
  11/11 cases parse to the intended legs, size, price and side with no errors and `is_priceable`
  (they were 0/11 under A10's syntax). What changed, with the parser site for each:
  - Month chars are `jfmayulgeonq` (`backend/order_constants.py:9-22`): `q` = December, `o` =
    October, `f` = February. A10's `q`=Feb, `w`=Oct, `p`=Dec no longer parse ("Unrecognised term").
  - A strike is ALWAYS preceded by `;`; digits straight after the month char are the expiration
    DAY (`backend/option_info_parser.py:93-109`, `:169-181`). `y180;190cs` was "May the 180th,
    one strike"; the cipher is `y;180;190cs`.
  - Year = the month char repeated once per year after this calendar year
    (`backend/option_info_parser.py:89-90`: `current_year + len(month_str) - 1`; `CURRENT_YEAR`
    is `date.today().year`, `order_parser.py:37`). `yy` = May next year, `jjj` = Jan two years on.
    There is no "nearest occurrence" rule in the parser, so A13's "a later year can't be written"
    was wrong: the emitter writes it; only a year before this one is refused (`expiry_year_in_past`).
    A month spoken with no year is taken as its next occurrence (this month = this year) — that
    half is the agent's own convention, not the parser's.
  - `bb`/`ss`/`r` prices always carry a decimal point (`backend/order_parser.py:293-366`: one
    number without a `.` is a SIZE — `bb2` read as size 2 and dropped the price).
  - Time spreads are one `/`-separated group per expiry with the code last
    (`nvda y;140c / u;140c cts`, the spelling in `backend/test_parser.py`); A10's `y/u140cts` gave
    an empty first group and the parser still reported ONE leg as priceable.
  - A side with no price is a trailing standalone `b`/`s` (sets `Order.bs_global`; the leading
    prefix form stamps only the first leg of a two-group time spread). Convention unchanged:
    b = broker buys = trader sells (`ANCHOR-DOMAIN-BS`, `backend/order_types.py:54`).
  - No cipher is emitted for `jr` (`SPREAD_EXPECTATIONS["jr"]` is TBD on every axis and
    `y;590jr` errors "Too few C/Ps") or for two expiries on anything but `cts`/`pts`.
  Still ASSUMED: the chain lookup (`backend/expiration_resolver.py:29`) finds the monthly expiry at or
  before the 3rd Friday; a month spoken in its own month after that Friday resolves to nothing and
  the trader sees an unresolved expiry rather than a silently different one.
