# 2026-11-05 — cipher verify against the live KOE parser

Checkpoint log. Append-only; the last section is always NEXT ACTION so a fresh session can
resume from this file alone.

## Setup (done)

- `trading-system-v2-SANDBOX` at `30b7cf7` (branch `claude/funny-wright-603voc`), read-only.
- `audio-agent-desktop` at `7bc1d6a` on `main`.
- `pip install -e ".[dev]"` FAILS: setuptools flat-layout discovery sees two top-level
  packages (`ui`, `audio_agent`). Pre-existing packaging defect, not in this task's scope;
  worked around by `pip install numpy sounddevice pytest` and running pytest from the repo root
  (pytest puts the root on `sys.path`). Baseline: `python -m pytest -q` → **69 passed**.


## Step 1 — DISCOVERY (done, read-only; all paths under trading-system-v2-SANDBOX @ 30b7cf7)

### Entry point
- KOE input arrives on the websocket `/ws/keystroke` (`backend/keystroke_endpoint.py:1347-1348`,
  `keystroke_websocket`). A `{"type":"keystroke","expression":"<raw cipher>"}` message is handled at
  `:1537-1549`; the parse call is `order = _order_from_msg(msg, expression)` at `:1549`.
- `_order_from_msg` (`backend/keystroke_endpoint.py:2604-2613`) is one line for this path:
  `return parse_order(expression, CURRENT_YEAR)`.
- **The parser entry point is `parse_order(expression: str, current_year: int = CURRENT_YEAR) -> Order`
  at `backend/order_parser.py:812`.** `CURRENT_YEAR = datetime.date.today().year`
  (`order_parser.py:37`). It splits on whitespace, classifies each term with `classify_term`
  (`backend/term_classifier.py:115`), and parses option-info terms via `parse_option_info`
  (`backend/option_info_parser.py:680`).

### Return shape (`backend/order_types.py:51-88`, `Order`)
- `symbol` (upper-cased), `option_info_groups: list[OptionInfoGroup]` each with `order_type`
  (spread code or None), `bs_global`, and `legs: list[Leg]`; `Leg` = `expiration`
  (`ExpirationInfo(month, year, day, raw, bs)`), `strike`, `cp` ('c'/'p'), `bs` (per-leg broker
  side or None), `raw_ratio`, `reduced_ratio` (`order_types.py:6-27`).
- `bs_global` ('b' = broker buys = trader sells, 's' mirror — `ANCHOR-DOMAIN-BS`, `order_types.py:54`),
  `order_price`, `order_size` (the broker's size, from `z`), `trade_size` (trader's, from `[`),
  `errors: list[ParseError]` (each carries `.blocking`), `is_priceable`
  (`_check_priceable`, `order_parser.py:763`: symbol + every leg has expiry/strike>0/cp + no
  blocking error). `Order.legs` flattens the groups. The Order is ALWAYS returned, never raised.

### Grammar facts that matter to the emitter (each measured by calling `parse_order` directly)
1. **Month alphabet is `jfmayulgeonq`** (`backend/order_constants.py:9-22`): j f m a y u l g e o n q.
   The emitter's `jqmayulgewnp` is stale — `q` is DECEMBER now (was Feb), `o` is OCTOBER (`w` no
   longer parses), `f` is FEBRUARY. `xom p110p` → `Unrecognised term`; `spy w14;592.5c` → same.
