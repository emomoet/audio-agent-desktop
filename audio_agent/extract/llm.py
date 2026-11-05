"""Optional LLM extractor (Anthropic Messages API, stdlib HTTP).

Off by default: it sends the transcript TEXT (never audio) off the desk. Enable only
after compliance signs off. Its output goes through exactly the same validation,
threshold and cipher path as the rule extractor; on any failure the agent falls back
to the rule extractor for that segment.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from .model import SPREAD_TYPES, Expiry, F, OrderFields

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = f"""You convert a floor options broker's spoken order (an ASR transcript) into JSON.
Return ONLY a JSON object with keys: ticker, expiries, strikes, cp, spread_type, ratio,
broker_side, broker_qty, price, notes. Each order key maps to {{"value": ..., "conf": 0..1}}.
- expiries.value: list of {{"month": 1-12, "day": int|null, "year": int|null}}
- strikes.value: list of numbers in leg order; ratio.value: list of numbers or null
- cp.value: "c", "p" or null; spread_type.value: one of {sorted(SPREAD_TYPES)} or null
- broker_side.value: "b" (broker buys), "s" (broker sells), "two_sided" (asks for a market) or null
- broker_qty.value: the broker's total size as an integer, or null
- price.value: per-unit option price as a number, or null
Never invent a value that was not said: use null with conf 0. Lower conf when the
transcript is ambiguous or looks misheard. notes: list of short strings."""


def _coerce(fields_json: dict) -> tuple[OrderFields, list[str]]:
    f = OrderFields()

    def get(k):
        v = fields_json.get(k) or {}
        return v.get("value"), float(v.get("conf") or 0.0)

    v, c = get("ticker")
    if isinstance(v, str) and 1 <= len(v) <= 6 and v.isalnum():
        f.ticker = F(v.upper(), c, "llm")
    v, c = get("expiries")
    if isinstance(v, list) and v:
        try:
            exps = [Expiry(int(e["month"]), e.get("day") and int(e["day"]), e.get("year") and int(e["year"]))
                    for e in v[:2]]
            if all(1 <= e.month <= 12 and (e.day is None or 1 <= e.day <= 31) for e in exps):
                f.expiries = F(exps, c, "llm")
        except (KeyError, TypeError, ValueError):
            pass
    v, c = get("strikes")
    if isinstance(v, list) and v and all(isinstance(x, (int, float)) and x > 0 for x in v):
        f.strikes = F(list(v), c, "llm")
    v, c = get("cp")
    if v in ("c", "p"):
        f.cp = F(v, c, "llm")
    v, c = get("spread_type")
    if v in SPREAD_TYPES:
        f.spread_type = F(v, c, "llm")
    v, c = get("ratio")
    if isinstance(v, list) and v and all(isinstance(x, (int, float)) and x > 0 for x in v):
        f.ratio = F(list(v), c, "llm")
    v, c = get("broker_side")
    if v in ("b", "s", "two_sided"):
        f.broker_side = F(v, c, "llm")
    v, c = get("broker_qty")
    if isinstance(v, (int, float)) and float(v).is_integer() and v > 0:
        f.broker_qty = F(int(v), c, "llm")
    v, c = get("price")
    if isinstance(v, (int, float)) and 0 < v < 10000:
        f.price = F(v, c, "llm")
    notes = [str(n) for n in (fields_json.get("notes") or [])][:5]
    return f, notes


def extract(transcript: str, asr_conf: float, model: str, timeout_s: float = 4.0) -> tuple[OrderFields, list[str]]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model": model, "max_tokens": 600, "system": SYSTEM,
        "messages": [{"role": "user", "content": f"Transcript: {transcript}"}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        resp = json.loads(r.read())
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    fields, notes = _coerce(json.loads(text[start:end + 1]))
    # the model never sees audio: cap every field by the recogniser's own confidence
    for name in OrderFields.NAMES:
        fld = getattr(fields, name)
        fld.conf = round(min(fld.conf, max(asr_conf, 0.0) or 1.0), 3)
    return fields, notes
