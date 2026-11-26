"""S3: buy the dip in an uptrend, on a small hand-built dataset where the rules are unambiguous.

200 flat bars give every indicator (sma200, sma5, atr) a clean, known baseline before the interesting
few bars that actually exercise the entry/exit rules -- the same style test_backtest.py uses for S1/S8.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import run_backtest
from trader.strategies.mean_reversion import MeanReversion

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)
LOOSE = SleeveLimits(max_position_pct_of_sleeve=100, risk_per_trade_pct=100, require_stop=False)

FLAT = [100.0] * 195   # long enough to give sma200 a settled, known baseline


def bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    s = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": s, "high": s + 0.5, "low": s - 0.5, "close": s, "volume": 1.0})


def _run(closes, **params):
    strat = MeanReversion(params={"rsi_entry": 30, "max_hold": 5, "stop_atr": 3.0, **params}, symbols=["X"])
    return run_backtest(strat, {"X": bars(closes)}, 10_000, LOOSE, {"X": NO_COST})


def test_the_dip_is_bought_in_an_uptrend_and_sold_on_the_bounce():
    """A run-up to 115 then a 2-day pullback to 108/106 pushes RSI2 down to ~29 while price (108) is
    still well above the 200-day average (~100.3) -- that is exactly the setup this strategy exists
    for. It should buy, then sell once price closes back above its 5-day average."""
    closes = FLAT + [103, 106, 109, 112, 115, 108, 106] + [107] * 20
    res = _run(closes)
    (trade, *_) = res.trades
    assert trade.entry_date == pd.Timestamp("2024-10-08"), "filled the day after the low-RSI2 close"
    assert trade.entry_price == pytest.approx(106.0)
    assert trade.exit_reason == "bounced above 5-day average"


def test_the_same_oversold_dip_is_not_bought_below_the_200day_average():
    """Same RSI2 extreme (0, maximally oversold) -- but reached by falling steadily from the flat
    baseline rather than pulling back from a high, so price sits BELOW its 200-day average throughout.
    'Buy the dip' only means something in an uptrend; this must not enter at all."""
    closes = FLAT + [97, 94, 91, 88, 85] + [107] * 20
    res = _run(closes)
    before_the_later_recovery = [t for t in res.trades if t.entry_date < pd.Timestamp("2024-10-07")]
    assert before_the_later_recovery == [], "RSI2 was 0 here -- only the trend filter can explain no entry"


def test_a_dip_that_never_recovers_is_closed_by_max_hold_not_left_open():
    """If price stays below the 5-day average, the position must not be held forever -- it exits once
    `max_hold` bars have passed, at a loss here since it never bounced."""
    closes = FLAT + [103, 106, 109, 112, 115, 108, 106] + [104] * 20
    res = _run(closes, max_hold=3)
    trade = res.trades[0]
    assert trade.entry_date == pd.Timestamp("2024-10-08")
    assert trade.bars_held == 3
    assert trade.exit_reason == "max hold reached"
    assert trade.exit_price == pytest.approx(104.0)


def test_a_wider_stop_atr_multiple_risks_more_per_share():
    """stop = entry close - stop_atr * ATR, and initial_risk feeds position sizing everywhere else --
    an off-by-one or sign error here would silently change how much of the sleeve every trade risks."""
    closes = FLAT + [103, 106, 109, 112, 115, 108, 106] + [107] * 20
    tight = _run(closes, stop_atr=2.0).trades[0]
    wide = _run(closes, stop_atr=4.0).trades[0]
    tight_risk_per_share = tight.initial_risk / tight.qty
    wide_risk_per_share = wide.initial_risk / wide.qty
    assert wide_risk_per_share > tight_risk_per_share
    # Both stops move by stop_atr * ATR off the same decision-day close, so the gap between them is
    # exactly 2 ATRs -- the constant offset between the decision close and the next day's fill cancels.
    from trader.strategies.indicators import atr

    atr_on_decision_day = atr(bars(closes), 14).loc["2024-10-07"]
    assert wide_risk_per_share - tight_risk_per_share == pytest.approx(2 * atr_on_decision_day)
