"""The backtester must not flatter results: next-bar fills, real costs, gap-through stops, no leverage, no peeking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import run_backtest
from trader.research.metrics import buy_and_hold, compute_metrics, max_drawdown
from trader.strategies.base import Holding, Strategy, Target

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)
COST = CostModel(half_spread_bps=5, slippage_bps=10, fee_bps=0, broker_fee_bps=25)  # 15 bps slip, 25 bps fee
LOOSE = SleeveLimits(max_position_pct_of_sleeve=100, risk_per_trade_pct=100, require_stop=False)


def make_bars(closes, opens=None, lows=None, start="2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, dtype=float)
    highs = np.maximum(opens, closes)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=idx)


@dataclass
class Scripted(Strategy):
    """Buys/sells on given bar numbers; used to test the engine, not a real strategy."""

    name: ClassVar[str] = "scripted"

    @property
    def warmup_bars(self) -> int:
        return 0

    def prepare(self, bars):
        for s in self.symbols:
            self.indicators[s] = pd.DataFrame({"close": bars[s]["close"],
                                               "n": np.arange(len(bars[s]), dtype=float)})

    def on_bar(self, date, holdings: dict[str, Holding]):
        out = {}
        for s in self.symbols:
            row = self.row(s, date)
            if row is None:
                continue
            n = int(row["n"])
            plan = self.params.get(s, {})
            if n in plan:
                weight, stop = plan[n]
                out[s] = Target(weight=weight, stop=stop, resize=self.params.get("resize", False))
        return out


def test_orders_fill_at_the_next_open_not_the_signal_close():
    bars = {"X": make_bars([100, 100, 100, 110, 120], opens=[100, 100, 100, 105, 115])}
    strat = Scripted(params={"X": {2: (1.0, None)}}, symbols=["X"])  # decide at close of bar 2 (price 100)
    res = run_backtest(strat, bars, 1000, LOOSE, {"X": NO_COST})
    # Filled at bar 3's open (105), not bar 2's close (100): 1000/100 = 10 units x 105 > cash -> capped by cash.
    qty = 1000 / 105
    assert res.equity.iloc[3] == pytest.approx(qty * 110)
    assert res.equity.iloc[2] == pytest.approx(1000)


def test_round_trip_pays_slippage_and_fees_both_ways():
    bars = {"X": make_bars([100] * 6)}
    strat = Scripted(params={"X": {0: (0.5, None), 2: (0.0, None)}}, symbols=["X"])
    res = run_backtest(strat, bars, 1000, LOOSE, {"X": COST})
    (trade,) = res.trades
    buy_px, sell_px = 100 * 1.0015, 100 * 0.9985
    qty = 500 / 100
    expected = qty * sell_px * (1 - 0.0025) - qty * buy_px * (1 + 0.0025)
    assert trade.pnl == pytest.approx(expected)
    assert res.equity.iloc[-1] == pytest.approx(1000 + expected)
    assert res.total_costs > 0


def test_stop_gapped_through_fills_at_the_worse_open():
    closes = [100, 100, 100, 100, 100]
    opens = [100, 100, 100, 85, 100]     # gaps down to 85 on bar 3
    lows = [100, 100, 100, 80, 100]
    bars = {"X": make_bars(closes, opens=opens, lows=lows)}
    limits = SleeveLimits(max_position_pct_of_sleeve=100, risk_per_trade_pct=100)
    strat = Scripted(params={"X": {0: (1.0, 95.0)}}, symbols=["X"])
    res = run_backtest(strat, bars, 1000, limits, {"X": NO_COST})
    (trade,) = res.trades
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(85)  # not the 95 stop price


def test_risk_per_trade_limits_position_size():
    bars = {"X": make_bars([100] * 5)}
    limits = SleeveLimits(max_position_pct_of_sleeve=100, risk_per_trade_pct=1, require_stop=True)
    strat = Scripted(params={"X": {0: (1.0, 90.0)}}, symbols=["X"])  # stop 10% away
    res = run_backtest(strat, bars, 10_000, limits, {"X": NO_COST})
    # 1% of 10k = $100 risk over a $10 stop distance = 10 units = $1,000 (10% of the sleeve)
    assert res.exposure.iloc[2] == pytest.approx(0.10)


def test_entry_without_stop_is_skipped_when_stops_are_required():
    bars = {"X": make_bars([100] * 5)}
    limits = SleeveLimits(require_stop=True)
    res = run_backtest(Scripted(params={"X": {0: (1.0, None)}}, symbols=["X"]), bars, 1000, limits, {"X": NO_COST})
    assert res.n_orders == 0


def test_never_uses_leverage():
    bars = {"A": make_bars([100] * 6), "B": make_bars([50] * 6)}
    strat = Scripted(params={"A": {0: (1.0, None)}, "B": {0: (1.0, None)}}, symbols=["A", "B"])
    res = run_backtest(strat, bars, 1000, LOOSE, {"A": NO_COST, "B": NO_COST})
    assert res.exposure.max() <= 1.0 + 1e-9


def test_rotation_can_switch_fully_from_one_asset_to_another():
    """Regression: the exit of the old holding must free room for the new one on the same day."""
    bars = {"A": make_bars([100] * 8), "B": make_bars([50] * 8)}
    strat = Scripted(params={"A": {0: (1.0, None), 3: (0.0, None)}, "B": {3: (1.0, None)}, "resize": True},
                     symbols=["A", "B"])
    res = run_backtest(strat, bars, 1000, LOOSE, {"A": NO_COST, "B": NO_COST})
    assert res.exposure.iloc[-1] == pytest.approx(1.0)
    assert [t.symbol for t in res.trades] == ["A"]


def test_future_prices_cannot_change_past_results():
    rng = np.random.default_rng(0)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, 300))
    base = make_bars(closes)
    altered = base.copy()
    altered.iloc[200:, :4] *= 1.5  # rewrite the future
    from trader.strategies.trend import TrendFollowing

    params = {"fast": 20, "slow": 50, "atr_mult": 3.0}
    limits = SleeveLimits()
    r1 = run_backtest(TrendFollowing(params=params, symbols=["X"]), {"X": base}, 10_000, limits, {"X": COST})
    r2 = run_backtest(TrendFollowing(params=params, symbols=["X"]), {"X": altered}, 10_000, limits, {"X": COST})
    pd.testing.assert_series_equal(r1.equity.iloc[:200], r2.equity.iloc[:200])


def test_metrics_on_a_known_curve():
    idx = pd.date_range("2020-01-01", periods=3, freq="365D")
    eq = pd.Series([100.0, 150.0, 75.0], index=idx)
    assert max_drawdown(eq) == pytest.approx(-0.5)
    m = compute_metrics(eq)
    assert m.total_return == pytest.approx(-0.25)
    assert m.max_drawdown == pytest.approx(-0.5)


def test_buy_and_hold_benchmark_pays_entry_cost():
    bars = {"X": make_bars([100, 110, 120])}
    bench = buy_and_hold(bars, ["X"], bars["X"].index, 1000, {"X": 40.0})
    assert bench.iloc[0] == pytest.approx(1000 * 0.996)
    assert bench.iloc[-1] == pytest.approx(1000 * 0.996 * 1.2)


def test_efficiency_ratio_tells_a_trend_from_chop():
    """A straight line scores ~1; a market that thrashes and ends where it started scores ~0."""
    import pandas as pd
    from trader.strategies.indicators import efficiency_ratio

    trend = pd.Series(range(1, 41), dtype=float)
    chop = pd.Series([10.0 + (1 if i % 2 else -1) for i in range(40)])
    assert efficiency_ratio(trend, 20).iloc[-1] == pytest.approx(1.0)
    assert efficiency_ratio(chop, 20).iloc[-1] < 0.2


def test_the_regime_filter_is_off_by_default():
    """It failed Gate 1, so it must not be silently on, and must stay out of the searched grid."""
    from trader.strategies.trend import TrendFollowing

    assert "min_er" not in TrendFollowing.param_grid
    assert float(TrendFollowing.default_params.get("min_er", 0.0)) == 0.0


def test_the_regime_filter_only_blocks_entries_never_forces_an_exit():
    """Exiting on a low reading would churn positions that are working."""
    import inspect

    from trader.strategies.trend import TrendFollowing

    source = inspect.getsource(TrendFollowing.on_bar)
    before_exit = source.split("elif not in_trend")[0]
    assert "trending_cleanly" in before_exit and "trending_cleanly" not in source.split("elif not in_trend")[1]
