"""Order-proposal data model.

SAFETY INVARIANTS (mirrors TP's own):
- A proposal is never priced or submitted by the agent. It populates the order-entry
  screen only after the trader loads it; every field is editable there.
- broker_qty is the BROKER's size ("Qty"). The trader's own share ("My Qty") is never
  produced from speech — `my_qty` is always None.
- broker_side is the broker's side: "b" = broker buys = trader sells. Never inverted here.
- A field whose confidence is below the threshold is left empty (None) and listed in
  `missing`; it is never guessed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SPREAD_TYPES = {
    "cs": "call spread", "ps": "put spread", "cbf": "call butterfly", "pbf": "put butterfly",
    "ic": "iron condor", "st": "straddle", "sr": "strangle", "rr": "risk reversal",
    "cv": "conversion/reversal", "bx": "box", "jr": "jelly roll",
    "cts": "call time spread", "pts": "put time spread",
}

# How many strikes / expiries each structure needs. None = any.
STRIKES_REQUIRED = {"cs": 2, "ps": 2, "cbf": 3, "pbf": 3, "ic": 4, "st": 1, "sr": 2, "rr": 2,
                    "cv": 1, "bx": 2, "jr": 1, "cts": 1, "pts": 1}
EXPIRIES_REQUIRED = {"cts": 2, "pts": 2, "jr": 2}


@dataclass
class F:
    """A field value with its confidence and the words it came from."""
    value: object = None
    conf: float = 0.0
    evidence: str = ""


@dataclass
class Expiry:
    month: int                      # 1..12
    day: int | None = None          # None = monthly (standard) expiration
    year: int | None = None         # None = nearest such expiration


@dataclass
class OrderFields:
    ticker: F = field(default_factory=F)
    expiries: F = field(default_factory=F)        # list[Expiry]
    strikes: F = field(default_factory=F)         # list[float]
    cp: F = field(default_factory=F)              # "c" | "p" | None (mixed/implied)
    spread_type: F = field(default_factory=F)     # key of SPREAD_TYPES
    ratio: F = field(default_factory=F)           # list[float], leg order
    broker_side: F = field(default_factory=F)     # "b" | "s" | "two_sided"
    broker_qty: F = field(default_factory=F)      # int — broker's full size (Qty)
    price: F = field(default_factory=F)           # float, option price per unit

    NAMES = ("ticker", "expiries", "strikes", "cp", "spread_type", "ratio",
             "broker_side", "broker_qty", "price")

    def apply_threshold(self, min_conf: float) -> list[str]:
        """Blank any field under the confidence threshold. Returns the names blanked."""
        blanked = []
        for n in self.NAMES:
            f: F = getattr(self, n)
            if f.value is not None and f.conf < min_conf:
                setattr(self, n, F(None, f.conf, f.evidence))
                blanked.append(n)
        return blanked

    def required_missing(self) -> list[str]:
        """Fields the order cannot be entered without (KOE minimum order_expression)."""
        miss = [n for n in ("ticker", "expiries", "strikes") if getattr(self, n).value is None]
        st = self.spread_type.value
        cp_implied = st in ("cs", "ps", "cbf", "pbf", "cts", "pts", "st", "sr", "rr", "cv", "ic", "bx", "jr")
        if self.cp.value is None and not cp_implied:
            miss.append("cp")
        return miss

    def to_dict(self) -> dict:
        out = {}
        for n in self.NAMES:
            f: F = getattr(self, n)
            v = f.value
            if n == "expiries" and v is not None:
                v = [asdict(e) for e in v]
            out[n] = {"value": v, "conf": round(f.conf, 3), "evidence": f.evidence}
        return out


@dataclass
class Proposal:
    id: str
    created_ts: float
    expires_ts: float
    segment: dict
    transcript: str
    asr_conf: float
    fields: OrderFields
    missing: list[str]
    cipher: str | None
    cipher_status: str              # draft | incomplete
    extractor: str
    my_qty: None = None             # invariant: never derived from speech
    notes: list[str] = field(default_factory=list)

    def to_dict(self, include_transcript: bool = True) -> dict:
        return {
            "id": self.id, "created_ts": self.created_ts, "expires_ts": self.expires_ts,
            "segment": self.segment,
            "transcript": self.transcript if include_transcript else None,
            "asr_conf": self.asr_conf, "fields": self.fields.to_dict(),
            "missing": self.missing, "cipher": self.cipher, "cipher_status": self.cipher_status,
            "extractor": self.extractor, "my_qty": None, "notes": self.notes,
        }
