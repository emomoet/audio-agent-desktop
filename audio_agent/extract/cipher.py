"""Draft KOE cipher from extracted fields.

The cipher is a DRAFT for the trader to load into KOE; TP's backend KOE parser stays the
sole source of truth and re-validates it. If a required piece is missing, or the parser
cannot express the order, the agent emits no cipher at all rather than a partial or
approximate one.

Syntax VERIFIED 2026-11-05 against the live parser — `backend/order_parser.py:812`
(`parse_order`), `backend/option_info_parser.py:93` (expiries) and `:169` (strikes),
`backend/order_constants.py:9` (month alphabet), `backend/order_parser.py:293` (bb/ss) — by
`tools/verify_ciphers.py`, which drives every emitted shape through that parser:

    <ticker> <option_info> [z<broker_qty>] [bb<px> | ss<px> | r<px>] [b | s]
    option_info = <exp><strikes><cp-or-spread>
    exp         = month char REPEATED once per year after this one (y = May this calendar
                  year, yy = next year) [+ day digits]; digits right after the month char are
                  ALWAYS the expiration DAY, never a strike
    strikes     = ;k1[;k2...]   (';' before EVERY strike, always — never bare after the month)
    ratio legs  = ;<r>x<k>
    time spread = <exp1>;<k>c / <exp2>;<k>c cts   (one group per expiry; pts for puts)
    price       = bb/ss + a number WITH a decimal point (bb2 is a SIZE of 2; bb2.0 is $2.00)
    side alone  = a trailing standalone b / s term sets the order's broker side
    b = broker buys = trader sells. Never inverted here.
"""
from __future__ import annotations

from datetime import date

from .model import OrderFields

# backend/order_constants.py:9-22 — j f m a y u l g e o n q (q is December, o is October,
# f is February; the March-2026 doc's q=Feb / w=Oct / p=Dec no longer parse).
MONTH_CHARS = "jfmayulgeonq"

# structures that carry a cipher spread code; flies use the first-class cbf/pbf
CIPHER_SPREAD = {"cs": "cs", "ps": "ps", "cbf": "cbf", "pbf": "pbf", "ic": "ic", "st": "st",
                 "sr": "sr", "rr": "rr", "cv": "cv", "bx": "bx", "jr": "jr", "cts": "cts", "pts": "pts"}
# the spread code already states call/put, so no separate c/p character is written
CP_IN_CODE = {"cs", "ps", "cbf", "pbf", "cts", "pts"}
# mixed call/put structures: the parser assigns c/p per leg from the code alone
MIXED_CP = {"st", "sr", "rr", "cv", "ic", "bx", "jr"}
# one '/'-separated group per expiry, the code on the last group
TIME_SPREADS = {"cts": "c", "pts": "p"}
# backend/order_constants.py SPREAD_EXPECTATIONS: jr is 'TBD' on every axis and `y;590jr`
# errors "Too few C/Ps" — the parser cannot read a jelly roll today, so none is emitted.
PARSER_CANNOT_EXPRESS = {"jr"}


def _today() -> date:
    return date.today()


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.4f}".rstrip("0").rstrip(".")


def _px(v) -> str:
    """Price text for bb/ss/r. ALWAYS carries a decimal point: the parser reads `bb2` as a
    SIZE of 2 and only `bb2.0` / `bb2.` as a $2.00 price (order_parser.py:293)."""
    s = f"{float(v):.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _encode_expiry(e, today: date) -> tuple[str | None, str | None]:
    """Month char repeated once per year beyond this calendar year, then the day.
    A month with no spoken year means its next occurrence (this month counts as this year)."""
    year = e.year
    if year is None:
        year = today.year if e.month >= today.month else today.year + 1
    if year < today.year:
        return None, "expiry_year_in_past"
    return MONTH_CHARS[e.month - 1] * (year - today.year + 1) + (str(e.day) if e.day else ""), None


def build_cipher(f: OrderFields, today: date | None = None) -> tuple[str | None, list[str]]:
    """Returns (cipher or None, reasons it could not be built)."""
    missing = f.required_missing()
    if missing:
        return None, missing

    st = f.spread_type.value
    if st in PARSER_CANNOT_EXPRESS:
        return None, [f"spread_type_not_in_parser:{st}"]

    today = _today() if today is None else today
    exps = []
    for e in f.expiries.value:
        code, why = _encode_expiry(e, today)
        if why:
            return None, [why]
        exps.append(code)
    if st in TIME_SPREADS:
        if len(exps) != 2:
            return None, ["time_spread_needs_two_expiries"]
    elif len(exps) != 1:
        return None, ["multi_expiry_not_encodable"]

    strikes = list(f.strikes.value)
    ratio = f.ratio.value if f.ratio.value and len(f.ratio.value) == len(strikes) else None
    parts = []
    for i, k in enumerate(strikes):
        r = ratio[i] if ratio else 1
        parts.append((f"{_num(r)}x" if ratio and float(r) != 1.0 else "") + _num(k))

    cp = f.cp.value
    if st in TIME_SPREADS:
        if len(parts) not in (1, 2):
            return None, ["time_spread_needs_one_or_two_strikes"]
        ks = parts if len(parts) == 2 else [parts[0], parts[0]]
        leg_cp = TIME_SPREADS[st]
        opt = f"{exps[0]};{ks[0]}{leg_cp} / {exps[1]};{ks[1]}{leg_cp} {CIPHER_SPREAD[st]}"
    else:
        tail = ""
        if st:
            tail = CIPHER_SPREAD[st]
            if st not in CP_IN_CODE and st not in MIXED_CP and cp:
                tail = cp + tail
        elif cp:
            tail = cp
        opt = f"{exps[0]};{';'.join(parts)}{tail}"

    side, px = f.broker_side.value, f.price.value
    terms = [f.ticker.value.lower(), opt]
    if f.broker_qty.value is not None:
        terms.append(f"z{int(f.broker_qty.value)}")
    if px is not None:
        if side == "b":
            terms.append(f"bb{_px(px)}")
        elif side == "s":
            terms.append(f"ss{_px(px)}")
        else:
            terms.append(f"r{_px(px)}")
    elif side in ("b", "s"):
        terms.append(side)   # standalone b/s: the order's broker side, every shape alike
    return " ".join(terms), []
