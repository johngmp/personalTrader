"""Full engine runs against the simulated broker and synthetic daily bars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trader.brokers.sim import SimBroker
from trader.engine.executor import Executor
from trader.engine.live import Engine
from trader.engine.live import RunReport
from trader.models import AssetClass, OrderStatus, OrderType, Side
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
    # S8 trades 40 markets and this world prices four, so leaving it on would fail every test here with
    # a missing-bars error about a symbol none of them is testing. Same pattern as test_shadow.py.
    # test_s8_decides_on_its_whole_widened_basket covers S8 against a world built for it.
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
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


def test_dashboard_shows_balance_strategies_and_trades(world):
    world["engine"].run_once()
    world["step"]({s: 0.01 for s in START})
    page = (world["settings"].root / "data" / "dashboard.html").read_text()
    for section in ("Account balance", "Profit / loss", "Trading objectives", "Strategies", "Open positions",
                    "Trading history", "Recent orders", 'id="chart-data"'):
        assert section in page, section
    assert page.count("<span class='pill buy'>Long</span>") >= 7 * 2  # open trips listed on overview + history
    assert "S1 Trend following" in page and "B0 Buy-and-hold benchmark" in page


def test_local_server_serves_only_the_dashboard(world):
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    from trader.engine.serve import make_handler

    world["engine"].run_once()
    def build_engine(journal):  # like the real server: fresh engines on the request's own journal connection
        return [Engine(world["settings"], world["broker"], journal, world["bars"], clock=world["clock"])]

    server = HTTPServer(("127.0.0.1", 0), make_handler(world["settings"], build_engine))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Account balance" in page and 'content="60"' in page  # served page refreshes every minute
        for path in ("/data/journal.db", "/.env", "/../config/risk.yaml"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + path)
            assert err.value.code == 404
    finally:
        server.shutdown()


def test_stock_stops_only_move_while_the_market_is_open(world):
    world["engine"].run_once()
    before = _stops(world["broker"])["SPY"]
    broker = world["broker"]
    broker.clock = broker.clock.model_copy(update={"is_open": False})
    world["step"]({s: 0.03 for s in START})               # new high after the close
    assert _stops(broker)["SPY"] == before                 # not moved while closed...
    assert _stops(broker)["BTC/USD"] > 0                   # (crypto trails any time)
    broker.clock = broker.clock.model_copy(update={"is_open": True})
    world["step"](None, hours=0.25)
    assert _stops(broker)["SPY"] > before                  # ...moved on the first run after the open
    assert sum(1 for o in broker.get_open_orders() if o.symbol == "SPY") == 1


def test_alpaca_replaced_status_is_final():
    from trader.brokers.alpaca import _STATUS_MAP
    from trader.models import OrderStatus

    assert _STATUS_MAP["replaced"] is OrderStatus.CANCELED
    assert _STATUS_MAP["pending_replace"] is OrderStatus.SUBMITTED


# --- S8's widened 40-market basket (2026-10-03) ---------------------------------------------------

def _wide_world(settings, tmp_path, seed=7):
    """S8 enabled on 40 markets, every one of them trending up, against the simulated broker."""
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": True})
    for sid in ("B0", "S1"):
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": False})
    symbols = settings.sleeves["S8"].symbols
    idx = pd.bdate_range("2025-01-01", periods=260)
    rng = np.random.default_rng(seed)
    # A steady uptrend in everything, so every symbol has a signal and nothing is skipped for want of
    # one. This is about covering the basket, not about picking winners.
    full = {}
    for symbol in symbols:
        close = 100.0 * np.cumprod(1 + 0.002 + rng.normal(0, 0.004, 260))
        full[symbol] = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                                     "close": close, "volume": 1.0}, index=idx)

    class WideBars:
        def completed_bars(self, symbol, asset_class, now, market):
            return full[symbol]

    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for symbol, df in full.items():
        broker.set_price(symbol, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, WideBars(), notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    return engine, journal, broker, full, symbols


def test_s8_decides_on_its_whole_widened_basket(settings, tmp_path):
    """S8 went from 19 markets to 40 on 2026-10-03, for speed of EVIDENCE rather than return.

    Measured over 31 years, 19 markets complete 7.0 trades a month and 40 complete 15.1, which turns the
    50 trades Gate 2 wants from 7.2 months into 3.3. That only holds if the engine really decides on all
    40, so this asserts the count rather than trusting the config: the first dry run after the change
    silently decided on 19 of them, because journal rows left behind by a half-finished rename made the
    other 21 look already-decided.
    """
    settings.risk.account.max_orders_per_day = 1000     # isolate this from the budget defect below
    engine, journal, _broker, full, symbols = _wide_world(settings, tmp_path)
    assert len(symbols) == 40, "this test is pinned to the widened basket"

    report = engine.run_once()
    assert not report.problems, report.problems
    decided = {s for s in symbols if journal.last_decided_bar("S8", s) is not None}
    assert decided == set(symbols), f"did not decide on {sorted(set(symbols) - decided)}"

    # No leverage: a sleeve is capped at its own capital, whatever the gate was asked for.
    held = journal.sleeve_holdings("S8")
    assert held, "S8 bought nothing at all"
    invested = sum(q * float(full[s]["close"].iloc[-1]) for s, q in held.items())
    assert invested <= settings.sleeves["S8"].capital_usd * 1.001, f"S8 levered to ${invested:,.0f}"
    journal.close()


def test_too_small_an_order_budget_starves_the_stops(settings, tmp_path):
    """The failure mode the budget exists to prevent, pinned at a deliberately low cap.

    An entry and its protective stop are two orders. The entries go first, so when the budget runs out
    it is the STOPS that are refused -- open positions with nothing protecting them for the rest of the
    day, which is the single failure this system exists to prevent. The budget is account-wide and
    shadow orders count against it too, so a research sleeve can starve B0's and S1's stops as well.

    This used to assert the SHIPPED cap was 60 and then show it starving. That made the test a record
    of one day's config rather than of the behaviour: raising the cap to 150 on 2026-10-08 would have
    failed it, which says nothing about whether the engine is sound. It now sets its own low cap, so it
    keeps describing the danger whatever risk.yaml happens to say.
    """
    settings.risk.account.max_orders_per_day = 60

    engine, journal, _broker, _full, symbols = _wide_world(settings, tmp_path)
    report = engine.run_once()

    starved = [p for p in report.problems if "stop" in p and "max_orders_per_day" in p]
    assert starved, f"expected starved stops at a 60-order budget, got {report.problems}"
    # The damage is specific: positions are open and some of them have no stop at the broker.
    held = set(journal.sleeve_holdings("S8"))
    stopped = {o.symbol for o in _broker.get_open_orders()
               if o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)}
    assert held - stopped, "budget was exhausted yet every position still had a stop"
    journal.close()


def test_the_shipped_budget_covers_a_cold_start_of_everything_enabled(settings):
    """The other half of the starvation test: that the real cap is big enough for the real config.

    Worst case is every enabled sleeve entering every symbol it can on one day, each needing an entry
    and a protective stop. That is rare -- it needs most markets trending at once -- which is exactly
    why it cannot be left to chance: it would pass every day until the day the strategies are busiest,
    and then refuse the stops.
    """
    worst = 0
    for sleeve in settings.sleeves.values():
        if sleeve.enabled:
            worst += 2 * len(sleeve.symbols)        # an entry and a stop for each
    cap = settings.risk.account.max_orders_per_day
    assert cap >= worst, (
        f"max_orders_per_day is {cap} but a cold start of the enabled sleeves could want {worst}. "
        "The overflow lands on the STOPS, leaving positions unprotected.")


def test_a_market_the_sleeve_cannot_afford_is_raised_not_swallowed(settings, tmp_path):
    """The silent failure that nearly went unnoticed on 2026-10-08.

    S8 wanted SPY, QQQ and MDY. Each was allocated $544 against share prices of $777/$758/$664, all
    three floored to zero whole shares, and NOTHING was ordered or reported -- while the Gate 1
    backtest held all three, because it sized fractionally. The sleeve looked like it was running the
    tested basket while missing its core equity exposure.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    # A sleeve far too small to buy a single share of anything it wants.
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"enabled": True, "capital_usd": 50.0})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    report = engine.run_once()

    unaffordable = [p for p in report.problems if "not affordable" in p]
    assert unaffordable, f"an unaffordable market must be reported, got {report.problems}"
    assert "Gate 1" in unaffordable[0], "say what the consequence is, not just that it happened"
    journal.close()


