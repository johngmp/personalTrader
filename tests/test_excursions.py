"""MAE/MFE: how far a trade ran against you and for you while it was open.

The P&L alone cannot say whether a stop is in the right place. These are measured from each bar's HIGH
and LOW, not its close, because a stop is hit intraday.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import run_backtest
from trader.strategies.base import Strategy, Target

SYMBOL = "TEST"
NO_COST = {SYMBOL: CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)}


class BuyThenExit(Strategy):
    """Buys on the first bar with a stop at 90, then goes flat after `hold_bars`.

    It exits by CHOICE so the tests can produce winners. A stopped-out trade has an MAE of exactly 1R by
    definition, which makes it useless for telling a calm winner from a nervous one.
    """

    name = "buy-then-exit"
    stop_price: float = 90.0
    hold_bars: int = 3

    @property
    def warmup_bars(self) -> int:
        return 0

    def prepare(self, bars):
        return None

    def on_bar(self, date, holdings):
        out = {}
        for sym in self.symbols:
            held = holdings.get(sym)
            if held is not None and held.bars_held >= self.hold_bars:
                out[sym] = Target(weight=0.0, reason="time exit")
            else:
                out[sym] = Target(weight=1.0, stop=self.stop_price, reason="hold")
        return out


def bars_from(rows: list[tuple[float, float, float, float]]) -> dict[str, pd.DataFrame]:
    """rows of (open, high, low, close)."""
    idx = pd.bdate_range("2020-01-01", periods=len(rows))
    return {SYMBOL: pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1e9}, index=idx)}


def run(rows):
    strat = BuyThenExit(params={}, symbols=[SYMBOL])
    return run_backtest(strat, bars_from(rows), capital=100_000, limits=SleeveLimits(), costs=NO_COST)


def test_a_trade_records_both_excursions():
    # enter ~100 (stop 90, so 1R = 10 a share), dip to 95, rally to 130, then stop out
    rows = [(100, 101, 99, 100)] * 2 + [(100, 100, 95, 97), (100, 130, 100, 128), (100, 100, 85, 88)]
    trades = run(rows).trades
    assert trades, "the position never closed, so there is nothing to measure"
    t = trades[0]
    assert t.mae_r is not None and t.mfe_r is not None
    assert t.mae_r > 0 and t.mfe_r > 0


def test_excursions_use_the_intraday_range_not_the_close():
    """A day that dipped to 92 and closed at 100 did go against you, even though the close hides it."""
    deep = run([(100, 101, 99, 100)] * 2 + [(100, 100, 92, 100)] + [(100, 101, 99, 100)] * 3).trades[0]
    shallow = run([(100, 101, 99, 100)] * 2 + [(100, 100, 99, 100)] + [(100, 101, 99, 100)] * 3).trades[0]
    assert deep.mae_r > shallow.mae_r


def test_a_winner_that_never_wobbled_has_a_small_mae():
    """This is the number that says whether a stop is too tight: heat a winner had to survive."""
    t = run([(100, 101, 99, 100)] * 2 + [(100, 120, 100, 119)] + [(119, 121, 118, 120)] * 3).trades[0]
    assert t.pnl > 0 and t.mae_r < 0.3


def test_mfe_captures_profit_that_was_handed_back():
    """A loser that was once far ahead is a different problem from one that never worked at all."""
    gave_back = run([(100, 101, 99, 100)] * 2 + [(100, 140, 100, 139), (100, 100, 85, 85)]).trades[0]
    never_worked = run([(100, 101, 99, 100)] * 2 + [(100, 101, 100, 100), (100, 100, 85, 85)]).trades[0]
    assert gave_back.pnl < 0 and never_worked.pnl < 0
    assert gave_back.mfe_r > 2 * never_worked.mfe_r
    assert gave_back.mae_r == pytest.approx(never_worked.mae_r)   # both stopped out: MAE is 1R either way


def test_excursions_are_none_without_a_stop_to_measure_against():
    """R is meaningless with no initial risk, and a fabricated number would be worse than none."""
    class NoStop(BuyThenExit):
        def on_bar(self, date, holdings):
            return {s: Target(weight=1.0, stop=None, reason="no stop") for s in self.symbols}

    strat = NoStop(params={}, symbols=[SYMBOL])
    result = run_backtest(strat, bars_from([(100, 101, 99, 100)] * 5), capital=100_000,
                          limits=SleeveLimits(), costs=NO_COST)
    for t in result.trades:
        assert t.mae_r is None and t.mfe_r is None


def test_the_r_multiple_still_works_alongside_them():
    t = run([(100, 101, 99, 100)] * 2 + [(100, 130, 100, 128)] + [(128, 131, 127, 129)] * 3).trades[0]
    assert t.r_multiple is not None
    assert t.mfe_r >= t.r_multiple      # you can never keep more than the best it ever showed
