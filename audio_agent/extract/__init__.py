"""Transcript -> order Proposal."""
from __future__ import annotations

import logging
import time
import uuid

from ..asr import AsrResult
from ..config import ExtractCfg
from . import llm, rules
from .cipher import build_cipher
from .model import OrderFields, Proposal

log = logging.getLogger(__name__)


def has_order_content(f: OrderFields) -> bool:
    """Chatter ("thanks", "you're done") produces no proposal at all."""
    return any(getattr(f, n).value is not None for n in ("expiries", "strikes", "spread_type"))


class Extractor:
    def __init__(self, cfg: ExtractCfg, aliases: dict[str, str]):
        self.cfg = cfg
        self.aliases = aliases

    def run(self, asr: AsrResult, segment: dict) -> Proposal | None:
        if self.cfg.backend == "none" or not asr.text.strip():
            return None
        used = "rules"
        notes: list[str] = []
        fields: OrderFields | None = None
        if self.cfg.backend == "anthropic":
            try:
                fields, notes = llm.extract(asr.text, asr.confidence, self.cfg.anthropic_model)
                used = "anthropic"
            except Exception as e:
                log.warning("LLM extractor failed (%s); falling back to rules", type(e).__name__)
                notes.append("llm extractor unavailable; rules used")
        if fields is None:
            fields, rnotes = rules.extract(asr, self.aliases, asr.text)
            notes += rnotes
        if not has_order_content(fields):
            return None
        blanked = fields.apply_threshold(self.cfg.min_field_conf)
        if blanked:
            notes.append("low confidence, left blank: " + ", ".join(blanked))
        cipher, missing = build_cipher(fields)
        now = time.time()
        return Proposal(
            id=uuid.uuid4().hex[:12], created_ts=now, expires_ts=now + self.cfg.proposal_ttl_s,
            segment=segment, transcript=asr.text, asr_conf=asr.confidence, fields=fields,
            missing=missing, cipher=cipher, cipher_status="draft" if cipher else "incomplete",
            extractor=used, notes=notes)
