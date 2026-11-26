"""S2: cross-asset momentum rotation, on a small dataset where the winner at each rebalance is obvious.

Same style as test_backtest.py's S1/S8 tests: build known bars, run the real backtester, check what
actually got bought.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import run_backtest
from trader.strategies.rotation import Rotation

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)
LOOSE = SleeveLimits(max_position_pct_of_sleeve=100, risk_per_trade_pct=100, require_stop=False)

# Spans two month boundaries (1 Feb and 1 Mar) with only a handful of bars, so lookback=2 is already
# enough history by the first one.
DATES = pd.to_datetime([
    "2024-01-29", "2024-01-30", "2024-01-31",                                            # Jan
    "2024-02-01", "2024-02-02", "2024-02-05", "2024-02-06", "2024-02-07", "2024-02-08",   # Feb
    "2024-03-01", "2024-03-04",                                                           # Mar
])


def bars(closes: list[float]) -> pd.DataFrame:
    s = pd.Series(closes, index=DATES, dtype=float)
    return pd.DataFrame({"open": s, "high": s, "low": s, "close": s, "volume": 1.0})


def _run():
    data = {
        # +10% over the lookback window by 1 Feb, then crashes -18% into 1 Mar.
        "A":   bars([100, 100, 100, 110, 110, 110, 110, 110, 110, 90, 90]),
        # +2% by 1 Feb (beats cash but loses to A), flat (0%) into 1 Mar (ties cash, does not beat it).
        "B":   bars([100, 100, 100, 102, 102, 102, 102, 102, 102, 102, 102]),
        "BIL": bars([100.0] * 11),   # cash proxy: never moves
    }
    strat = Rotation(params={"lookback": 2, "top_n": 1}, symbols=["A", "B", "BIL"])
    return run_backtest(strat, data, 10_000, LOOSE, {s: NO_COST for s in data})


def test_rotation_buys_the_strongest_asset_on_the_first_rebalance():
    """At the 1 Feb rebalance, A is +10% over the window and B is only +2% -- both beat cash, but only
    the top_n=1 winner should be bought, and nothing should happen before the decision is even made."""
    res = _run()
    assert res.exposure.loc["2024-01-31"] == pytest.approx(0.0), "nothing bought before the rebalance decides"
    assert res.exposure.loc["2024-02-02"] == pytest.approx(1.0), "filled at the next day's open"
    assert res.symbol_pnl["B"] == 0.0, "B beat cash but lost to A, so it must never be bought"


def test_rotation_moves_out_of_a_former_winner_that_stops_beating_cash():
    """By 1 Mar, A has crashed and B is merely flat -- tied with cash, not beating it -- so the sleeve
    must fully exit A rather than rotate into a tie."""
    res = _run()
    (trade,) = res.trades
    assert trade.symbol == "A"
    assert trade.entry_date == pd.Timestamp("2024-02-02")
    assert trade.exit_date == pd.Timestamp("2024-03-04"), "sold the day after the 1 Mar decision"
    assert trade.exit_reason.startswith("momentum"), trade.exit_reason


def test_rotation_does_not_rebalance_mid_month():
    """Only the first trading day of the month may trigger a decision -- a mid-month day must not,
    even though fresh momentum data is available every day."""
    res = _run()
    # If a mid-month day rebalanced, selling and rebuying A would show up as more than the one
    # round trip recorded between the two month-start decisions.
    assert len(res.trades) == 1