def test_the_unaffordable_alert_fires_once_a_day_not_every_run(settings, tmp_path):
    """The engine runs every fifteen minutes and the same symbols stay unaffordable all day. Alerting
    on each run is how a real warning gets trained into background noise."""
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"enabled": True, "capital_usd": 50.0})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)

    def run():
        engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                        executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                        fill_wait_s=0)
        return engine.run_once()

    first = [p for p in run().problems if "not affordable" in p]
    second = [p for p in run().problems if "not affordable" in p]
    assert first, "the first run of the day must say so"
    assert not second, "the second run must not repeat it"
    journal.close()


def test_a_buy_is_not_sent_when_another_sleeve_has_a_resting_stop_on_it(settings, tmp_path):
    """The wash-trade conflict, measured live on 2026-10-08.

    The broker sees ONE account; sleeves are our abstraction, not its. Alpaca refuses a buy while any
    opposite-side order rests on the same symbol, and it refuses a plain LIMIT order too -- so the
    error's advice to "use complex orders" means brackets and OCO, not an order type we can substitute.
    The conflict cannot be traded through, only avoided, and cancelling the other sleeve's stop to make
    room is not avoiding it: it leaves a real position unprotected to place a new one.

    So the engine must not send an order the broker is always going to reject, and must say why.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    engine.run_once()                                   # B0 and S1 take their positions and stops

    # Another sleeve now wants a symbol S1 already holds with a resting stop.
    held = [s for s in journal.sleeve_holdings("S1") if journal.position_state("S1", s)]
    assert held, "S1 should hold something with a state after the first run"
    symbol = held[0]
    blocker = engine._blocking_stop("S8", symbol)
    assert blocker == "S1", f"expected S1 to be named as the blocker for {symbol}, got {blocker}"

    # And a sleeve is never blocked by its own stop.
    assert engine._blocking_stop("S1", symbol) is None, "a sleeve must not block itself"
    journal.close()


def test_a_wash_blocked_market_is_reported_as_a_problem(settings, tmp_path):
    """Unlike the affordability problem, no amount of capital fixes this one, so it has to be said."""
    from trader.engine.live import RunReport
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    report = RunReport(started=clock.now)
    report.wash_blocked = ["SPY (held by S1)", "QQQ (held by S1)"]
    engine._report_wash_blocked(report)
    assert report.problems and "wash trade" in report.problems[0]
    assert "More capital will not fix" in report.problems[0]

    # Once a day, not once every fifteen minutes.
    again = RunReport(started=clock.now)
    again.wash_blocked = ["SPY (held by S1)"]
    engine._report_wash_blocked(again)
    assert not again.problems, "the same conflict must not alert on every run"
    journal.close()


def test_a_retiring_sleeve_sells_what_it_holds_through_the_normal_exit_path(settings, tmp_path):
    """Winding a sleeve down must not mean abandoning it.

    Flipping `enabled` straight to false simply stops the sleeve being managed: its positions would sit
    at the broker with stops that never trail and nothing watching them. `retiring` makes it want
    nothing and sell through the ordinary exit, so stops are cancelled and every close is journalled.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")

    def engine_now():
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
        return Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                      executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                      fill_wait_s=0)

    engine_now().run_once()
    assert journal.sleeve_holdings("S1"), "S1 should have taken positions on the first run"

    # Retire it, move a day forward so there is a new bar to decide on, and run again.
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"retiring": True})
    clock.now += timedelta(hours=24)
    bars.add_day({})
    engine_now().run_once()

    assert not journal.sleeve_holdings("S1"), "a retiring sleeve must end up flat"
    stops = {o.symbol for o in broker.get_open_orders()
             if o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)}
    assert not (set(journal.sleeve_holdings("S1")) & stops), "no stop may outlive its position"
    sells = [f for f in journal.fills("S1") if f["side"] == "sell"]
    assert sells, "the closes must be journalled as ordinary fills"
    assert any("retiring" in (f["reason"] or "") for f in sells), "and say why they happened"
    journal.close()


