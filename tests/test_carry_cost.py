"""Holding a position overnight costs money (forex swap). A backtest that ignores it flatters slow strategies."""

from __future__ import annotations

import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import BacktestResult, run_backtest
from trader.strategies.base import Strategy, Target

SYMBOL = "EUR/USD"
PRICE = 100.0
STOP = 90.0          # a stop is required for risk sizing, so the position gets a size at all


class BuyAndHold(Strategy):
    """Buys on the first bar and never sells, so the only variable left is the cost of holding."""

    name = "buy-and-hold"

    @property
    def warmup_bars(self) -> int:
        return 0

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        return None

    def on_bar(self, date, holdings) -> dict[str, Target]:
        return {s: Target(weight=1.0, stop=STOP, reason="hold") for s in self.symbols}


def flat_bars(days: int) -> dict[str, pd.DataFrame]:
    """A market that never moves, so every difference in the result is a cost."""
    index = pd.bdate_range("2020-01-01", periods=days)
    return {SYMBOL: pd.DataFrame({"open": PRICE, "high": PRICE, "low": PRICE, "close": PRICE, "volume": 1e9},
                                 index=index)}


def backtest(carry_bps_per_day: float, days: int = 60) -> BacktestResult:
    costs = {SYMBOL: CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0,
                               carry_bps_per_day=carry_bps_per_day)}
    return run_backtest(BuyAndHold(params={}, symbols=[SYMBOL]), flat_bars(days), capital=10_000,
                        limits=SleeveLimits(), costs=costs)


def final_equity(carry_bps_per_day: float, days: int = 60) -> float:
    return float(backtest(carry_bps_per_day, days).equity.iloc[-1])


def test_the_position_actually_opens_so_these_tests_mean_something():
    """Guard against the whole file passing vacuously because nothing was ever bought."""
    assert backtest(0.0).n_orders > 0
    assert backtest(0.0).exposure.iloc[-1] > 0


def test_a_flat_market_with_no_costs_leaves_equity_untouched():
    assert final_equity(carry_bps_per_day=0.0) == pytest.approx(10_000, abs=1.0)


def test_holding_overnight_costs_money_even_when_the_price_never_moves():
    """The whole point: a position that goes nowhere still bleeds if you pay to carry it."""
    assert final_equity(carry_bps_per_day=1.0) < final_equity(carry_bps_per_day=0.0) - 0.1


def test_carrying_longer_costs_more():
    assert backtest(1.0, days=120).total_costs > backtest(1.0, days=30).total_costs


def test_the_charge_matches_rate_times_value_times_days():
    result = backtest(1.0)
    held = result.exposure * result.equity          # dollars on the table each day
    expected = float(held.sum()) * 0.0001           # 1 bp of that, every day
    assert result.total_costs == pytest.approx(expected, rel=0.02)


def test_carry_is_counted_as_a_cost_not_hidden_in_the_equity_curve():
    result = backtest(2.0)
    assert result.total_costs > 0
    assert result.total_costs == pytest.approx(10_000 - float(result.equity.iloc[-1]), rel=0.02)


def test_carry_defaults_to_zero_so_existing_configs_are_unchanged():
    assert CostModel(half_spread_bps=1, slippage_bps=1, fee_bps=0).carry_bps_per_day == 0.0
