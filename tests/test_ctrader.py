"""cTrader conversions and the demo-only guard. No network calls here.

The volume tests are the point of this file. cTrader counts volume in hundredths of a unit, so a
factor-of-100 slip would size every forex trade 100x wrong in complete silence.
"""

from __future__ import annotations

import pytest

from trader.brokers.base import LiveTradingRefused
from trader.brokers.ctrader import (
    DEMO_HOST,
    LIVE_HOST,
    CTraderError,
    SymbolSpec,
    build_symbol_map,
    check_demo_only,
    from_ctrader_name,
    from_volume_cents,
    round_price,
    round_stop,
    to_ctrader_name,
    to_volume_cents,
)

# EUR/USD as a cTrader broker reports it: one lot is 100,000 units, the smallest trade is 0.01 lot.
EURUSD = SymbolSpec(symbol_id=1, name="EURUSD", digits=5, lot_size_cents=10_000_000,
                    min_volume_cents=100_000, max_volume_cents=1_000_000_000, step_volume_cents=100_000)
# A symbol with a finer step, to prove rounding follows the broker rather than a hardcoded assumption.
XAUUSD = SymbolSpec(symbol_id=41, name="XAUUSD", digits=2, lot_size_cents=10_000,
                    min_volume_cents=100, max_volume_cents=5_000_000, step_volume_cents=100)


# --- the paper-only guard -------------------------------------------------------------------------

def test_live_host_is_refused():
    with pytest.raises(LiveTradingRefused, match="demo"):
        check_demo_only(LIVE_HOST)


def test_demo_host_is_accepted_despite_case_and_spaces():
    check_demo_only(f"  {DEMO_HOST.upper()} ")


@pytest.mark.parametrize("host", ["", "localhost", "demo.ctraderapi.com.evil.test", "live.ctraderapi.com"])
def test_anything_that_is_not_the_demo_host_is_refused(host):
    with pytest.raises(LiveTradingRefused):
        check_demo_only(host)


# --- symbol names ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("ours", "theirs"), [("EUR/USD", "EURUSD"), ("GBP/USD", "GBPUSD"), ("XAU/USD", "XAUUSD")])
def test_our_symbols_convert_to_ctrader_names(ours, theirs):
    assert to_ctrader_name(ours) == theirs


@pytest.mark.parametrize("theirs", ["EURUSD", "eurusd", "EUR/USD", "EURUSD.r", "EURUSD.pro"])
def test_broker_names_map_back_to_ours_including_broker_suffixes(theirs):
    assert from_ctrader_name(theirs, ["EUR/USD", "GBP/USD"]) == "EUR/USD"


def test_symbols_we_do_not_trade_map_to_nothing():
    assert from_ctrader_name("USDJPY", ["EUR/USD"]) is None


def test_symbol_map_keeps_ours_and_ignores_the_rest():
    others = [SymbolSpec(2, "USDJPY", 3, 10_000_000, 100_000, 10_000_000, 100_000)]
    assert build_symbol_map([EURUSD, *others], ["EUR/USD"]) == {"EUR/USD": EURUSD}


def test_two_broker_symbols_matching_one_of_ours_is_an_error():
    """Silently picking one feed over another would mis-size every trade in it."""
    variant = SymbolSpec(99, "EURUSD.r", 5, 1_000_000, 10_000, 1_000_000, 10_000)
    with pytest.raises(CTraderError, match="two cTrader symbols"):
        build_symbol_map([EURUSD, variant], ["EUR/USD"])


# --- volume: the 100x trap ------------------------------------------------------------------------

def test_one_thousand_units_is_one_hundred_thousand_cents():
    """The anchor test. cTrader volume is units x 100 -- not units, and not lots."""
    assert to_volume_cents(1_000, EURUSD) == 100_000


def test_one_standard_lot_matches_the_brokers_own_lot_size():
    assert to_volume_cents(100_000, EURUSD) == EURUSD.lot_size_cents


def test_volume_rounds_down_to_the_brokers_step():
    """Rounding up would risk more than the 1% the risk gate sized for."""
    assert to_volume_cents(1_999, EURUSD) == 100_000        # 1,999 units -> 0.01 lot, not 0.02
    assert to_volume_cents(2_000, EURUSD) == 200_000


def test_a_finer_step_rounds_to_that_step_not_a_hardcoded_one():
    assert to_volume_cents(3.7, XAUUSD) == 300              # step is 100 cents = 1 unit


def test_below_the_broker_minimum_raises_rather_than_sending_nothing():
    with pytest.raises(CTraderError, match="below the broker minimum"):
        to_volume_cents(999, EURUSD)


def test_volume_is_clamped_to_the_broker_maximum():
    assert to_volume_cents(50_000_000, EURUSD) == EURUSD.max_volume_cents


@pytest.mark.parametrize("units", [0, -1, -1000])
def test_non_positive_units_are_refused(units):
    with pytest.raises(CTraderError, match="must be positive"):
        to_volume_cents(units, EURUSD)


def test_float_dust_does_not_lose_a_whole_step():
    """0.01 lot arrived at by division can land at 999.9999999 units; that must still be tradeable."""
    assert to_volume_cents(100_000 / 100.0000000001, EURUSD) == 100_000


def test_cents_convert_back_to_units():
    assert from_volume_cents(to_volume_cents(2_000, EURUSD)) == 2_000


def test_a_step_that_is_not_positive_is_rejected_at_construction():
    with pytest.raises(CTraderError, match="step volume must be positive"):
        SymbolSpec(1, "BAD", 5, 10_000_000, 100_000, 1_000_000, 0)


def test_min_above_max_is_rejected_at_construction():
    with pytest.raises(CTraderError, match="exceeds max"):
        SymbolSpec(1, "BAD", 5, 10_000_000, 2_000, 1_000, 100)


# --- prices ---------------------------------------------------------------------------------------

def test_prices_round_to_the_symbols_precision():
    assert round_price(1.123456789, EURUSD) == 1.12346     # 5 digits
    assert round_price(1934.567, XAUUSD) == 1934.57        # 2 digits


def test_directional_rounding_goes_the_way_it_is_told():
    assert round_price(1.234564, EURUSD, mode="up") == 1.23457
    assert round_price(1.234566, EURUSD, mode="down") == 1.23456


def test_a_protective_stop_rounds_up_towards_the_market():
    """Rounding a sell stop down would widen the loss past what the risk gate approved."""
    assert round_stop(1.2345612, EURUSD) == 1.23457


@pytest.mark.parametrize("price", [0, -1.5])
def test_non_positive_prices_are_refused(price):
    with pytest.raises(CTraderError, match="must be positive"):
        round_price(price, EURUSD)


def test_an_unknown_rounding_mode_is_an_error():
    with pytest.raises(CTraderError, match="unknown rounding mode"):
        round_price(1.2345, EURUSD, mode="sideways")