def test_a_sleeve_that_is_not_retiring_still_trades_normally(settings, tmp_path):
    """The flag must do nothing at all when it is off."""
    assert settings.sleeves["S1"].retiring is False
    assert settings.sleeves["S8"].retiring is False


def test_a_retiring_sleeve_does_not_raise_a_false_stop_alarm(settings, tmp_path):
    """"Could not place stop" is this system's most alarming message. It must not fire when nothing
    is wrong.

    A retiring sleeve cancels its stop and places a market sell. The protective-stop step would then try
    to add a stop back, which the duplicate-order guard refuses -- two sells of the same symbol inside a
    minute -- and the refusal was reported as a problem. The exit order IS the protection at that point.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")

    def engine_now():
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
        return Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                      executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                      fill_wait_s=0)

    engine_now().run_once()
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"retiring": True})
    clock.now += timedelta(hours=24)
    bars.add_day({})
    report = engine_now().run_once()

    stop_alarms = [p for p in report.problems if "stop" in p.lower()]
    assert not stop_alarms, f"a wind-down must not report a stop failure: {stop_alarms}"
    journal.close()


def test_retirement_does_not_wait_for_a_new_daily_bar(settings, tmp_path):
    """Deciding once per daily bar is right for a strategy and wrong for a wind-down: having already
    decided today would leave the positions open until tomorrow, when the point is to be out of them."""
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")

    def engine_now():
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
        return Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                      executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                      fill_wait_s=0)

    engine_now().run_once()
    assert journal.sleeve_holdings("S1")
    # Retire and run again on the SAME bar it has already decided on.
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"retiring": True})
    engine_now().run_once()
    assert not journal.sleeve_holdings("S1"), "it must exit on the same bar, not wait for tomorrow"
    journal.close()


def test_a_cancelled_stop_is_still_tracked_until_the_broker_confirms(settings, tmp_path):
    """Asking to cancel is not the same as it being cancelled.

    An equity stop cancelled while the market is shut sits at "pending_cancel" until the open, which the
    adapter rightly maps to an OPEN status. The engine used to write CANCELED into the journal itself,
    which took the order out of open_orders() -- so sync_open_orders stopped checking it, and if the
    cancel were refused, or the stop filled first, nothing would ever notice. Observed live on
    2026-10-08 retiring S1: our journal said "canceled" while Alpaca said "pending_cancel" on SPY and
    QQQ both.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    engine.run_once()

    held = [s for s in journal.sleeve_holdings("S1") if journal.position_state("S1", s)]
    symbol = next(s for s in held if journal.position_state("S1", s)["stop_order_id"])
    state = journal.position_state("S1", symbol)
    stop_id = state["stop_order_id"]

    engine._cancel_stop(state)

    row = journal.order(stop_id)
    assert row["status"] != OrderStatus.CANCELED.value, (
        "the engine must not record a cancellation the broker has not confirmed")
    # The sleeve no longer counts on that stop, but the ORDER stays tracked so the next sync
    # resolves it to whatever the broker actually did.
    assert journal.position_state("S1", symbol)["stop_order_id"] is None
    assert any(r["client_order_id"] == stop_id for r in journal.open_orders("alpaca")), \
        "a pending cancel must stay in open_orders so sync keeps watching it"
    journal.close()


