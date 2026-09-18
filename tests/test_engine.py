"""Full engine runs against the simulated broker and synthetic daily bars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trader.brokers.sim import SimBroker
from trader.engine.executor import Executor
from trader.engine.live import Engine
from trader.models import AssetClass, OrderStatus, OrderType
from trader.notify.telegram import NullNotifier
from trader.risk.kill_switch import KillSwitch

START = {"BTC/USD": 60_000.0, "ETH/USD": 3_000.0, "QQQ": 450.0, "SPY": 500.0}


class FakeBars:
    """Serves bars up to `cutoff` (the last finished day)."""

    def __init__(self, n: int = 260) -> None:
        idx = pd.bdate_range("2025-01-01", periods=n)
        rng = np.random.default_rng(1)
        self.full = {}
        for i, (symbol, p0) in enumerate(START.items()):
            steps = 1 + 0.002 + rng.normal(0, 0.004, n)  # steady uptrend
            close = p0 * np.cumprod(steps)
            self.full[symbol] = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                                              "close": close, "volume": 1.0}, index=idx)
        self.cutoff = idx[-1]

    def completed_bars(self, symbol, asset_class, now, market):
        return self.full[symbol].loc[: self.cutoff]

    def add_day(self, moves: dict[str, float]) -> pd.Timestamp:
        """Append one finished day, moving each symbol's close by the given fraction."""
        new_date = self.cutoff + pd.offsets.BDay(1)
        for symbol, df in self.full.items():
            c = float(df["close"].iloc[-1]) * (1 + moves.get(symbol, 0.0))
            self.full[symbol] = pd.concat([df, pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0},
                                                            index=[new_date])])
        self.cutoff = new_date
        return new_date


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(settings, tmp_path):
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")

    def sync_prices():
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)

    sync_prices()
    notifier = NullNotifier()
    executor = Executor(settings, broker, journal, clock=clock, sleep=lambda _: None)
    engine = Engine(settings, broker, journal, bars, notifier=notifier, clock=clock, executor=executor, fill_wait_s=0)

    def step(moves: dict[str, float] | None = None, hours: float = 24):
        clock.now += timedelta(hours=hours)
        if moves is not None:
            bars.add_day(moves)
        sync_prices()
        return engine.run_once()

    yield dict(engine=engine, broker=broker, journal=journal, bars=bars, clock=clock, notifier=notifier,
               step=step, settings=settings)
    journal.close()


def _stops(broker: SimBroker) -> dict[str, float]:
    return {o.symbol: o.stop_price for o in broker.get_open_orders()
            if o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)}


def test_first_run_buys_benchmark_enters_trends_and_places_stops(world):
    report = world["engine"].run_once()
    assert not report.problems, report.problems
    j, broker = world["journal"], world["broker"]

    b0 = j.sleeve_holdings("B0")
    assert set(b0) == {"SPY", "QQQ", "BTC/USD"}
    assert float(b0["SPY"]).is_integer() and float(b0["QQQ"]).is_integer()  # whole shares for stocks
    assert set(j.sleeve_holdings("S1")) == set(START)

    # Every S1 position has a resting stop at the broker; the benchmark has none.
    stops = _stops(broker)
    assert set(stops) == set(START)
    stock_stop = next(o for o in broker.get_open_orders() if o.symbol == "SPY")
    crypto_stop = next(o for o in broker.get_open_orders() if o.symbol == "BTC/USD")
    assert stock_stop.order_type is OrderType.STOP
    assert crypto_stop.order_type is OrderType.STOP_LIMIT and crypto_stop.limit_price < crypto_stop.stop_price

    # Journal and broker agree position by position.
    total: dict[str, float] = {}
    for (_, s), q in j.all_holdings().items():
        total[s] = total.get(s, 0) + q
    assert total == pytest.approx({p.symbol: p.qty for p in broker.get_positions()})
    assert (world["settings"].root / "vault" / "Home.md").exists()
    assert (world["settings"].root / "data" / "dashboard.html").exists()


def test_rerunning_without_a_new_bar_does_nothing(world):
    world["engine"].run_once()
    orders_before = len(world["broker"].submitted)
    report = world["step"](None, hours=0.25)
    assert len(world["broker"].submitted) == orders_before
    assert not report.problems


def test_trailing_stop_moves_up_with_the_price(world):
    world["engine"].run_once()
    before = _stops(world["broker"])["SPY"]
    world["step"]({s: 0.03 for s in START})  # a strong up day
    after = _stops(world["broker"])["SPY"]
    assert after > before
    # The old stop order was replaced, not left behind.
    assert sum(1 for o in world["broker"].get_open_orders() if o.symbol == "SPY") == 1


