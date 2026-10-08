"""End to end: journal -> risk gate -> (simulated) broker -> journal, including fills and cost haircuts."""

from __future__ import annotations

import pytest

from trader.brokers.base import LiveTradingRefused
from trader.brokers.sim import SimBroker
from trader.engine.executor import Executor
from trader.models import OrderRequest, OrderStatus, Side
from trader.risk.kill_switch import KillSwitch

from .conftest import NOW


@pytest.fixture
def executor(settings, sim, journal) -> Executor:
    return Executor(settings, sim, journal, clock=lambda: NOW, sleep=lambda _: None)


def btc_buy(notional=2_000.0) -> OrderRequest:
    return OrderRequest(sleeve_id="S1", symbol="BTC/USD", side=Side.BUY, notional=notional, stop_price=58_800)


def test_approved_order_is_sent_filled_and_journaled(executor, sim, journal, settings):
    result = executor.submit(btc_buy(), wait_for_fill_s=0)
    assert result.status is OrderStatus.FILLED
    assert len(sim.submitted) == 1

    row = journal.order(result.order.client_order_id)
    assert row["status"] == "filled" and row["broker"] == "sim"
    assert journal.last_decision(result.order.client_order_id)["approved"] == 1

    (fill,) = result.fills
    assert fill.qty == pytest.approx(2_000 / 60_000)
    assert fill.price == 60_000
    # estimated haircut for crypto = 5 + 10 bps of $2,000 (Alpaca's real fee is recorded separately)
    assert fill.cost_haircut_usd == pytest.approx(3.0)
    assert fill.broker_fee_usd == pytest.approx(5.0)  # 25 bps of $2,000
    assert fill.fee_qty == pytest.approx(fill.qty * 0.0025)
    assert journal.sleeve_holdings("S1") == {"BTC/USD": pytest.approx(2_000 / 60_000 * 0.9975)}


def test_rejected_order_never_reaches_the_broker(executor, sim, journal):
    result = executor.submit(btc_buy(notional=9_000), wait_for_fill_s=0)  # over the 25% position limit
    assert result.status is OrderStatus.REJECTED
    assert sim.submitted == []
    assert journal.order(result.order.client_order_id)["status"] == "rejected"
    assert "max_position_of_sleeve" in journal.last_decision(result.order.client_order_id)["summary"]


def test_kill_switch_blocks_everything(executor, sim, settings):
    KillSwitch(settings.kill_switch_path).engage("test")
    assert executor.submit(btc_buy(), wait_for_fill_s=0).status is OrderStatus.REJECTED
    assert sim.submitted == []


def test_drawdown_limit_engages_the_kill_switch(settings, journal):
    broker = SimBroker(cash=100_000)
    broker.set_price("BTC/USD", 60_000, NOW)
    journal.snapshot_equity("sim", 120_000, 120_000)  # earlier peak
    ex = Executor(settings, broker, journal, clock=lambda: NOW, sleep=lambda _: None)
    assert ex.submit(btc_buy(), wait_for_fill_s=0).status is OrderStatus.REJECTED
    assert KillSwitch(settings.kill_switch_path).active


def test_round_trip_buy_then_sell_flattens_the_sleeve(executor, journal, sim):
    executor.submit(btc_buy(), wait_for_fill_s=0)
    held = journal.sleeve_holdings("S1")["BTC/USD"]
    sim.set_price("BTC/USD", 61_000, NOW)
    result = executor.submit(OrderRequest(sleeve_id="S1", symbol="BTC/USD", side=Side.SELL, qty=held),
                             wait_for_fill_s=0)
    assert result.status is OrderStatus.FILLED
    assert journal.sleeve_holdings("S1") == {}


def test_duplicate_order_is_blocked(executor, sim):
    executor.submit(btc_buy(), wait_for_fill_s=0)
    second = executor.submit(btc_buy(notional=100), wait_for_fill_s=0)
    assert "no_duplicate" in {c.name for c in second.decision.failures}
    assert len(sim.submitted) == 1


def test_unfilled_order_is_picked_up_by_sync(settings, journal):
    broker = SimBroker(cash=100_000, fill_immediately=False)
    broker.set_price("BTC/USD", 60_000, NOW)
    ex = Executor(settings, broker, journal, clock=lambda: NOW, sleep=lambda _: None)
    result = ex.submit(btc_buy(), wait_for_fill_s=0)
    assert result.status is OrderStatus.SUBMITTED
    assert len(journal.open_orders()) == 1

    broker.fill_pending()
    fills = ex.sync_open_orders()
    assert len(fills) == 1
    assert journal.open_orders() == []
    assert journal.order(result.order.client_order_id)["status"] == "filled"