def test_a_retirement_completes_itself_once_the_sleeve_is_flat(settings, tmp_path):
    """Winding down should finish on its own, not wait for someone to remember.

    Once the sleeve holds nothing and has nothing queued it is done: `retiring` means it wants nothing,
    so it cannot enter again. It stops being processed and says so once. The config flag is left alone
    deliberately -- an engine that rewrites its own configuration unattended is a surprise nobody wants
    at 3am -- but the behaviour from here is exactly that of a disabled sleeve.
    """
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    sent = []

    class Spy(NullNotifier):
        def send(self, text):
            sent.append(text)

    def engine_now():
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
        return Engine(settings, broker, journal, bars, notifier=Spy(), clock=clock,
                      executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                      fill_wait_s=0)

    engine_now().run_once()
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"retiring": True})
    clock.now += timedelta(hours=24)
    bars.add_day({})
    engine_now().run_once()                              # sells everything
    assert not journal.sleeve_holdings("S1")

    sent.clear()
    clock.now += timedelta(hours=24)
    bars.add_day({})
    report = engine_now().run_once()                     # first run after it is flat
    assert any("retirement complete" in a for a in report.actions), report.actions
    assert any("retired" in m.lower() for m in sent), "it should say so once"

    # ...and only once, however many times the engine runs afterwards.
    sent.clear()
    clock.now += timedelta(hours=24)
    bars.add_day({})
    again = engine_now().run_once()
    assert not any("retirement complete" in a for a in again.actions)
    assert not sent, "a finished retirement must not keep announcing itself"
    journal.close()


