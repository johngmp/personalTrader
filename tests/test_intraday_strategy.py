"""The London opening-range breakout, and the backtester that charges the real quoted spread.

Tested on constructed sessions rather than downloaded data, so the rules are verified independently of
whether Dukascopy is reachable today.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader.research.intraday_backtest import run, summarise
from trader.strategies.intraday import OpeningRangeBreakout


def session(bars: list[tuple[str, float, float, float, float]], spread: float = 0.00002) -> pd.DataFrame:
    """bars of (HH:MM, open, high, low, close) on one day, in UTC."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-06-15 {t}", tz="UTC") for t, *_ in bars])
    return pd.DataFrame({"open": [b[1] for b in bars], "high": [b[2] for b in bars],
                         "low": [b[3] for b in bars], "close": [b[4] for b in bars],
                         "spread": spread, "ticks": 100}, index=idx)


FLAT = [("07:00", 1.1000, 1.1010, 1.0990, 1.1000), ("07:30", 1.1000, 1.1010, 1.0990, 1.1000)]


def test_a_breakout_above_the_opening_range_is_taken():
    bars = session(FLAT + [("08:00", 1.1000, 1.1050, 1.0995, 1.1045)])
    plan = OpeningRangeBreakout().plan(bars)
    assert plan is not None
    assert plan["entry"] == pytest.approx(1.1010)        # the range high, not the bar close
    assert plan["stop"] < 1.0990                          # below the range low


def test_nothing_is_taken_when_price_never_clears_the_range():
    bars = session(FLAT + [("08:00", 1.1000, 1.1005, 1.0995, 1.1000)])
    assert OpeningRangeBreakout().plan(bars) is None


def test_a_range_too_tight_to_mean_anything_is_skipped():
    """A two-pip range is noise; breaking it is not a signal."""
    tight = [("07:00", 1.1000, 1.1001, 1.0999, 1.1000), ("07:30", 1.1000, 1.1001, 1.0999, 1.1000),
             ("08:00", 1.1000, 1.1050, 1.0999, 1.1045)]
    assert OpeningRangeBreakout(min_range_pips=3.0).plan(session(tight)) is None


def test_the_range_window_only_sees_bars_inside_it():
    """If the range could see later bars it would know the future, which is the classic backtest lie."""
    bars = session(FLAT + [("08:00", 1.1000, 1.2000, 1.0000, 1.1500)])
    plan = OpeningRangeBreakout().plan(bars)
    assert plan["range_high"] == pytest.approx(1.1010)   # unaffected by the huge later bar
    assert plan["range_low"] == pytest.approx(1.0990)


def test_a_trade_after_the_close_hour_is_not_taken():
    late = FLAT + [("17:00", 1.1000, 1.1050, 1.0995, 1.1045)]
    assert OpeningRangeBreakout(close_hour=16).plan(session(late)) is None


# --- the backtester ----------------------------------------------------------------------------------

def test_the_spread_is_charged_on_both_sides():
    bars = session(FLAT + [("08:00", 1.1000, 1.1050, 1.1005, 1.1045),
                           ("08:30", 1.1045, 1.1060, 1.1040, 1.1055)], spread=0.0002)
    result = run(OpeningRangeBreakout(), bars, capital=10_000)
    assert result["trades"], "the breakout should have been taken"
    trade = result["trades"][0]
    assert trade.spread_cost > 0
    assert trade.entry > 1.1010, "entry pays the ask, so it is worse than the level"
    assert trade.exit < 1.1055, "exit sells into the bid, so it is worse than the close"


def test_a_wider_spread_costs_more():
    bars = [("07:00", 1.1000, 1.1010, 1.0990, 1.1000), ("07:30", 1.1000, 1.1010, 1.0990, 1.1000),
            ("08:00", 1.1000, 1.1050, 1.1005, 1.1045), ("08:30", 1.1045, 1.1060, 1.1040, 1.1055)]
    tight = run(OpeningRangeBreakout(), session(bars, spread=0.00002), capital=10_000)
    wide = run(OpeningRangeBreakout(), session(bars, spread=0.0005), capital=10_000)
    assert wide["trades"][0].spread_cost > tight["trades"][0].spread_cost * 5
    assert wide["equity"] < tight["equity"]


def test_a_stop_that_gaps_fills_at_the_worse_price():
    """Assuming you always get the stop exactly is how a backtest hides its worst days."""
    bars = session(FLAT + [("08:00", 1.1000, 1.1050, 1.1005, 1.1045),
                           ("08:30", 1.0900, 1.0905, 1.0800, 1.0850)])
    trade = run(OpeningRangeBreakout(), bars, capital=10_000)["trades"][0]
    assert trade.reason == "stop"
    assert trade.exit < 1.0960, "a gap through the stop must cost what it cost"


def test_position_size_follows_the_one_percent_rule():
    bars = session(FLAT + [("08:00", 1.1000, 1.1050, 1.1005, 1.1045),
                           ("08:30", 1.1045, 1.1060, 1.1040, 1.1055)])
    trade = run(OpeningRangeBreakout(), bars, capital=10_000, risk_per_trade=0.01)["trades"][0]
    risked = trade.qty * (trade.entry - trade.stop)
    assert risked == pytest.approx(100, rel=0.15), "1% of $10,000 at risk, give or take the spread"


def test_the_summary_reports_what_the_spread_took():
    """The whole reason for this backtester: the share of gross profit eaten by friction."""
    bars = session(FLAT + [("08:00", 1.1000, 1.1050, 1.1005, 1.1045),
                           ("08:30", 1.1045, 1.1060, 1.1040, 1.1055)], spread=0.0002)
    got = summarise(run(OpeningRangeBreakout(), bars, capital=10_000))
    assert got["trades"] == 1
    assert got["spread_paid"] > 0 and "spread_share_of_gross" in got


def test_no_trades_summarises_without_crashing():
    assert summarise({"trades": [], "equity": 1.0, "capital": 1.0})["trades"] == 0