def test_stop_fill_closes_the_position_cleanly(world):
    world["engine"].run_once()
    stop = _stops(world["broker"])["QQQ"]
    world["broker"].set_price("QQQ", stop * 0.99, world["clock"].now)  # broker-held stop triggers
    report = world["step"](None, hours=1)
    assert "QQQ" not in world["journal"].sleeve_holdings("S1")
    assert world["journal"].position_state("S1", "QQQ") is None
    assert any(f.symbol == "QQQ" and f.sleeve_id == "S1" for f in report.fills)
    assert "QQQ" in world["journal"].sleeve_holdings("B0")  # the benchmark's QQQ is untouched


def test_stopped_out_position_can_re_enter_while_the_trend_holds(world):
    """Like the backtest: a stop-out isn't an exit signal; if the trend is intact the strategy buys again."""
    world["engine"].run_once()
    first_stop = _stops(world["broker"])["SPY"]
    world["step"]({"SPY": -0.03, "QQQ": 0.0, "BTC/USD": 0.0, "ETH/USD": 0.0})  # falls through the stop
    fills = [f for f in world["journal"].fills("S1") if f["symbol"] == "SPY"]
    assert [f["side"] for f in fills] == ["buy", "sell", "buy"]
    assert _stops(world["broker"])["SPY"] < first_stop  # the new trade has its own stop


def test_trend_ending_exits_and_cancels_the_stop(world):
    world["engine"].run_once()
    # Park the stop far away so only the trend rule can close the trade.
    state = world["journal"].position_state("S1", "SPY")
    world["journal"].upsert_position_state("S1", "SPY", stop=1.0)
    world["step"](None, hours=0.25)  # the engine moves the resting order to match (it may only go down here via re-place)
    world["broker"].cancel_order(next(o.broker_order_id for o in world["broker"].get_open_orders() if o.symbol == "SPY"))
    world["journal"].mark_order(state["stop_order_id"], OrderStatus.CANCELED, "test")
    world["step"](None, hours=0.25)  # re-placed at the parked level
    assert _stops(world["broker"])["SPY"] == pytest.approx(1.0)

    world["step"]({"SPY": -0.30, "QQQ": 0.0, "BTC/USD": 0.0, "ETH/USD": 0.0})  # close far below the 200-day average
    assert "SPY" not in world["journal"].sleeve_holdings("S1")
    assert "SPY" not in _stops(world["broker"])
    assert world["journal"].position_state("S1", "SPY") is None
    assert "SPY" in world["journal"].sleeve_holdings("B0")


def test_mismatch_freezes_the_symbol_and_alerts(world):
    world["engine"].run_once()
    asset_class, qty, avg = world["broker"].holdings["ETH/USD"]
    world["broker"].holdings["ETH/USD"] = (asset_class, qty * 0.5, avg)  # something changed outside the engine
    orders_before = len(world["broker"].submitted)
    report = world["step"]({s: 0.01 for s in START})
    assert "ETH/USD" in report.mismatched
    assert any("ETH/USD" in p for p in report.problems)
    assert not any(o.symbol == "ETH/USD" for o in world["broker"].submitted[orders_before:])


def test_kill_switch_skips_decisions(world):
    KillSwitch(world["settings"].kill_switch_path).engage("test")
    report = world["engine"].run_once()
    assert world["broker"].submitted == []
    assert any("kill switch" in a for a in report.actions)


def test_engine_refuses_to_run_twice_at_once(world):
    import fcntl

    lock = (world["settings"].root / "data" / "engine.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        from trader.engine.live import EngineBusy

        with pytest.raises(EngineBusy):
            world["engine"].run_once()


def test_vault_keeps_review_notes_written_below_the_engine_block(world):
    world["engine"].run_once()
    note = next((world["settings"].root / "vault" / "Trades").rglob("*_S1_SPY_*.md"))
    note.write_text(note.read_text() + "\nMy own note about this trade.\n")
    world["step"]({s: 0.01 for s in START})
    assert "My own note about this trade." in note.read_text()


def test_stock_orders_queue_when_the_market_is_closed(world):
    broker = world["broker"]
    broker.fill_immediately = False
    broker.clock = broker.clock.model_copy(update={"is_open": False, "last_close": world["clock"].now - timedelta(minutes=30)})
    report = world["engine"].run_once()
    spy_orders = [o for o in broker.orders.values() if o.symbol == "SPY" and o.order_type is OrderType.MARKET]
    assert spy_orders and all(o.status is OrderStatus.SUBMITTED for o in spy_orders)
    assert not [p for p in report.problems if "SPY" in p]
    assert AssetClass.US_EQUITY  # queued orders fill at the next open via sync