def test_a_retiring_sleeve_is_not_called_finished_while_anything_is_outstanding(settings, tmp_path):
    """Two ways to still be busy, and both must block completion.

    Holding positions is the obvious one. The subtle one is an equity sell placed while the market was
    shut: the journal can already show no holdings while the order sits queued until the open, and
    calling that finished would stop the engine watching it.
    """
    from trader.engine.live import RunReport
    from trader.journal.db import Journal

    clock = Clock()
    journal = Journal(tmp_path / "journal.db", clock=clock)
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    bars = FakeBars()
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None),
                    fill_wait_s=0)
    engine.run_once()
    retiring = settings.sleeves["S1"].model_copy(update={"retiring": True})

    assert journal.sleeve_holdings("S1"), "the first run should have taken positions"
    assert engine._retirement_done(retiring, RunReport(started=clock.now)) is False, \
        "a sleeve still holding positions is not finished"

    # A sleeve that is NOT retiring is never 'done', whatever it holds.
    assert engine._retirement_done(settings.sleeves["S1"], RunReport(started=clock.now)) is False


# --- the duplicate exit ---------------------------------------------------------------------------

def _queue_sell(journal, sleeve_id, symbol, qty, order_type):
    """Put a sell at the broker that has not filled: queued against a shut market."""
    oid = f"queued-{sleeve_id}-{symbol}-{order_type}"
    journal.conn.execute(
        """INSERT INTO orders (client_order_id, created_at, sleeve_id, symbol, asset_class, side, qty,
           order_type, status, broker, broker_order_id, filled_qty, updated_at)
           VALUES (?,?,?,?,'us_equity','sell',?,?,?,'alpaca',?,0,?)""",
        (oid, "2026-10-08T03:47:50+00:00", sleeve_id, symbol, qty, order_type,
         OrderStatus.SUBMITTED.value, f"broker-{oid}", "2026-10-08T03:47:50+00:00"))
    journal.conn.commit()
    return oid


def test_a_retiring_sleeve_does_not_send_the_same_sell_every_run(world):
    """The wind-down re-decides every cycle, and its first exit is still queued.

    S1 asked to sell 3 SPY at 03:47 while the market was shut, then asked again at 04:03, 04:19,
    04:35 ... Alpaca reserves the shares behind the queued order, so every repeat came back
    "insufficient qty available for order (requested: 3, available: 1)" -- 8 errors overnight on
    2026-10-08, each one a real broker rejection in the log.
    """
    world["engine"].run_once()
    assert world["journal"].sleeve_holdings("S1").get("SPY")

    held = world["journal"].sleeve_holdings("S1")["SPY"]
    _queue_sell(world["journal"], "S1", "SPY", held, OrderType.MARKET.value)

    before = len(world["broker"].submitted)
    world["engine"]._exit("S1", "SPY", "sleeve retiring", RunReport(started=datetime(2026, 10, 8, 3, 47, tzinfo=timezone.utc)))
    assert len(world["broker"].submitted) == before, \
        "the exit was already on its way; sending it again can only be rejected"


def test_a_resting_stop_does_not_block_the_exit_it_is_being_replaced_by(world):
    """The trap in the guard above, and the reason a broader version of it was wrong.

    `_exit` cancels the protective stop and then sells. The cancel is deliberately not journalled
    until the broker confirms it, so at the moment of selling the stop still reads as an open sell for
    the full position. A guard that counted it would subtract everything and send nothing -- leaving a
    position that was supposed to close sitting there with no stop and no exit. Much worse than the
    log noise it was written to silence.
    """
    world["engine"].run_once()
    held = world["journal"].sleeve_holdings("S1")["SPY"]
    _queue_sell(world["journal"], "S1", "SPY", held, OrderType.STOP.value)

    before = len(world["broker"].submitted)
    world["engine"]._exit("S1", "SPY", "trend ended", RunReport(started=datetime(2026, 10, 8, 3, 47, tzinfo=timezone.utc)))
    sent = world["broker"].submitted[before:]
    assert [o.symbol for o in sent] == ["SPY"], "a stop standing in the way must not cancel the exit"
    assert sent[0].side is Side.SELL


