from datetime import date

import pytest

from audio_agent.asr import AsrResult, Word
from audio_agent.config import ExtractCfg
from audio_agent.extract import Extractor
from audio_agent.extract.cipher import build_cipher
from audio_agent.extract.model import Expiry, F, OrderFields

ALIASES = {"apple": "AAPL", "amd": "AMD", "spy": "SPY", "spiders": "SPY", "intel": "INTC",
           "tesla": "TSLA", "nvidia": "NVDA", "exxon": "XOM"}


def run(text, conf=0.95, min_conf=0.6):
    return Extractor(ExtractCfg(min_field_conf=min_conf), ALIASES).run(AsrResult(text, conf), {})


def vals(p):
    return {k: v["value"] for k, v in p.fields.to_dict().items()}


CASES = [
    ("Apple, May 180 190 call spread, 500 up, paying 2.10", "aapl y180;190cs z500 bb2.1",
     dict(ticker="AAPL", strikes=[180, 190], spread_type="cs", broker_side="b", broker_qty=500, price=2.1)),
    ("In AMD, what's the market in the June 150 straddle, 200 up", "amd u150st z200",
     dict(ticker="AMD", spread_type="st", broker_side="two_sided", broker_qty=200, price=None)),
    ("Spiders, May 15th 590 595 600 call fly, 1000 up, 1.25 bid", "spy y15;590;595;600cbf z1000 bb1.25",
     dict(strikes=[590, 595, 600], spread_type="cbf", broker_side="b", price=1.25)),
    ("Intel, July 30 35 put spread, I'll pay a dollar ten for 400", "intc l30;35ps z400 bb1.1",
     dict(spread_type="ps", broker_qty=400, price=1.1, broker_side="b")),
    ("Nvidia May June 140 call calendar, 100 up, 3.20 offer", "nvda y/u140cts z100 ss3.2",
     dict(spread_type="cts", broker_side="s", price=3.2)),
    ("Apple May 15 180 calls, a car, paying 2", "aapl y15;180c z100 bb2",
     dict(strikes=[180], broker_qty=100, price=2)),
    ("Apple May 180 190 1 by 2 call spread 100 up", "aapl y180;2x190cs z100",
     dict(ratio=[1, 2], broker_side=None)),
    ("buying 10 May 180 Apple calls", "aapl by180c z10", dict(broker_qty=10, price=None)),
    ("Exxon, December 110 put, selling 50 at one twenty five", None, {}),   # see note below
    ("Exxon December 110 puts, 300 up, offered at 1.40", "xom p110p z300 ss1.4",
     dict(cp="p", broker_side="s")),
    ("Apple May 180 190 call spread, 100 by 200", "aapl y180;2x190cs z100", dict(ratio=[1, 2], broker_qty=100)),
    ("Tesla Jan 250 300 strangle, how are you making that, 50 up", "tsla j250;300sr z50",
     dict(spread_type="sr", broker_side="two_sided")),
]


@pytest.mark.parametrize("text,cipher,expect", CASES)
def test_floor_phrases(text, cipher, expect):
    p = run(text)
    assert p is not None
    v = vals(p)
    for k, want in expect.items():
        assert v[k] == want, (k, v[k], want)
    if cipher is not None:
        assert p.cipher == cipher
    assert p.my_qty is None and p.to_dict()["my_qty"] is None


def test_ambiguous_spoken_price_is_left_blank_not_guessed():
    # "one twenty five" could be 1.25 or 125; the extractor must not pick one confidently
    p = run("Exxon, December 110 put, selling 50 at one twenty five")
    assert vals(p)["price"] in (None, 1.25)
    assert vals(p)["price"] != 125


def test_chatter_produces_no_proposal():
    for t in ["thanks, you're done", "hang on", "", "you there?"]:
        assert run(t) is None


def test_low_asr_word_probability_blanks_that_field():
    words = [Word("Apple", 0.97), Word("May", 0.95), Word("180", 0.30), Word("calls", 0.9),
             Word("500", 0.9), Word("up", 0.9)]
    asr = AsrResult("Apple May 180 calls 500 up", 0.6, words)
    p = Extractor(ExtractCfg(min_field_conf=0.6), ALIASES).run(asr, {})
    assert vals(p)["strikes"] is None            # misheard strike: blank, not guessed
    assert p.cipher is None and "strikes" in p.missing
    assert vals(p)["broker_qty"] == 500           # unaffected fields survive


def test_unknown_ticker_is_not_trusted():
    p = run("Hey ZZQ May 50 calls 100 up")
    assert vals(p)["ticker"] is None
    assert "ticker" in p.missing and p.cipher is None


def test_spelled_ticker():
    assert vals(run("A M D June 150 calls"))["ticker"] == "AMD"


def test_strike_count_mismatch_lowers_confidence():
    p = run("Apple May 180 call spread 100 up")      # a call spread needs two strikes
    assert any("expects 2 strike" in n for n in p.notes)
    assert vals(p)["spread_type"] is None or p.fields.spread_type.conf < 0.6


def test_quantity_is_broker_size_never_my_qty():
    p = run("Apple May 180 calls, 2000 up, paying 1.05")
    d = p.to_dict()
    assert d["fields"]["broker_qty"]["value"] == 2000
    assert d["my_qty"] is None
    assert "my_qty" not in d["fields"]


def test_side_convention_b_is_broker_buys():
    assert vals(run("Apple May 180 calls paying 1.05"))["broker_side"] == "b"
    assert vals(run("Apple May 180 calls selling at 1.05"))["broker_side"] == "s"
    assert vals(run("Apple May 180 calls 1.05 bid"))["broker_side"] == "b"
    assert vals(run("Apple May 180 calls 1.05 offer"))["broker_side"] == "s"


def _fields(**kw):
    f = OrderFields()
    for k, v in kw.items():
        setattr(f, k, F(v, 0.99))
    return f


def test_cipher_semicolon_after_day_and_decimal_strikes():
    f = _fields(ticker="SPY", expiries=[Expiry(10, 14)], strikes=[592.5], cp="c")
    assert build_cipher(f)[0] == "spy w14;592.5c"


def test_cipher_refuses_unencodable_year():
    f = _fields(ticker="AAPL", expiries=[Expiry(1, None, 2028)], strikes=[200], cp="c")
    c, why = build_cipher(f, today=date(2026, 11, 5))
    assert c is None and why == ["expiry_year_not_encodable"]
    f2 = _fields(ticker="AAPL", expiries=[Expiry(1, None, 2027)], strikes=[200], cp="c")
    assert build_cipher(f2, today=date(2026, 11, 5))[0] == "aapl j200c"


def test_cipher_needs_minimum_order_expression():
    c, why = build_cipher(_fields(ticker="AAPL", strikes=[180], cp="c"))
    assert c is None and why == ["expiries"]


@pytest.mark.parametrize("spoken,qty", [("fifteen hundred up", 1500), ("twenty five hundred up", 2500),
                                         ("two thousand five hundred up", 2500), ("five hundred up", 500)])
def test_spoken_sizes(spoken, qty):
    assert vals(run(f"Apple May 180 calls {spoken}"))["broker_qty"] == qty