def test_broker_error_is_recorded_not_retried(executor, sim, journal, monkeypatch):
    def boom(*_):
        raise RuntimeError("insufficient buying power")

    monkeypatch.setattr(sim, "submit_order", boom)
    result = executor.submit(btc_buy(), wait_for_fill_s=0)
    assert result.status is OrderStatus.REJECTED
    row = journal.order(result.order.client_order_id)
    assert row["status"] == "rejected" and "insufficient buying power" in row["broker_status"]


def test_executor_refuses_live_mode(settings, sim, journal):
    live = settings.model_copy(update={"risk": settings.risk.model_copy(update={"mode": "live"})})
    with pytest.raises(LiveTradingRefused):
        Executor(live, sim, journal)


def test_journal_matches_broker_when_fees_are_taken_in_coins(settings, journal):
    """Regression: Alpaca takes its crypto fee from the coins bought, so the sleeve holds less than was filled."""
    broker = SimBroker(cash=100_000, crypto_fee_bps=25)
    broker.set_price("BTC/USD", 60_000, NOW)
    ex = Executor(settings, broker, journal, clock=lambda: NOW, sleep=lambda _: None)
    ex.submit(btc_buy(), wait_for_fill_s=0)

    (position,) = broker.get_positions()
    assert journal.sleeve_holdings("S1")["BTC/USD"] == pytest.approx(position.qty)

    # Selling the journal's full holding must succeed (it would fail if the fee were ignored).
    held = journal.sleeve_holdings("S1")["BTC/USD"]
    result = ex.submit(OrderRequest(sleeve_id="S1", symbol="BTC/USD", side=Side.SELL, qty=held), wait_for_fill_s=0)
    assert result.status is OrderStatus.FILLED, result.error
    assert journal.sleeve_holdings("S1") == {}
    assert broker.get_positions() == []


def test_costs_survive_a_symbol_leaving_the_universe(settings, tmp_path):
    """`asset_class` reads straight from universe.yaml, so deleting a line from it while a position or an
    open order still exists made the cost lookup `settings.costs[None]`: "sync fills failed: KeyError:
    None", every run.

    That is worse than it sounds. Syncing is how the engine learns an order filled, so the journal would
    stop following the broker while every other part of the system carried on trusting it.
    """
    from trader.brokers.sim import SimBroker
    from trader.engine.executor import Executor
    from trader.journal.db import Journal
    from trader.models import AssetClass, OrderRequest, Side

    journal = Journal(tmp_path / "j.db")
    broker = SimBroker(cash=100_000, name="alpaca")
    broker.set_price("SPY", 500.0, NOW)
    ex = Executor(settings, broker, journal, clock=lambda: NOW, sleep=lambda _: None)
    ex.submit(OrderRequest(sleeve_id="S1", symbol="SPY", side=Side.BUY, qty=1.0, stop_price=490.0,
                           reason="test"), wait_for_fill_s=0.0)
    assert journal.last_asset_class("SPY") is AssetClass.US_EQUITY

    # SPY is deleted from universe.yaml while the position is still open.
    symbols = dict(settings.universe.symbols)
    symbols.pop("SPY")
    settings.universe.symbols = symbols
    assert settings.asset_class("SPY") is None

    assert ex._cost_model("SPY") == settings.costs[AssetClass.US_EQUITY], \
        "the order recorded what it was; that is a fact, not a guess"
    journal.close()


def test_a_symbol_never_ordered_and_not_in_the_universe_costs_the_most(settings, tmp_path):
    """If this is going to be wrong about a cost, it should be wrong in the direction that makes a
    strategy look worse than it was. Crypto is the dearest of the three."""
    from trader.brokers.sim import SimBroker
    from trader.engine.executor import Executor
    from trader.journal.db import Journal
    from trader.models import AssetClass

    journal = Journal(tmp_path / "j.db")
    ex = Executor(settings, SimBroker(cash=1_000, name="alpaca"), journal, clock=lambda: NOW,
                  sleep=lambda _: None)
    assert ex._cost_model("NEVER/TRADED") == settings.costs[AssetClass.CRYPTO]
    journal.close()