# --- shadow alerts must not read as instructions ---------------------------------------------------

def test_a_shadow_fill_says_it_is_not_a_trade_to_place():
    """The only enabled shadow sleeve runs the FX rules that FAILED Gate 1 at -0.184 out of sample.

    Its message read "S9 Bought 60000 EUR/USD @ 1.11894" with a stop and a dollar risk beside it --
    identical in shape to a real fill from the paper account, and the most actionable-looking thing this
    system sends. The dashboard states the verdict plainly. The phone did not state it at all, so the
    one alert that should carry the loudest caveat carried none.
    """
    from trader.engine.live import _fill_text
    from trader.models import Fill

    fill = Fill(sleeve_id="S9", symbol="EUR/USD", side=Side.BUY, qty=60_000, price=1.11894,
                ts=datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc), client_order_id="x",
                broker_order_id="y", cost_haircut_usd=0.0)

    shadow = _fill_text(fill, "trend up", AssetClass.FOREX, stop=1.11456, shadow=True)
    assert "would have bought" in shadow, "a simulated fill must not claim a purchase happened"
    assert "NOT a recommendation" in shadow and "Do not place this trade" in shadow
    assert "failed Gate 1" in shadow, "the reason not to follow it belongs in the message"
    assert "would risk" in shadow

    real = _fill_text(fill, "trend up", AssetClass.FOREX, stop=1.11456)
    assert real.startswith("🟢") and "Bought 60000" in real
    assert "Shadow test" not in real, "a real fill must not be hedged with a caveat that does not apply"


def test_one_rule_decides_who_gets_messaged():
    """Three places asked this as `name != "shadow"`, which matches the name EXACTLY.

    So every shadow broker except the one literally called "shadow" was messaged, regardless of
    HAND_EXECUTED. It agreed with _should_notify only by coincidence -- shadow_fx happens to be
    hand-executed -- and a second shadow broker would have been texted to the phone while the method
    that supposedly decides said to stay quiet.
    """
    import inspect

    from trader.engine import live

    # The code form, not the prose: _should_notify's own docstring quotes the bug it replaced, and
    # matching that was a false positive this test produced on its first run.
    code = "".join(line.split("#")[0] for line in inspect.getsource(live).splitlines())
    assert 'self.broker.name != "shadow"' not in code, "the exact-match test IS the bug; ask _should_notify"
    assert code.count("def _should_notify") == 1


def test_a_shadow_stop_move_does_not_tell_you_to_move_a_real_stop():
    """This path is reached only for a hand-executed broker, and the only one today is the FX shadow
    running rules that failed Gate 1 -- so every message it has ever sent was for that strategy, and it
    ended with a flat instruction to go and move a stop. Telling someone to adjust a real position on
    behalf of rules that lost to T-bills is the most direct way this system could cost money."""
    from trader.engine.live import _stop_move_text

    shadow = _stop_move_text("S9", "EUR/USD", 1.11456, 1.11600, AssetClass.FOREX, shadow=True)
    assert "Move your stop" not in shadow, "a simulated position must not be an instruction"
    assert "would move" in shadow and "do not move a real stop" in shadow
    assert "failed Gate 1" in shadow

    # A real hand-executed broker, if one is ever configured, still gets the instruction it needs.
    real = _stop_move_text("S4", "EUR/USD", 1.11456, 1.11600, AssetClass.FOREX)
    assert "Move your stop if you are following this by hand." in real
    assert "Shadow test" not in real


def test_the_daily_summary_does_not_pass_a_simulation_off_as_an_account(settings, tmp_path):
    """This is the message most likely to be read and least likely to be questioned.

    A shadow engine was listed as "<name> paper account $X" directly beneath the real Alpaca one, with
    its sleeve's return beside the real sleeves'. A shadow holds no account and placed none of those
    trades, and the only enabled one runs rules that failed Gate 1 -- so a glance down the list read a
    simulation of a losing strategy as money that had been made.
    """
    from trader.engine.live import status_text
    from trader.journal.db import Journal

    journal = Journal(tmp_path / "j.db")
    real = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, FakeBars(),
                  notifier=NullNotifier())
    shadow = Engine(settings, SimBroker(cash=10_000, name="shadow_fx"), journal, FakeBars(),
                    notifier=NullNotifier())
    text = status_text([real, shadow])

    assert "alpaca paper account" in text
    assert "shadow_fx paper account" not in text, "a shadow holds no account"
    assert "simulated, NOT an account" in text
    journal.close()


