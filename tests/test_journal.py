from datetime import datetime, timezone

import pytest

from trader.config import CostModel
from trader.journal.db import trading_day_start
from trader.models import BrokerOrder, OrderRequest, OrderStatus, Side

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)


def _order(journal) -> OrderRequest:
    o = OrderRequest(sleeve_id="S1", symbol="SPY", side=Side.BUY, qty=10, stop_price=490)
    journal.record_order(o, "us_equity")
    return o


def _bo(o, status, filled_qty, avg) -> BrokerOrder:
    return BrokerOrder(broker_order_id="b1", client_order_id=o.client_order_id, symbol="SPY", side=Side.BUY,
                       status=status, qty=10, filled_qty=filled_qty, filled_avg_price=avg)


def test_partial_fills_are_recorded_as_separate_slices(journal):
    o = _order(journal)
    f1 = journal.record_fill_progress(_bo(o, OrderStatus.PARTIALLY_FILLED, 4, 100.0), "S1", NO_COST)
    f2 = journal.record_fill_progress(_bo(o, OrderStatus.FILLED, 10, 101.2), "S1", NO_COST)
    assert (f1.qty, f1.price) == (4, 100.0)
    assert f2.qty == pytest.approx(6)
    assert f2.price == pytest.approx(102.0)  # (10*101.2 - 4*100) / 6
    assert journal.sleeve_holdings("S1") == {"SPY": pytest.approx(10)}


def test_repeated_updates_do_not_double_count(journal):
    o = _order(journal)
    bo = _bo(o, OrderStatus.FILLED, 10, 100.0)
    assert journal.record_fill_progress(bo, "S1", NO_COST) is not None
    assert journal.record_fill_progress(bo, "S1", NO_COST) is None
    assert journal.sleeve_holdings("S1") == {"SPY": pytest.approx(10)}


def test_peak_equity_tracks_the_highest_snapshot(journal):
    for eq in (100_000, 104_000, 98_000):
        journal.snapshot_equity("alpaca", eq, eq)
    assert journal.peak_equity("alpaca") == 104_000
    assert journal.peak_equity("other") == 0.0


def test_trading_day_starts_at_new_york_midnight():
    # 03:00 UTC on the 18th is still the 17th in New York.
    start = trading_day_start(datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc))
    assert (start.year, start.month, start.day, start.hour) == (2026, 9, 17, 0)


def test_broker_rounding_leftovers_count_as_flat(journal):
    """Regression: the first live round trip left 3.3e-10 BTC in the journal after Alpaca rounded to 9 decimals."""
    buy = OrderRequest(sleeve_id="TEST", symbol="BTC/USD", side=Side.BUY, notional=10, stop_price=70_000)
    sell = OrderRequest(sleeve_id="TEST", symbol="BTC/USD", side=Side.SELL, qty=0.000127149)
    fee = CostModel(half_spread_bps=5, slippage_bps=10, fee_bps=0, broker_fee_bps=25)
    for o, qty in ((buy, 0.000127468), (sell, 0.000127149)):
        journal.record_order(o, "crypto")
        journal.record_fill_progress(
            BrokerOrder(broker_order_id=o.client_order_id, client_order_id=o.client_order_id, symbol="BTC/USD",
                        side=o.side, status=OrderStatus.FILLED, filled_qty=qty, filled_avg_price=76_900.0),
            "TEST", fee)
    assert journal.sleeve_holdings("TEST") == {}
    assert journal.all_holdings() == {}