2. **A strike is ALWAYS preceded by `;`**, and digits directly after a month char are ALWAYS the
   expiration DAY (`option_info_parser.py:93-109` docstring; `_parse_strike_components` `:169-181`).
   So `y180;190cs` reads May **day 180**, one strike 190 → cs refused ("requires exactly two
   legs"). `u150st` → zero strikes. `by180c` → "Strike must be preceded by ';'". Correct forms:
   `y;180;190cs`, `u;150st`, `by;180c`, `j;250;300sr`.
3. **Year = repeat count of the month char**: `_parse_year` (`option_info_parser.py:89-90`) is
   `current_year + (len(month_str) - 1)` — `y` = May of THIS calendar year, `yy` = next year,
   `yyy` = year after (comment `:151`). There is no "nearest occurrence" rule anywhere in the parser;
   `resolve_expiration` (`backend/expiration_resolver.py:29`) just looks the (year, month[, day]) up in
   the venue list. So an emitted single `y` in November means May 2026 — already expired — and would
   resolve to no expiry. The emitter must double the char for a month earlier than today's, and the
   A13 refusal ("a later year can't be written") is wrong: `jjj;200c` parses as Jan 2028.
4. **`bb<n>` / `ss<n>`: price must carry a decimal point** (`_parse_bs_global_price`,
   `order_parser.py:293-366`): "1 number: has a decimal → price; no decimal → SIZE". `bb2` is
   bs_global=b + `order_size=2` (and it overrode `z100`); `bb2.0` / `bb2.` is price 2.00.
5. **Time spreads**: `y/u140cts` makes TWO groups, the first with NO legs, and the order still reads
   `is_priceable=True` with one leg — a silent half-order. The parser's own tests spell it
   `aapl j;100c / m;120c cts` (`backend/test_parser.py`, 2 uses; also `intc jj;100c / uu;120cts`):
   one group per expiry, `/` term between, trailing `cts`/`pts` → 2 legs, last group typed. The
   single-term `y;u;140cts` (Slice 3 Notation 2a) also gives 2 legs; the test-corpus spelling is used.
6. **Side without price**: `by;180c` → `leg.bs='b'` (single leg); `by;180;190cs` → `bs_global='b'`.
   A trailing standalone `b` also sets `bs_global` (`TermType.BS_GLOBAL`). Prefix form kept.
7. **Ratio** `y;180;2x190cs` → legs raw_ratio 1.0 / 2.0, cs, priceable. Leading `;` always present
   now, so the emitter's special-case for a ratio on the first strike is moot.
8. Spread table `SPREAD_EXPECTATIONS` (`order_constants.py:218`): cs/ps/cts/pts 2 legs; st 1 strike;
   sr 2 strikes; cbf/pbf 3 strikes; ic 4 strikes; bx 2 strikes (4 legs); rr 2; cv 1; **jr is `TBD` on
   every axis** and `y;590jr` errors "Too few C/Ps" — the parser cannot express a jelly roll today.
9. Probed and reading correctly with `;`: `spy y;580;590;600;610ic`, `spy y;580;590bx`,
   `spy y;580;590rr`, `spy y;590cv`, `spy y;590;595;600pbf`, `aapl y;180c r2.5 z100`.

### Raw PASS/FAIL of the task's eleven ciphers (BEFORE, fed verbatim to `parse_order(c, 2026)`)
| cipher | result |
|---|---|
| `aapl y180;190cs z500 bb2.1` | FAIL — day=180, one leg, cs refused |
| `amd u150st z200` | FAIL — zero strikes, st structure mismatch |
| `spy y15;590;595;600cbf z1000 bb1.25` | PASS (year aside: `y` = 2026) |
| `intc l30;35ps z400 bb1.1` | FAIL — day=30, one leg |
| `nvda y/u140cts z100 ss3.2` | FAIL — group 1 has no legs; one-leg order reads priceable |
| `aapl y15;180c z100 bb2` | FAIL — `bb2` is SIZE 2, no price; size 100 overwritten |
| `aapl y180;2x190cs z100` | FAIL — day=180, one leg |
| `aapl by180c z10` | FAIL — "Strike must be preceded by ';'", no leg |
| `xom p110p z300 ss1.4` | FAIL — `p` not a month char: Unrecognised term |
| `tsla j250;300sr z50` | FAIL — day=250, one strike |
| `spy w14;592.5c` | FAIL — `w` not a month char: Unrecognised term |

Year on every row: the emitter writes a single char; in Nov 2026 that is the 2026 occurrence,
which for May/Jun/Jul/Jan has passed — a FAIL on year once the verifier checks it.

## Step 2 — `tools/verify_ciphers.py` (done)

- Run: `cd <trading-system-v2> && python <audio-agent-desktop>/tools/verify_ciphers.py` (or pass the
  TP path as argv[1]). Puts `<tp>/backend` and the agent root on `sys.path`, calls
  `order_parser.parse_order(cipher, today.year)` for the legacy cipher (BEFORE) and for what
  `build_cipher` emits from the same meaning (AFTER); checks symbol, legs (month, year, day, strike,
  cp, ratio — derived from the STRUCTURE, not the parser), spread type, size, price, side, no
  errors, priceable. Expected year = nearest occurrence of the spoken month (this month = this year).
- Result at `144666e` (emitter unchanged): **BEFORE 0/11, AFTER 0/11.** The cbf case parsed
  cleanly but as May 2026 (single `y` in Nov 2026) — the year check is what fails it.

## Step 3 — emitter fixes (done; `audio_agent/extract/cipher.py` rewritten, `tests/test_extract.py` updated)

Each fix, the parser fact it answers, and the verifier result after it (one verifier run covers all —
they were built together; the per-fix attribution below is from the mutation run, which killed
each one separately):

1. **Month alphabet** `MONTH_CHARS = "jfmayulgeonq"` (was `jqmayulgewnp`) — `order_constants.py:9-22`.
   Fixes `xom p110p` (→ `q;110p`) and `spy w14;592.5c` (→ `o14;592.5c`). Mutation back to the old
   string: 3 tests fail.
2. **`;` before every strike, unconditionally** (was: only after a day digit or a leading ratio) —
   `option_info_parser.py:93-109,169-181`. Fixes cs/ps/st/sr/ratio/side-prefix cases. Mutation
   (drop the `;`): 16 tests fail.
3. **Year = month char repeated** once per year past this calendar year; no year spoken ⇒ next
   occurrence of the month (this month counts as this year); year < today ⇒ `None,
   ["expiry_year_in_past"]` — `option_info_parser.py:89-90`. A13's refusal of a later year is gone:
   2028 is `jjj`. Mutation (always one char): 1 test fails.
4. **Price text always has a decimal point** (`bb2.0`, never `bb2`) — `order_parser.py:293-366`
   ("no decimal → SIZE"). Fixes `aapl y15;180c z100 bb2` (which had read size=2, price=None).
   Mutation (strip the point): 2 tests fail.
5. **Time spreads** emitted as `<exp1>;<k>c / <exp2>;<k>c cts` (one group per expiry, the parser's
   own test spelling, `backend/test_parser.py` "aapl j;100c / m;120c cts"); `pts` for puts; a
   second strike makes a diagonal; ≠2 expiries ⇒ `None, ["time_spread_needs_two_expiries"]`.
   Fixes `nvda y/u140cts` (which had read ONE leg and `priceable=True` — the silent one).
6. **Side without price** is a trailing standalone `b`/`s` term (sets `order.bs_global` for every
   shape; the old `b` prefix on the option term stamps only the FIRST leg of a two-group time
   spread). `bb`/`ss` price tokens unchanged. Mutation (`ss` → `bb`): 3 tests fail.
7. **Refusals instead of approximations**: `jr` ⇒ `None, ["spread_type_not_in_parser:jr"]`
   (`SPREAD_EXPECTATIONS["jr"]` is TBD on every axis; `y;590jr` errors "Too few C/Ps"); two
   expiries on a non-time-spread ⇒ `None, ["multi_expiry_not_encodable"]`.

Tests: `tests/test_extract.py` expected ciphers updated to the verified forms; 8 new tests
(month alphabet, `;` without a day, year repetition, past-year refusal, decimal price, standalone
side, time spread, parser-cannot-read refusals); an autouse fixture pins `cipher._today` to
2026-01-02 so the CASES strings do not flip with the calendar. Result: **34 passed** (was 26 in
this file). Five in-place mutations of the emitter each turned the file red (counts above); the file
was restored from a copy, never `git checkout`.

Verifier at this state (run from inside trading-system-v2, today=2026-11-05): **AFTER 11/11 PASS**
(BEFORE still 0/11). Emitted forms: `aapl yy;180;190cs z500 bb2.1` · `amd uu;150st z200` ·
`spy yy15;590;595;600cbf z1000 bb1.25` · `intc ll;30;35ps z400 bb1.1` ·
`nvda yy;140c / uu;140c cts z100 ss3.2` · `aapl yy15;180c z100 bb2.0` · `aapl yy;180;2x190cs z100` ·
`aapl yy;180c z10 b` · `xom q;110p z300 ss1.4` · `tsla jj;250;300sr z50` · `spy oo14;592.5c`
(doubled chars because every month but December has passed in November 2026).

⚠️ Shared assumption between emitter and verifier, stated so it can be challenged: "no year spoken
= next occurrence of the month, this month = this year" is MINE (GAP-FILLED), not the parser's —
the parser only knows repeat-count = years ahead. The verifier derives its expected year by the
same rule, so it cannot catch that rule being wrong; it does catch the emitter disagreeing with it.

## Step 4 — ASSUMPTIONS.md (done) and the one full-suite run

- Appended **A20 MEASURED**, superseding A10 and A13, citing `order_parser.py:812`,
  `keystroke_endpoint.py:2604`, `order_constants.py:9-22`, `option_info_parser.py:89-109,169-181`,
  `order_parser.py:293-366`, `order_types.py:54`, `expiration_resolver.py:29`.
- Full suite (`python -m pytest -q`, repo root): first run **2 failed, 74 passed** —
  `tests/test_agent_api.py:80` and `:173` are STATIC READERS of the cipher string through the whole
  pipeline (the CLAUDE.md "grep the test tree for the moved strings" species; a targeted run could
  not see them). Fixed by pinning `cipher._today` to 2026-01-02 in that file too and updating the two
  strings to `y;180;190cs` / `l;30;35ps` / `y;180c`. Final: **76 passed** (69 at baseline, +8 new
  emitter tests, −1 replaced: `test_cipher_refuses_unencodable_year` is now
  `test_cipher_year_is_the_month_char_repeated` + `test_cipher_refuses_a_past_year`).
- Verifier at the final tree: **AFTER 11/11 PASS** (BEFORE 0/11).
- trading-system-v2-SANDBOX: `git status` clean — nothing changed there.

## DECISIONS (GAP-FILLED unless marked)
- **No-year month = next occurrence, this month = this year** — prompt said nothing about year
  encoding; alternative: refuse any month with no spoken year; reversal CHEAP. Carried into the
  verifier's expectation too (so it cannot catch this rule being wrong).
- **Side without price → trailing standalone `b`/`s`** (not the `by;…` prefix the task's example
  used) — prompt gave the example cipher as a *meaning*; alternative: prefix form, which stamps
  only the first leg of a two-group time spread; reversal CHEAP.
- **Time spread spelling = the parser test corpus's `y;140c / u;140c cts`** over the equally valid
  single-term `y;u;140cts`; reversal CHEAP.
- **`jr` and non-time-spread multi-expiry refused** (`None, [reason]`) per the prompt's "if the
  parser cannot express a case"; alternative: emit `y;590jr` and let the parser error; reversal CHEAP.
- **`pip install -e` packaging defect (two top-level packages) NOT fixed** — out of scope; REFUSED
  to widen; worked around by installing deps directly. Noted here only.
- Pushed to `main` as the prompt instructs, and mirrored to `claude/funny-wright-603voc`.

## NEXT ACTION

None — task complete. If resumed: re-run `tools/verify_ciphers.py` from inside trading-system-v2
after any parser change on their side; the eleven cases and the structural expectations live in it.
