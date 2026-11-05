"""Rule-based floor-speak extractor (local, deterministic, no network).

Turns a transcript like
    "Apple, May 180 190 call spread, 500 up, paying 2.10"
into OrderFields with a confidence per field. Every confidence is the rule's own
confidence multiplied by the lowest ASR word probability among the words it used, so
a misheard word cannot yield a confident field.

It is deliberately conservative: when a reading is ambiguous it lowers confidence
(and the threshold then blanks the field) rather than picking one.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..asr import AsrResult
from .model import EXPIRIES_REQUIRED, STRIKES_REQUIRED, Expiry, F, OrderFields

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# multi-word phrases first (longest match wins); value = (kind, payload)
PHRASES: list[tuple[tuple[str, ...], tuple[str, object]]] = sorted([
    (("call", "spread"), ("SPREAD", ("cs", "c"))), (("put", "spread"), ("SPREAD", ("ps", "p"))),
    (("call", "fly"), ("SPREAD", ("cbf", "c"))), (("put", "fly"), ("SPREAD", ("pbf", "p"))),
    (("call", "butterfly"), ("SPREAD", ("cbf", "c"))), (("put", "butterfly"), ("SPREAD", ("pbf", "p"))),
    (("butterfly",), ("SPREAD", ("bf", None))), (("fly",), ("SPREAD", ("bf", None))),
    (("iron", "condor"), ("SPREAD", ("ic", None))), (("condor",), ("SPREAD", ("ic", None))),
    (("straddle",), ("SPREAD", ("st", None))), (("strangle",), ("SPREAD", ("sr", None))),
    (("risk", "reversal"), ("SPREAD", ("rr", None))), (("risky",), ("SPREAD", ("rr", None))),
    (("conversion",), ("SPREAD", ("cv", None))), (("reversal",), ("SPREAD", ("cv", None))),
    (("box",), ("SPREAD", ("bx", None))), (("jelly", "roll"), ("SPREAD", ("jr", None))),
    (("call", "calendar"), ("SPREAD", ("cts", "c"))), (("put", "calendar"), ("SPREAD", ("pts", "p"))),
    (("call", "time", "spread"), ("SPREAD", ("cts", "c"))), (("put", "time", "spread"), ("SPREAD", ("pts", "p"))),
    (("calendar",), ("SPREAD", ("ts", None))), (("time", "spread"), ("SPREAD", ("ts", None))),
    (("what's", "the", "market"), ("MKT", None)), (("make", "me", "a", "market"), ("MKT", None)),
    (("how", "are", "you", "making"), ("MKT", None)), (("where", "are", "you"), ("MKT", None)),
    (("i'll", "pay"), ("SIDE", "b")), (("i'll", "sell"), ("SIDE", "s")),
], key=lambda p: -len(p[0]))

WORDS: dict[str, tuple[str, object]] = {
    "call": ("CP", "c"), "calls": ("CP", "c"), "put": ("CP", "p"), "puts": ("CP", "p"),
    "buy": ("SIDE", "b"), "buying": ("SIDE", "b"), "buyer": ("SIDE", "b"), "buyers": ("SIDE", "b"),
    "pay": ("SIDE", "b"), "paying": ("SIDE", "b"), "pays": ("SIDE", "b"), "take": ("SIDE", "b"),
    "taking": ("SIDE", "b"), "bid": ("SIDE", "b"), "bidding": ("SIDE", "b"),
    "sell": ("SIDE", "s"), "selling": ("SIDE", "s"), "seller": ("SIDE", "s"), "sellers": ("SIDE", "s"),
    "offer": ("SIDE", "s"), "offered": ("SIDE", "s"), "offering": ("SIDE", "s"),
    "market": ("MKT", None), "making": ("MKT", None),
    "up": ("UP", None), "lots": ("UP", None), "contracts": ("UP", None), "times": ("UP", None),
    "by": ("BY", None), "x": ("BY", None),
    "at": ("AT", None), "@": ("AT", None), "for": ("FOR", None), "size": ("FOR", None),
    "car": ("CAR", None), "cars": ("CAR", None),
}
# side words that, placed right AFTER a price, describe that price ("2.10 bid")
POST_PRICE_SIDE = {"bid": "b", "offer": "s", "offered": "s"}

UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
         "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fifth": 5, "eighth": 8, "ninth": 9, "twelfth": 12,
            "twentieth": 20, "thirtieth": 30}

NUM_RE = re.compile(r"^\$?(\d+(?:\.\d+)?)(st|nd|rd|th|s)?$")


@dataclass
class Tok:
    kind: str
    val: object
    text: str
    prob: float
    i: int = 0
    used: bool = False
    dec: bool = False
    ordinal: bool = False
    money: bool = False


def _split_raw(text: str) -> list[str]:
    t = text.lower().replace("’", "'")
    t = re.sub(r"(\d)\s*x\s*(\d)", r"\1 x \2", t)          # 1x2 -> 1 x 2
    t = re.sub(r"(\d)/(\d)", r"\1 \2", t)                     # 180/190 -> 180 190
    t = re.sub(r"(\d)-(?=up\b)", r"\1 ", t)                   # 500-up
    t = t.replace("-", " ")
    t = re.sub(r"[,;:!?\"()]", " ", t)
    t = re.sub(r"\.(\s|$)", r" ", t)                          # sentence periods, not decimals
    return t.split()


def _words_with_probs(asr: AsrResult) -> list[tuple[str, float]]:
    if asr.words:
        out = []
        for w in asr.words:
            for piece in _split_raw(w.text):
                out.append((piece, w.prob))
        return out
    return [(w, asr.confidence if asr.confidence > 0 else 1.0) for w in _split_raw(asr.text)]


def _parse_number_words(words: list[tuple[str, float]], i: int):
    """Spoken number at words[i]. Returns (value, is_decimal, next_i, minprob) or None."""
    w, p = words[i]
    if w in ORDINALS:
        return None
    # strict English composition: "twenty five", "five hundred", "two thousand", but
    # "one twenty five" is two numbers (1, 25) — never summed into 26.
    total, cur, j, used, minp = 0.0, 0.0, i, False, 1.0
    last = None                       # "unit" | "tens" | "scale"
    while j < len(words):
        wj, pj = words[j]
        if wj in UNITS and last in (None, "tens", "scale") and not (last == "tens" and UNITS[wj] >= 10):
            cur += UNITS[wj]
            last = "unit"
        elif wj in TENS and last in (None, "scale"):
            cur += TENS[wj]
            last = "tens"
        elif wj == "hundred" and last in ("unit", "tens") and 0 < cur < 100:   # "fifteen hundred" = 1500
            cur *= 100
            last = "scale"
        elif wj == "thousand" and used and last in ("unit", "tens", "scale"):
            total += cur * 1000
            cur = 0
            last = "scale"
        elif wj == "a" and last is None and j + 1 < len(words) and words[j + 1][0] in ("hundred", "thousand"):
            cur, last = 1, "unit"
        else:
            break
        used, minp, j = True, min(minp, pj), j + 1
    if not used:
        return None
    value, dec = total + cur, False
    # "two point one five" / "two and a half"
    if j + 1 < len(words) and words[j][0] == "point":
        digits, k = "", j + 1
        while k < len(words) and words[k][0] in UNITS and UNITS[words[k][0]] < 10:
            digits += str(UNITS[words[k][0]])
            minp = min(minp, words[k][1])
            k += 1
        if digits:
            value, dec, j = float(f"{int(value)}.{digits}"), True, k
    elif j + 2 < len(words) and words[j][0] == "and" and words[j + 1][0] == "a" and words[j + 2][0] == "half":
        value, dec, j = value + 0.5, True, j + 3
    return value, dec, j, minp


def tokenize(asr: AsrResult, aliases: dict[str, str]) -> list[Tok]:
    words = _words_with_probs(asr)
    known_tickers = set(aliases.values())
    toks: list[Tok] = []
    i = 0
    while i < len(words):
        w, p = words[i]
        # multi-word phrases
        hit = None
        for phrase, (kind, val) in PHRASES:
            n = len(phrase)
            if tuple(x for x, _ in words[i:i + n]) == phrase:
                hit = (kind, val, n)
                break
        if hit:
            kind, val, n = hit
            toks.append(Tok(kind, val, " ".join(x for x, _ in words[i:i + n]),
                            min(q for _, q in words[i:i + n])))
            i += n
            continue
        m = NUM_RE.match(w)
        if m:
            v = float(m.group(1))
            toks.append(Tok("NUM", v, w, p, dec="." in m.group(1),
                            ordinal=m.group(2) in ("st", "nd", "rd", "th"), money=w.startswith("$")))
            i += 1
            continue
        if w in ORDINALS:
            toks.append(Tok("NUM", float(ORDINALS[w]), w, p, ordinal=True))
            i += 1
            continue
        # "a dollar ten" / "two dollars" / "ten cents"
        if w in ("dollar", "dollars", "buck", "bucks"):
            base = 1.0
            if toks and toks[-1].kind == "NUM" and not toks[-1].used:
                base = toks.pop().val
            elif toks and toks[-1].text == "a":
                toks.pop()
            cents, j, minp = 0.0, i + 1, p
            if j < len(words) and words[j][0] == "and":
                j += 1
            nw = _parse_number_words(words, j) if j < len(words) else None
            if nw and nw[0] < 100:
                cents, j, minp = nw[0], nw[2], min(p, nw[3])
            elif j < len(words) and NUM_RE.match(words[j][0]) and float(NUM_RE.match(words[j][0]).group(1)) < 100:
                cents, minp, j = float(NUM_RE.match(words[j][0]).group(1)), min(p, words[j][1]), j + 1
            else:
                j = i + 1
            toks.append(Tok("NUM", round(base + cents / 100, 2), "dollar", minp, dec=True, money=True))
            i = j
            continue
        if w in ("cent", "cents") and toks and toks[-1].kind == "NUM":
            t = toks[-1]
            t.val, t.dec, t.money = round(t.val / 100, 2), True, True
            i += 1
            continue
        nw = _parse_number_words(words, i)
        if nw:
            v, dec, j, minp = nw
            toks.append(Tok("NUM", v, " ".join(x for x, _ in words[i:j]), minp, dec=dec))
            i = j
            continue
        if w in MONTHS:
            toks.append(Tok("MONTH", MONTHS[w], w, p))
        elif w in WORDS:
            kind, val = WORDS[w]
            toks.append(Tok(kind, val, w, p))
        elif w in aliases:
            toks.append(Tok("TICKER", aliases[w], w, p))
        elif w.upper() in known_tickers:
            toks.append(Tok("TICKER", w.upper(), w, p))
        else:
            toks.append(Tok("WORD", w, w, p))
        i += 1
    # spelled-out tickers: "a a p l"
    out: list[Tok] = []
    i = 0
    while i < len(toks):
        if toks[i].kind == "WORD" and len(toks[i].text) == 1 and toks[i].text.isalpha():
            j = i
            while j < len(toks) and toks[j].kind == "WORD" and len(toks[j].text) == 1 and toks[j].text.isalpha():
                j += 1
            sym = "".join(t.text for t in toks[i:j]).upper()
            if 2 <= len(sym) <= 5 and sym in known_tickers:
                out.append(Tok("TICKER", sym, " ".join(t.text for t in toks[i:j]),
                               min(t.prob for t in toks[i:j])))
                i = j
                continue
        out.append(toks[i])
        i += 1
    for k, t in enumerate(out):
        t.i = k
    return out


def _f(value, rule_conf: float, toks: list[Tok]) -> F:
    probs = [t.prob for t in toks] or [1.0]
    return F(value, round(rule_conf * min(probs), 3), " ".join(t.text for t in toks))


def _fmt_num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


def extract(asr: AsrResult, aliases: dict[str, str], raw_text_original: str | None = None) -> tuple[OrderFields, list[str]]:
    toks = tokenize(asr, aliases)
    fields = OrderFields()
    notes: list[str] = []
    n = len(toks)

    def at(k):
        return toks[k] if 0 <= k < n else None

    # ---- ticker ------------------------------------------------------------
    tick = [t for t in toks if t.kind == "TICKER"]
    if tick:
        syms = {t.val for t in tick}
        conf = 0.95 if len(syms) == 1 else 0.3
        if len(syms) > 1:
            notes.append(f"multiple tickers heard: {sorted(syms)}")
        fields.ticker = _f(tick[0].val, conf, [tick[0]])
        for t in tick:
            t.used = True
    elif raw_text_original:
        cands = [w for w in re.findall(r"\b[A-Z]{2,5}\b", raw_text_original) if w.lower() not in MONTHS]
        if cands:
            # an unknown capitalised token is a weak guess: below the default threshold by design
            fields.ticker = F(cands[0], 0.5, cands[0])
            notes.append(f"ticker {cands[0]!r} not in symbol list")

    # ---- ratio: NUM BY NUM [BY NUM ...] -----------------------------------
    for k in range(n):
        if toks[k].kind == "NUM" and at(k + 1) and at(k + 1).kind == "BY" and at(k + 2) and at(k + 2).kind == "NUM":
            seq, j = [toks[k]], k
            while at(j + 1) and at(j + 1).kind == "BY" and at(j + 2) and at(j + 2).kind == "NUM":
                seq.append(toks[j + 2])
                j += 2
            vals = [t.val for t in seq]
            g = vals[0]
            for v in vals[1:]:
                g = math.gcd(int(g), int(v)) if all(float(x).is_integer() for x in (g, v)) else 1
            g = g or 1
            if g > 1 and min(vals) >= 10:
                # "100 by 200": ratio 1x2 and the size (KOE semantics)
                fields.ratio = _f([_fmt_num(v / g) for v in vals], 0.9, seq)
                fields.broker_qty = _f(int(vals[0]), 0.85, seq)
            else:
                fields.ratio = _f([_fmt_num(v) for v in vals], 0.9, seq)
            for t in seq:
                t.used = True
            for t in toks[k:j + 3]:
                if t.kind == "BY":
                    t.used = True
            break

    # ---- quantity ----------------------------------------------------------
    for k, t in enumerate(toks):
        if t.kind == "NUM" and not t.used and not t.dec and at(k + 1) and at(k + 1).kind == "UP":
            fields.broker_qty = _f(int(t.val), 0.9, [t, at(k + 1)])
            t.used = at(k + 1).used = True
            break
        if t.kind == "CAR":
            mult = toks[k - 1] if k and toks[k - 1].kind == "NUM" and not toks[k - 1].used else None
            q = int((mult.val if mult else 1) * 100)
            fields.broker_qty = _f(q, 0.85, [x for x in (mult, t) if x])
            t.used = True
            if mult:
                mult.used = True
            break

    # ---- spoken "one twenty five" after a price word -> 1.25 -------------
    for k in range(1, n - 1):
        a_, b_, prev = toks[k], toks[k + 1], toks[k - 1]
        if (prev.kind == "AT" or prev.text in ("pay", "paying", "bid", "offer", "offered")) \
                and a_.kind == "NUM" and b_.kind == "NUM" and not a_.used and not b_.used \
                and not a_.text[0].isdigit() and not b_.text[0].isdigit() \
                and 1 <= a_.val <= 9 and 10 <= b_.val <= 99 and not a_.dec and not b_.dec:
            merged = Tok("NUM", round(a_.val + b_.val / 100, 2), f"{a_.text} {b_.text}",
                         min(a_.prob, b_.prob) * 0.8, dec=True, money=True)
            toks[k:k + 2] = [merged]
            for idx, t in enumerate(toks):
                t.i = idx
            n = len(toks)
            break

    # ---- price -------------------------------------------------------------
    price_tok = None
    for k, t in enumerate(toks):
        if t.kind != "NUM" or t.used:
            continue
        prev = at(k - 1)
        nxt = at(k + 1)
        priceish = t.dec or t.money
        if not priceish:
            # a bare integer is a price only straight after "at"/"pay"/"bid"/"offer" and
            # not followed by a month ("buying 10 May 180 calls" is a size)
            priceish = (prev is not None and t.val < 20
                        and (prev.kind == "AT" or prev.text in ("pay", "paying", "pays", "bid", "offer", "offered"))
                        and not (nxt and nxt.kind in ("MONTH", "UP", "CAR")))
        if prev and prev.kind in ("AT", "SIDE", "FOR") and priceish:
            conf = 0.9 if (t.dec or t.money) else 0.7
            fields.price = _f(_fmt_num(t.val), conf, [prev, t])
            price_tok = t
            if prev.kind == "SIDE":
                fields.broker_side = _f(prev.val, 0.9, [prev])
                prev.used = True
            prev.used = t.used = True
            break
        if nxt and nxt.kind == "SIDE" and nxt.text in POST_PRICE_SIDE and (t.dec or t.money):
            fields.price = _f(_fmt_num(t.val), 0.9, [t, nxt])
            fields.broker_side = _f(POST_PRICE_SIDE[nxt.text], 0.9, [nxt])
            price_tok = t
            t.used = nxt.used = True
            break
    if price_tok is None:
        decs = [t for t in toks if t.kind == "NUM" and not t.used and (t.dec or t.money)]
        if len(decs) == 1:
            fields.price = _f(_fmt_num(decs[0].val), 0.6, [decs[0]])
            decs[0].used = True
            notes.append("price inferred from a lone decimal")

    # ---- size after FOR ("for 200") ----------------------------------------
    if fields.broker_qty.value is None:
        for k, t in enumerate(toks):
            if t.kind == "FOR" and at(k + 1) and at(k + 1).kind == "NUM" and not at(k + 1).used and not at(k + 1).dec:
                fields.broker_qty = _f(int(at(k + 1).val), 0.8, [t, at(k + 1)])
                t.used = at(k + 1).used = True
                break

    # ---- size straight after a side word ("selling 300 Tesla ...") ---------
    if fields.broker_qty.value is None:
        for k, t in enumerate(toks):
            nx = at(k + 1)
            if t.kind == "SIDE" and nx and nx.kind == "NUM" and not nx.used and not nx.dec \
                    and nx.val >= 1 and not (at(k + 2) and at(k + 2).kind == "MONTH"):
                fields.broker_qty = _f(int(nx.val), 0.8, [t, nx])
                nx.used = True
                break

    # ---- side --------------------------------------------------------------
    if fields.broker_side.value is None:
        sides = [t for t in toks if t.kind == "SIDE" and not t.used]
        mkt = [t for t in toks if t.kind == "MKT"]
        vals = {t.val for t in sides}
        if len(vals) == 1:
            fields.broker_side = _f(sides[0].val, 0.9 if not mkt else 0.5, sides[:1])
        elif len(vals) > 1:
            fields.broker_side = _f("two_sided", 0.6, sides)
            notes.append("both buy and sell words heard")
        elif mkt:
            fields.broker_side = _f("two_sided", 0.9, mkt[:1])

    # ---- expiries ----------------------------------------------------------
    months = [t for t in toks if t.kind == "MONTH"]
    expiries: list[Expiry] = []
    exp_toks: list[Tok] = []
    last_exp_i = -1
    for mt in months[:2]:
        e = Expiry(mt.val)
        used = [mt]
        nx = at(mt.i + 1)
        if nx and nx.kind == "NUM" and not nx.used and nx.ordinal and 1 <= nx.val <= 31:
            e.day = int(nx.val)
            used.append(nx)
        elif nx and nx.kind == "NUM" and 2026 <= nx.val <= 2040:
            e.year = int(nx.val)
            used.append(nx)
        for u in used:
            u.used = True
        expiries.append(e)
        exp_toks += used
        last_exp_i = max(last_exp_i, used[-1].i)
    if len(months) > 2:
        notes.append("more than two expirations heard")

    # ---- spread type & cp --------------------------------------------------
    spreads = [t for t in toks if t.kind == "SPREAD"]
    cps = [t for t in toks if t.kind == "CP"]
    st_tok = spreads[0] if spreads else None
    st, st_cp = (st_tok.val if st_tok else (None, None))
    cp_vals = {t.val for t in cps} | ({st_cp} if st_cp else set())
    if st in ("bf", "ts"):
        if len(cp_vals) == 1:
            c = next(iter(cp_vals))
            st = {"bf": "cbf" if c == "c" else "pbf", "ts": "cts" if c == "c" else "pts"}[st]
        else:
            notes.append("fly/calendar without a single call/put")
            st = None
    if st:
        fields.spread_type = _f(st, 0.95 if len(spreads) == 1 else 0.4, [st_tok])
    if len(cp_vals) == 1:
        fields.cp = _f(next(iter(cp_vals)), 0.95, cps[:1] or [st_tok])
    elif len(cp_vals) > 1 and st not in ("st", "sr", "rr", "cv", "ic", "bx", "jr"):
        notes.append("both calls and puts heard for a single-type structure")
    for t in cps + spreads:
        t.used = True

    # ---- strikes -----------------------------------------------------------
    stop = {"CP", "SPREAD", "SIDE", "MKT", "UP", "AT", "FOR", "TICKER", "MONTH", "CAR"}
    strike_toks: list[Tok] = []
    if last_exp_i >= 0:
        k = last_exp_i + 1
        while k < n and toks[k].kind not in stop:
            if toks[k].kind == "NUM" and not toks[k].used:
                strike_toks.append(toks[k])
            k += 1
    if not strike_toks:
        # "the 180 190 call spread in May": numbers just before the cp/structure word
        anchor = (cps or spreads or [None])[0]
        if anchor is not None:
            k = anchor.i - 1
            while k >= 0 and toks[k].kind in ("NUM", "WORD") and toks[k].kind != "MONTH":
                if toks[k].kind == "NUM" and not toks[k].used:
                    strike_toks.insert(0, toks[k])
                elif toks[k].kind == "WORD" and toks[k].text not in ("the", "and", "in"):
                    break
                k -= 1
    # a day without "th": "May 15 180 190 call spread" — first number is a day iff the
    # structure needs one fewer strike than we have
    if expiries and expiries[0].day is None and strike_toks and not strike_toks[0].dec:
        need = STRIKES_REQUIRED.get(fields.spread_type.value or "", 1 if fields.spread_type.value is None else None)
        first = strike_toks[0]
        if need and len(strike_toks) == need + 1 and 1 <= first.val <= 31 and first.i == last_exp_i + 1:
            expiries[0].day = int(first.val)
            exp_toks.append(first)
            strike_toks = strike_toks[1:]
            notes.append(f"read {int(first.val)} as expiration day")
    # qty right before the first month: "buying 500 May 180 calls"
    if fields.broker_qty.value is None and months:
        prev = at(months[0].i - 1)
        if prev and prev.kind == "NUM" and not prev.used and not prev.dec and prev not in strike_toks:
            fields.broker_qty = _f(int(prev.val), 0.75, [prev])
            prev.used = True
    for t in strike_toks:
        t.used = True

    if expiries:
        conf = 0.95
        if any(t.text == "may" for t in exp_toks) and not strike_toks:
            conf = 0.5  # "may" without strikes is probably the verb
        fields.expiries = _f(expiries, conf, exp_toks)
    if strike_toks:
        fields.strikes = _f([_fmt_num(t.val) for t in strike_toks], 0.9, strike_toks)

    # ---- structural consistency -------------------------------------------
    stv = fields.spread_type.value
    if stv:
        need_k = STRIKES_REQUIRED.get(stv)
        have_k = len(fields.strikes.value or [])
        if need_k and have_k and have_k != need_k:
            notes.append(f"{stv} expects {need_k} strike(s), heard {have_k}")
            fields.spread_type.conf = round(fields.spread_type.conf * 0.5, 3)
            fields.strikes.conf = round(fields.strikes.conf * 0.7, 3)
        need_e = EXPIRIES_REQUIRED.get(stv, 1)
        have_e = len(expiries)
        if have_e and have_e != need_e:
            notes.append(f"{stv} expects {need_e} expiration(s), heard {have_e}")
            fields.spread_type.conf = round(fields.spread_type.conf * 0.6, 3)
    elif len(fields.strikes.value or []) > 1 or len(expiries) > 1:
        notes.append("multiple strikes/expirations but no structure named")
        if fields.strikes.value:
            fields.strikes.conf = round(fields.strikes.conf * 0.8, 3)
    if fields.ratio.value and fields.strikes.value and len(fields.ratio.value) != len(fields.strikes.value) \
            and len(fields.ratio.value) != len(expiries):
        notes.append("ratio legs don't match strikes")
        fields.ratio.conf = round(fields.ratio.conf * 0.5, 3)

    return fields, notes
