#!/usr/bin/env python3
"""Verify the audio agent's draft KOE ciphers against TP's REAL KOE parser.

Run from inside a trading-system-v2 checkout (or pass its path):

    cd /path/to/trading-system-v2 && python /path/to/audio-agent-desktop/tools/verify_ciphers.py
    python tools/verify_ciphers.py /path/to/trading-system-v2

For every case this feeds TWO ciphers to `backend.order_parser.parse_order` — the entry point
`keystroke_endpoint._order_from_msg` calls for a KOE keystroke (keystroke_endpoint.py:2604) —
and checks the parsed Order against the intended meaning:

  BEFORE  the literal cipher the March-2026 emitter wrote (the task's list, kept as a record)
  AFTER   what `audio_agent.extract.cipher.build_cipher` emits NOW from the same meaning

Checks: symbol; every leg's (month, year, day, strike, cp, ratio); spread type; broker size;
price; broker side (b = broker buys = trader sells, never inverted); no parser errors; priceable.
Nothing here prices, logs or sends an order. Exit status is non-zero if any AFTER case fails.
"""
from __future__ import annotations

import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # audio-agent-desktop
TP = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
BACKEND = TP if os.path.basename(TP) == "backend" else os.path.join(TP, "backend")
if not os.path.exists(os.path.join(BACKEND, "order_parser.py")):
    sys.exit(f"no backend/order_parser.py under {TP!r}: run from inside trading-system-v2 or pass its path")
sys.path.insert(0, BACKEND)
sys.path.insert(0, ROOT)

from order_parser import parse_order                      # noqa: E402  (TP backend)
from audio_agent.extract.cipher import build_cipher       # noqa: E402
from audio_agent.extract.model import Expiry, F, OrderFields  # noqa: E402

TODAY = date.today()

# (legacy cipher, meaning). meaning: symbol, expiries [(month, day)], strikes, cp, spread,
# ratio (per strike, or None), side ('b' | 's' | None), qty, price.
CASES = [
    ("aapl y180;190cs z500 bb2.1",
     dict(symbol="AAPL", expiries=[(5, None)], strikes=[180, 190], cp="c", spread="cs", ratio=None, side="b", qty=500, price=2.10)),
    ("amd u150st z200",
     dict(symbol="AMD", expiries=[(6, None)], strikes=[150], cp=None, spread="st", ratio=None, side=None, qty=200, price=None)),
    ("spy y15;590;595;600cbf z1000 bb1.25",
     dict(symbol="SPY", expiries=[(5, 15)], strikes=[590, 595, 600], cp="c", spread="cbf", ratio=None, side="b", qty=1000, price=1.25)),
    ("intc l30;35ps z400 bb1.1",
     dict(symbol="INTC", expiries=[(7, None)], strikes=[30, 35], cp="p", spread="ps", ratio=None, side="b", qty=400, price=1.10)),
    ("nvda y/u140cts z100 ss3.2",
     dict(symbol="NVDA", expiries=[(5, None), (6, None)], strikes=[140], cp="c", spread="cts", ratio=None, side="s", qty=100, price=3.20)),
    ("aapl y15;180c z100 bb2",
     dict(symbol="AAPL", expiries=[(5, 15)], strikes=[180], cp="c", spread=None, ratio=None, side="b", qty=100, price=2.00)),
    ("aapl y180;2x190cs z100",
     dict(symbol="AAPL", expiries=[(5, None)], strikes=[180, 190], cp="c", spread="cs", ratio=[1, 2], side=None, qty=100, price=None)),
    ("aapl by180c z10",
     dict(symbol="AAPL", expiries=[(5, None)], strikes=[180], cp="c", spread=None, ratio=None, side="b", qty=10, price=None)),
    ("xom p110p z300 ss1.4",
     dict(symbol="XOM", expiries=[(12, None)], strikes=[110], cp="p", spread=None, ratio=None, side="s", qty=300, price=1.40)),
    ("tsla j250;300sr z50",
     dict(symbol="TSLA", expiries=[(1, None)], strikes=[250, 300], cp=None, spread="sr", ratio=None, side=None, qty=50, price=None)),
    ("spy w14;592.5c",
     dict(symbol="SPY", expiries=[(10, 14)], strikes=[592.5], cp="c", spread=None, ratio=None, side=None, qty=None, price=None)),
]


def nearest_year(month: int, today: date = TODAY) -> int:
    """The spoken month with no year means its next occurrence (this month counts as this year)."""
    return today.year if month >= today.month else today.year + 1


