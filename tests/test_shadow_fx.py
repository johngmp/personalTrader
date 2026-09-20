"""Forex with no broker at all: Yahoo prices, shadow execution, and the weekend-aware freshness rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.brokers.base import LiveTradingRefused
from trader.data.fx_quotes import EASTERN, FX_UNIT_STEP, YahooFXFeed
from trader.data.yahoo_bars import drop_forming_bar
from trader.models import AssetClass, OrderRequest, Side

SUNDAY = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)      # weekend: forex shut
WEDNESDAY = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)  # mid-week: forex open


# --- the feed is a dead end for orders ------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda f: f.get_account(),
    lambda f: f.submit_order(OrderRequest(sleeve_id="S9", symbol="EUR/USD", side=Side.BUY, qty=1000),
                             AssetClass.FOREX),
    lambda f: f.get_order("x"),
    lambda f: f.cancel_order("x"),
    lambda f: f.replace_stop("x", "y", 1.0),
])
def test_the_price_feed_refuses_everything_that_would_touch_a_market(call):
    with pytest.raises(LiveTradingRefused, match="price feed"):
        call(YahooFXFeed())


def test_the_feed_holds_no_positions_and_no_orders():
    feed = YahooFXFeed()
    assert feed.get_positions() == [] and feed.get_open_orders() == []


def test_forex_trades_in_whole_units():
    assert YahooFXFeed().order_step("EUR/USD", AssetClass.FOREX) == FX_UNIT_STEP


def test_the_clock_knows_forex_shuts_at_the_weekend():
    assert not YahooFXFeed(clock=lambda: SUNDAY).get_clock().is_open
    assert YahooFXFeed(clock=lambda: WEDNESDAY).get_clock().is_open


def test_an_unmapped_symbol_is_refused_before_any_network_call():
    from trader.data.fx_quotes import FXQuoteError

    with pytest.raises(FXQuoteError, match="no Yahoo ticker"):
        YahooFXFeed()._fetch("ZZZ/USD", SUNDAY)


# --- bar completion -------------------------------------------------------------------------------

def frame_for(dates: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="date")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}, index=idx)


def test_todays_unfinished_bar_is_dropped():
    """Wednesday 11am New York: Wednesday's bar is still forming and must not be decided on."""
    df = frame_for(["2026-09-14", "2026-09-15", "2026-09-16"])
    kept = drop_forming_bar(df, datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc))
    assert list(kept.index.strftime("%Y-%m-%d")) == ["2026-09-14", "2026-09-15"]


def test_after_the_five_pm_rollover_the_day_counts_as_finished():
    df = frame_for(["2026-09-15", "2026-09-16"])
    after_close = datetime(2026, 9, 16, 21, 30, tzinfo=timezone.utc)  # 5:30pm New York
    assert "2026-09-16" in list(drop_forming_bar(df, after_close).index.strftime("%Y-%m-%d"))


def test_an_empty_frame_survives_bar_completion():
    assert drop_forming_bar(pd.DataFrame(), SUNDAY).empty


# --- the weekend freshness rule -------------------------------------------------------------------

def fresh_price_check(settings, asset_class: AssetClass, quote_age_s: float, market_open: bool = False):
    """Run one buy through the real risk gate and return just its fresh_price verdict."""
    from trader.models import Account, MarketClock, Quote
    from trader.risk.gate import RiskContext, RiskGate

    symbol = {AssetClass.FOREX: "EUR/USD", AssetClass.CRYPTO: "BTC/USD"}[asset_class]
    price = 100.0
    ctx = RiskContext(
        now=SUNDAY, broker_is_paper=True,
        account=Account(equity=100_000, last_equity=100_000, cash=100_000, buying_power=100_000, is_paper=True),
        positions=[],
        quote=Quote(symbol=symbol, price=price, ts=SUNDAY - timedelta(seconds=quote_age_s)),
        asset_class=asset_class, sleeve_holdings={}, peak_equity=100_000, orders_today=0,
        # Market shut since Friday's 5pm New York close, as it is every weekend.
        market=MarketClock(is_open=market_open, last_close=SUNDAY - timedelta(hours=40),
                           next_open=SUNDAY + timedelta(hours=8)),
    )
    order = OrderRequest(sleeve_id="S1", symbol=symbol, side=Side.BUY, notional=2_000.0,
                         stop_price=price * 0.98)
    decision = RiskGate(settings).check(order, ctx)
    checks = [c for c in decision.checks if c.name == "fresh_price"]
    assert checks, "the gate did not run a fresh_price check at all"
    return checks[0]


def test_a_closed_forex_market_accepts_the_last_session_price(settings):
    """Without this, an FX sleeve could never act on a Sunday -- the price is always ~36 hours old."""
    assert fresh_price_check(settings, AssetClass.FOREX, quote_age_s=36 * 3600).passed


def test_crypto_never_gets_the_weekend_excuse(settings):
    """Crypto trades around the clock, so a stale crypto price is a fault, not a closed market."""
    assert not fresh_price_check(settings, AssetClass.CRYPTO, quote_age_s=36 * 3600).passed


def test_a_forex_price_older_than_the_last_close_is_still_stale(settings):
    assert not fresh_price_check(settings, AssetClass.FOREX, quote_age_s=60 * 3600).passed


# --- delayed free data, market open ---------------------------------------------------------------

def test_a_twelve_minute_old_forex_price_is_accepted_while_the_market_is_open(settings):
    """Yahoo's gold feed runs 10-15 minutes behind. Under the 300s rule that was rejected every run."""
    assert fresh_price_check(settings, AssetClass.FOREX, quote_age_s=12 * 60, market_open=True).passed


def test_crypto_still_gets_the_strict_limit_while_its_market_is_open(settings):
    """Crypto comes from a real broker feed; 12 minutes old there means something is wrong."""
    assert not fresh_price_check(settings, AssetClass.CRYPTO, quote_age_s=12 * 60, market_open=True).passed


def test_a_genuinely_dead_forex_feed_is_still_caught(settings):
    """The check must still do its job: hours old with the market open means the feed is broken."""
    assert not fresh_price_check(settings, AssetClass.FOREX, quote_age_s=5 * 3600, market_open=True).passed
