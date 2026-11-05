from datetime import date

import pytest

from audio_agent.asr import AsrResult, Word
from audio_agent.config import ExtractCfg
from audio_agent.extract import Extractor
from audio_agent.extract import cipher as cipher_mod
from audio_agent.extract.cipher import MONTH_CHARS, build_cipher
from audio_agent.extract.model import Expiry, F, OrderFields

ALIASES = {"apple": "AAPL", "amd": "AMD", "spy": "SPY", "spiders": "SPY", "intel": "INTC",
           "tesla": "TSLA", "nvidia": "NVDA", "exxon": "XOM"}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """The cipher's year is the month char's repeat count relative to TODAY (parser rule), so
    the expected strings below are pinned to 2 Jan 2026: every month is 'this year', one char."""
    monkeypatch.setattr(cipher_mod, "_today", lambda: date(2026, 1, 2))


def run(text, conf=0.95, min_conf=0.6):
    return Extractor(ExtractCfg(min_field_conf=min_conf), ALIASES).run(AsrResult(text, conf), {})


def vals(p):
    return {k: v["value"] for k, v in p.fields.to_dict().items()}


CASES = [
    ("Apple, May 180 190 call spread, 500 up, paying 2.10", "aapl y;180;190cs z500 bb2.1",
     dict(ticker="AAPL", strikes=[180, 190], spread_type="cs", broker_side="b", broker_qty=500, price=2.1)),
    ("In AMD, what's the market in the June 150 straddle, 200 up", "amd u;150st z200",
     dict(ticker="AMD", spread_type="st", broker_side="two_sided", broker_qty=200, price=None)),
    ("Spiders, May 15th 590 595 600 call fly, 1000 up, 1.25 bid", "spy y15;590;595;600cbf z1000 bb1.25",
     dict(strikes=[590, 595, 600], spread_type="cbf", broker_side="b", price=1.25)),
    ("Intel, July 30 35 put spread, I'll pay a dollar ten for 400", "intc l;30;35ps z400 bb1.1",
     dict(spread_type="ps", broker_qty=400, price=1.1, broker_side="b")),
    ("Nvidia May June 140 call calendar, 100 up, 3.20 offer", "nvda y;140c / u;140c cts z100 ss3.2",
     dict(spread_type="cts", broker_side="s", price=3.2)),
    ("Apple May 15 180 calls, a car, paying 2", "aapl y15;180c z100 bb2.0",
     dict(strikes=[180], broker_qty=100, price=2)),
    ("Apple May 180 190 1 by 2 call spread 100 up", "aapl y;180;2x190cs z100",
     dict(ratio=[1, 2], broker_side=None)),
    ("buying 10 May 180 Apple calls", "aapl y;180c z10 b", dict(broker_qty=10, price=None)),
    ("Exxon, December 110 put, selling 50 at one twenty five", None, {}),   # see note below
    ("Exxon December 110 puts, 300 up, offered at 1.40", "xom q;110p z300 ss1.4",
     dict(cp="p", broker_side="s")),
    ("Apple May 180 190 call spread, 100 by 200", "aapl y;180;2x190cs z100", dict(ratio=[1, 2], broker_qty=100)),
    ("Tesla Jan 250 300 strangle, how are you making that, 50 up", "tsla j;250;300sr z50",
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
    assert build_cipher(f)[0] == "spy o14;592.5c"


def test_cipher_semicolon_before_every_strike_even_without_a_day():
    # parser: digits right after the month char are the DAY, so 'y180' is May-180th, not a strike
    f = _fields(ticker="AAPL", expiries=[Expiry(5)], strikes=[180], cp="c")
    assert build_cipher(f)[0] == "aapl y;180c"


def test_cipher_month_alphabet_matches_the_parser():
    # backend/order_constants.py:9-22 — q is December, o is October, f is February
    assert MONTH_CHARS == "jfmayulgeonq"
    for m in range(1, 13):
        f = _fields(ticker="SPY", expiries=[Expiry(m)], strikes=[500], cp="c")
        assert build_cipher(f)[0] == f"spy {MONTH_CHARS[m - 1]};500c"


def test_cipher_year_is_the_month_char_repeated():
    today = date(2026, 11, 5)
    f = _fields(ticker="AAPL", expiries=[Expiry(1, None, 2028)], strikes=[200], cp="c")
    assert build_cipher(f, today=today)[0] == "aapl jjj;200c"
    f2 = _fields(ticker="AAPL", expiries=[Expiry(1, None, 2027)], strikes=[200], cp="c")
    assert build_cipher(f2, today=today)[0] == "aapl jj;200c"
    # no year spoken: the next occurrence of that month — May after November is next year
    f3 = _fields(ticker="AAPL", expiries=[Expiry(5)], strikes=[200], cp="c")
    assert build_cipher(f3, today=today)[0] == "aapl yy;200c"
    f4 = _fields(ticker="AAPL", expiries=[Expiry(11)], strikes=[200], cp="c")
    assert build_cipher(f4, today=today)[0] == "aapl n;200c"


def test_cipher_refuses_a_past_year():
    f = _fields(ticker="AAPL", expiries=[Expiry(1, None, 2025)], strikes=[200], cp="c")
    assert build_cipher(f, today=date(2026, 11, 5)) == (None, ["expiry_year_in_past"])


def test_cipher_price_always_carries_a_decimal_point():
    # parser: 'bb2' is a SIZE of 2 (order_parser.py:293); only 'bb2.0' is a $2.00 price
    f = _fields(ticker="AAPL", expiries=[Expiry(5)], strikes=[180], cp="c", broker_side="b", price=2)
    assert build_cipher(f)[0] == "aapl y;180c bb2.0"
    f.broker_side, f.price = F("s", 0.99), F(3, 0.99)
    assert build_cipher(f)[0] == "aapl y;180c ss3.0"
    f.broker_side = F(None, 0.99)
    assert build_cipher(f)[0] == "aapl y;180c r3.0"


def test_cipher_side_without_price_is_a_standalone_term_never_inverted():
    f = _fields(ticker="AAPL", expiries=[Expiry(5)], strikes=[180, 190], cp="c", spread_type="cs",
                broker_side="b", broker_qty=500)
    assert build_cipher(f)[0] == "aapl y;180;190cs z500 b"
    f.broker_side = F("s", 0.99)
    assert build_cipher(f)[0] == "aapl y;180;190cs z500 s"


def test_cipher_time_spread_is_one_group_per_expiry():
    f = _fields(ticker="NVDA", expiries=[Expiry(5), Expiry(6)], strikes=[140], spread_type="cts")
    assert build_cipher(f)[0] == "nvda y;140c / u;140c cts"
    f.spread_type, f.strikes = F("pts", 0.99), F([140, 150], 0.99)
    assert build_cipher(f)[0] == "nvda y;140p / u;150p pts"
    f.expiries = F([Expiry(5)], 0.99)
    assert build_cipher(f) == (None, ["time_spread_needs_two_expiries"])


def test_cipher_refuses_what_the_parser_cannot_read():
    f = _fields(ticker="SPY", expiries=[Expiry(5), Expiry(6)], strikes=[590], spread_type="jr")
    assert build_cipher(f) == (None, ["spread_type_not_in_parser:jr"])
    f2 = _fields(ticker="SPY", expiries=[Expiry(5), Expiry(6)], strikes=[590], cp="c")
    assert build_cipher(f2) == (None, ["multi_expiry_not_encodable"])


def test_cipher_needs_minimum_order_expression():
    c, why = build_cipher(_fields(ticker="AAPL", strikes=[180], cp="c"))
    assert c is None and why == ["expiries"]


@pytest.mark.parametrize("spoken,qty", [("fifteen hundred up", 1500), ("twenty five hundred up", 2500),
                                         ("two thousand five hundred up", 2500), ("five hundred up", 500)])
def test_spoken_sizes(spoken, qty):
    assert vals(run(f"Apple May 180 calls {spoken}"))["broker_qty"] == qty