def expected_legs(m: dict) -> list[tuple]:
    """(month, year, day, strike, cp, ratio) per leg, derived from the STRUCTURE, not the parser."""
    exps = [(mo, nearest_year(mo), d) for mo, d in m["expiries"]]
    ks, sp, cp = m["strikes"], m["spread"], m["cp"]
    e = exps[0]
    if sp in ("cs", "ps", "cc", "pp"):
        r = m["ratio"] or [1, 1]
        return [(*e, ks[0], cp, r[0]), (*e, ks[1], cp, r[1])]
    if sp in ("cbf", "pbf"):
        return [(*e, ks[0], cp, 1), (*e, ks[1], cp, 2), (*e, ks[2], cp, 1)]
    if sp == "st":
        return [(*e, ks[0], "c", 1), (*e, ks[0], "p", 1)]
    if sp == "sr":
        return [(*e, ks[0], "p", 1), (*e, ks[1], "c", 1)]
    if sp in ("cts", "pts"):
        kk = ks if len(ks) == 2 else [ks[0], ks[0]]
        return [(*exps[0], kk[0], cp, 1), (*exps[1], kk[1], cp, 1)]
    if sp is None:
        return [(*e, ks[0], cp, 1)]
    raise ValueError(f"no structural expectation for {sp!r}")


def leg_key(l) -> tuple:
    x = l.expiration
    return (x.month, x.year, x.day, float(l.strike), l.cp, float(l.raw_ratio))


def check(cipher: str | None, m: dict) -> tuple[bool, list[str], str]:
    """Returns (ok, mismatch reasons, one-line parser output)."""
    if cipher is None:
        return False, ["no cipher"], "-"
    o = parse_order(cipher, TODAY.year)
    legs = list(o.legs)
    got = (f"symbol={o.symbol} bs_global={o.bs_global} price={o.order_price} size={o.order_size} "
           f"type={[g.order_type for g in o.option_info_groups]} priceable={o.is_priceable} "
           f"errors={[str(e) for e in o.errors]} legs={[leg_key(l) + (l.bs,) for l in legs]}")
    why = []
    if o.symbol != m["symbol"]:
        why.append(f"symbol {o.symbol!r} != {m['symbol']!r}")
    want = sorted(expected_legs(m))
    have = sorted(leg_key(l) for l in legs)
    if want != have:
        why.append(f"legs {have} != {want}")
    types = [g.order_type for g in o.option_info_groups]
    if (types[-1] if types else None) != m["spread"]:
        why.append(f"spread {types} != {m['spread']!r}")
    if o.order_size != m["qty"]:
        why.append(f"size {o.order_size} != {m['qty']}")
    if o.order_price != m["price"]:
        why.append(f"price {o.order_price} != {m['price']}")
    side = m["side"]
    if side in ("b", "s"):
        if not (o.bs_global == side or (legs and all(l.bs == side for l in legs))):
            why.append(f"side: bs_global={o.bs_global} leg bs={[l.bs for l in legs]} != {side!r}")
    elif o.bs_global is not None or any(l.bs is not None for l in legs):
        why.append(f"side stated where none was spoken: bs_global={o.bs_global} legs={[l.bs for l in legs]}")
    if o.errors:
        why.append(f"errors {[str(e) for e in o.errors]}")
    if not o.is_priceable:
        why.append("not priceable")
    return not why, why, got


def fields_for(m: dict) -> OrderFields:
    f = OrderFields()
    f.ticker = F(m["symbol"], 0.99)
    f.expiries = F([Expiry(mo, d) for mo, d in m["expiries"]], 0.99)
    f.strikes = F(list(m["strikes"]), 0.99)
    f.cp = F(m["cp"], 0.99)
    f.spread_type = F(m["spread"], 0.99)
    f.ratio = F(m["ratio"], 0.99)
    f.broker_side = F(m["side"] or ("two_sided" if m["spread"] in ("st", "sr") else None), 0.99)
    f.broker_qty = F(m["qty"], 0.99)
    f.price = F(m["price"], 0.99)
    return f


def main() -> int:
    print(f"parser: {os.path.join(BACKEND, 'order_parser.py')}  today={TODAY}")
    failures = 0
    for legacy, m in CASES:
        ok_b, why_b, got_b = check(legacy, m)
        emitted, reasons = build_cipher(fields_for(m), today=TODAY)
        ok_a, why_a, got_a = check(emitted, m)
        failures += not ok_a
        print(f"\n== {legacy}")
        print(f"   BEFORE {'PASS' if ok_b else 'FAIL'}  {legacy}")
        print(f"          parser: {got_b}")
        for w in why_b:
            print(f"          - {w}")
        print(f"   AFTER  {'PASS' if ok_a else 'FAIL'}  {emitted!r}{'  refused: ' + str(reasons) if emitted is None else ''}")
        print(f"          parser: {got_a}")
        for w in why_a:
            print(f"          - {w}")
    print(f"\nAFTER: {len(CASES) - failures}/{len(CASES)} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
