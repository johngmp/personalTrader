"""Forex price precision and the alerts you act on by hand.

Rounding an FX price to 2 decimals is not cosmetic: EUR/USD 0.69823 -> 0.70 moves the stop 18 pips and
silently changes the risk the gate approved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.engine.live import HAND_EXECUTED, _fill_text, _stop_move_text
from trader.models import AssetClass, Fill, Side, format_price, price_decimals, round_price


@pytest.mark.parametrize(("price", "asset_class", "decimals"), [
    (0.71250, AssetClass.FOREX, 5),      # AUD/USD -- a major
    (1.14890, AssetClass.FOREX, 5),      # EUR/USD
    (156.874, AssetClass.FOREX, 3),      # USD/JPY -- hundreds, three places
    (4412.30, AssetClass.FOREX, 2),      # XAU/USD -- gold, thousands, cents
    (761.35, AssetClass.US_EQUITY, 2),
    (77454.09, AssetClass.CRYPTO, 2),
])
def test_each_market_is_quoted_to_its_own_precision(price, asset_class, decimals):
    assert price_decimals(price, asset_class) == decimals


def test_an_fx_stop_no_longer_collapses_to_two_decimals():
    """The bug: round(0.69823, 2) == 0.7, an 18 pip move in the stop, changing the approved risk."""
    assert round_price(0.69823, AssetClass.FOREX) == 0.69823
    assert round(0.69823, 2) == 0.7        # what the code used to do
    assert round_price(0.69823, AssetClass.FOREX) != round(0.69823, 2)


def test_stock_and_crypto_prices_still_round_to_cents():
    assert round_price(761.3549, AssetClass.US_EQUITY) == 761.35
    assert round_price(77454.0912, AssetClass.CRYPTO) == 77454.09


def test_gold_keeps_cents_rather_than_five_decimals():
    assert round_price(4412.2998, AssetClass.FOREX) == 4412.3


def test_formatting_matches_the_rounding():
    assert format_price(0.7125, AssetClass.FOREX) == "0.71250"
    assert format_price(4412.2998, AssetClass.FOREX) == "4,412.30"
    assert format_price(761.35, AssetClass.US_EQUITY) == "761.35"


# --- the messages you act on ----------------------------------------------------------------------

def a_fill(symbol: str, price: float, side: Side = Side.BUY) -> Fill:
    return Fill(client_order_id="c1", sleeve_id="S9", symbol=symbol, side=side, qty=3440, price=price,
                cost_haircut_usd=0.0, ts=datetime.now(timezone.utc))


def test_an_fx_alert_shows_the_full_rate_and_no_dollar_sign():
    """'@ $1.15' is both wrong and unusable: a rate is not a dollar amount and 2dp loses the trade."""
    text = _fill_text(a_fill("AUD/USD", 0.71250), asset_class=AssetClass.FOREX)
    assert "0.71250" in text and "$" not in text


def test_a_stock_alert_still_shows_dollars():
    assert "$761.35" in _fill_text(a_fill("SPY", 761.35), asset_class=AssetClass.US_EQUITY)


def test_a_stop_move_alert_names_both_prices_and_asks_you_to_act():
    text = _stop_move_text("S9", "AUD/USD", 0.69800, 0.70410, AssetClass.FOREX)
    assert "0.69800" in text and "0.70410" in text
    assert "by hand" in text


def test_the_first_stop_reads_sensibly_when_there_was_none_before():
    assert "none" in _stop_move_text("S9", "EUR/USD", None, 1.13500, AssetClass.FOREX)


# --- who gets messaged ----------------------------------------------------------------------------

@pytest.mark.parametrize(("broker", "notifies"), [
    ("alpaca", True),        # real paper account
    ("shadow", False),       # research only: stays off your phone
    ("shadow_fx", True),     # you execute this one by hand, so it must reach you
])
def test_only_brokers_you_act_on_reach_your_phone(broker, notifies):
    from trader.engine.live import Engine

    fake = type("B", (), {"name": broker})()
    assert Engine._should_notify(type("E", (), {"broker": fake})()) is notifies


def test_the_hand_executed_set_is_all_shadow_brokers():
    """A hand-executed broker that wasn't a shadow one would mean real orders going out unsupervised."""
    assert all(name.startswith("shadow") for name in HAND_EXECUTED)