def test_a_shadow_never_claims_a_trade_happened(settings, tmp_path):
    """Three more messages asserted real events for a sleeve that places nothing: a position "closed",
    an order "Queued ... fills when the market opens", and a retirement that "sold" its positions."""
    import inspect

    from trader.engine import live

    source = inspect.getsource(live)
    # Each of the three now has a shadow-aware form beside it.
    assert "would have closed its" in source
    assert "would be queued for the next open" in source
    assert 'sold = "would have sold" if self._is_shadow() else "sold"' in source
    # And every one of them is reached through the single notify rule.
    assert source.count("_should_notify()") >= 5


def test_a_wide_stop_shrinks_the_entry_instead_of_cancelling_it(world):
    """The gate refused these outright, so the sleeve held nothing.

    The allocation comes from the strategy's target weight; the gate then judges it by how far the stop
    is, and nothing connected the two. On 2026-10-08 S1 wanted $1,211 of ETH/USD behind a stop 8.3%
    below, which risks $100.67 against a $100 budget, and the order was rejected over 67 cents -- leaving
    the sleeve without a holding the backtest had. The backtest applies no per-trade risk cap at all, so
    holding 97% of the tested position is a far smaller divergence than holding none of it.
    """
    report = RunReport(started=datetime(2026, 10, 8, 0, 8, tzinfo=timezone.utc))
    price, stop = 2571.72, 2357.9491707013276           # the real numbers from that refusal
    limits = world["settings"].sleeve_limits("S1")
    budget = world["settings"].sleeves["S1"].capital_usd * limits.risk_per_trade_pct / 100

    fitted = world["engine"]._fit_to_risk("S1", "ETH/USD", 1211.50, price, stop, report)
    assert fitted < 1211.50, "an entry whose stop breaches the budget must shrink"
    assert fitted * (1 - stop / price) <= budget, "and must then fit inside it"
    assert fitted > 1211.50 * 0.9, "shrinking it to nothing would defeat the point"
    assert any("sized ETH/USD down" in a for a in report.actions), "a resize must be visible, not silent"


def test_an_entry_that_already_fits_is_left_exactly_alone(world):
    """Trimming every order would make the whole sleeve smaller than the one that was tested."""
    report = RunReport(started=datetime(2026, 10, 8, tzinfo=timezone.utc))
    # A 2% stop on a $1,000 entry risks $20, far inside S1's $100 budget.
    assert world["engine"]._fit_to_risk("S1", "ETH/USD", 1_000.0, 100.0, 98.0, report) == 1_000.0
    assert report.actions == [], "an order that fits must not be announced as resized"


def test_a_sleeve_with_no_stop_rule_is_not_resized(world):
    """B0 holds the benchmark with no stops, so there is nothing to size against."""
    report = RunReport(started=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert world["engine"]._fit_to_risk("B0", "SPY", 5_000.0, 500.0, None, report) == 5_000.0
    assert report.actions == []


def test_the_entry_path_actually_applies_the_risk_fit(world):
    """The wiring, not the helper.

    The first version of the test above called _fit_to_risk directly and passed with the call site in
    _enter deleted -- the same mistake as the forex clock the executor never handed the gate. A unit test
    on a helper says nothing about whether anything calls it.
    """
    report = RunReport(started=datetime(2026, 10, 8, 0, 8, tzinfo=timezone.utc))
    broker = world["broker"]
    price, stop = 2571.72, 2357.9491707013276
    broker.set_price("ETH/USD", price, datetime(2026, 10, 8, 0, 8, tzinfo=timezone.utc))

    before = len(broker.submitted)
    world["engine"]._enter("S1", "ETH/USD", 1211.50, price, stop, "trend up", report)
    sent = broker.submitted[before:]

    assert len(sent) == 1, "the entry must still be placed, just smaller"
    order = sent[0]
    assert order.notional is not None and order.notional < 1211.50
    budget = world["settings"].sleeves["S1"].capital_usd * \
        world["settings"].sleeve_limits("S1").risk_per_trade_pct / 100
    assert order.notional * (1 - stop / price) <= budget
