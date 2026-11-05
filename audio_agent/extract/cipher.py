"""Draft KOE cipher from extracted fields.

The cipher is a DRAFT for the trader to load into KOE; TP's backend KOE parser stays the
sole source of truth and re-validates it. If a required piece is missing the agent emits
no cipher at all rather than a partial one.

Syntax used (from the KOE keystroke logic; flagged as ASSUMED in ASSUMPTIONS.md until
checked against the current KOE_Order_Entry_Syntax):
    <ticker> <option_info> [z<broker_qty>] [bb<px> | ss<px> | r<px>]
    option_info = [b|s]<exp>[/<exp2>]<strikes><cp><spread>
    exp         = month char (j q m a y u l g e w n p = Jan..Dec) [+ day]
    strikes     = k1[;k2...]   (';' before the first strike when exp ends in a digit)
    ratio legs  = ;<r>x<k>
"""
from __future__ import annotations

from datetime import date

from .model import OrderFields

MONTH_CHARS = "jqmayulgewnp"

# structures that carry a cipher spread code; flies use the first-class cbf/pbf
CIPHER_SPREAD = {"cs": "cs", "ps": "ps", "cbf": "cbf", "pbf": "pbf", "ic": "ic", "st": "st",
                 "sr": "sr", "rr": "rr", "cv": "cv", "bx": "bx", "jr": "jr", "cts": "cts", "pts": "pts"}
# the spread code already states call/put, so no separate c/p character is written
CP_IN_CODE = {"cs", "ps", "cbf", "pbf", "cts", "pts"}


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.4f}".rstrip("0").rstrip(".")


def _px(v) -> str:
    return f"{float(v):.2f}".rstrip("0").rstrip(".")


def build_cipher(f: OrderFields, today: date | None = None) -> tuple[str | None, list[str]]:
    """Returns (cipher or None, reasons it could not be built)."""
    missing = f.required_missing()
    if missing:
        return None, missing

    exps = []
    today = date.today() if today is None else today
    for e in f.expiries.value:
        if e.year is not None:
            nearest = today.year if e.month >= today.month else today.year + 1
            if e.year != nearest:
                # the cipher month char means the nearest such month; a later year can't be written
                return None, ["expiry_year_not_encodable"]
        exps.append(MONTH_CHARS[e.month - 1] + (str(e.day) if e.day else ""))
    exp = "/".join(exps)

    strikes = list(f.strikes.value)
    ratio = f.ratio.value if f.ratio.value and len(f.ratio.value) == len(strikes) else None
    parts = []
    for i, k in enumerate(strikes):
        r = ratio[i] if ratio else 1
        parts.append((f"{_num(r)}x" if ratio and float(r) != 1.0 else "") + _num(k))
    strike_str = ";".join(parts)
    if exp[-1].isdigit() or (ratio and float(ratio[0]) != 1.0):
        strike_str = ";" + strike_str

    st = f.spread_type.value
    cp = f.cp.value
    tail = ""
    if st:
        tail = CIPHER_SPREAD[st]
        if st not in CP_IN_CODE and cp and st not in ("st", "sr", "rr", "cv", "ic", "bx", "jr"):
            tail = cp + tail
    elif cp:
        tail = cp

    side, px = f.broker_side.value, f.price.value
    prefix = side if side in ("b", "s") and px is None else ""
    opt = f"{prefix}{exp}{strike_str}{tail}"

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
    return " ".join(terms), []
