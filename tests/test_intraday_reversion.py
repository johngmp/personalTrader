"""Session mean reversion, and the honesty rules in the backtester that make its numbers mean anything.

The opening-range breakout failed on this same data, so the bar for believing a second intraday idea is
higher, not lower. These tests cover the two ways a result like that is usually wrong: a look-ahead in
the strategy, and a fill model that resolves ambiguity in its own favour.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader.research import intraday_backtest as ib
from trader.strategies.intraday_reversion import SessionReversion


def _session(day: str, prices: list[tuple[str, float, float, float, float]], spread=0.00003):
    """Bars from (hh:mm, open, high, low, close)."""
    idx = pd.to_datetime([f"{day} {t}" for t, *_ in prices]).tz_localize("UTC")
    return pd.DataFrame(
        {"open": [p[1] for p in prices], "high": [p[2] for p in prices],
         "low": [p[3] for p in prices], "close": [p[4] for p in prices],
         "spread": spread, "ticks": 10}, index=idx)


def test_the_scale_it_measures_against_uses_only_earlier_days():
    """The whole result rests on this. If today's own range fed the trigger, the strategy would be
    choosing its entry with knowledge of how far the day was going to move."""
    frames = []
    for i, day in enumerate(["2026-01-05", "2026-01-06", "2026-01-07"]):
        base = 1.10 + i * 0.01
        frames.append(_session(day, [("07:00", base, base + 0.002, base - 0.002, base),
                                     ("07:05", base, base + 0.002, base - 0.002, base),
                                     ("07:10", base, base + 0.002, base - 0.002, base),
                                     ("07:15", base, base + 0.002, base - 0.002, base)]))
    bars = pd.concat(frames)
    strat = SessionReversion(atr_days=20)
    seen = []
    for _day, _chunk in strat.sessions(bars):
        seen.append(strat._atr)
    assert seen[0] is None, "the first day has no earlier range, so it must not trade"
    assert seen[1] is not None and seen[2] is not None
    # Day 2's scale is day 1's range alone; day 3's is the mean of days 1 and 2.
    assert seen[1] == pytest.approx(0.004, abs=1e-9)
    assert seen[2] == pytest.approx(0.004, abs=1e-9)


def test_a_session_with_no_earlier_range_makes_no_plan():
    bars = _session("2026-01-05", [("07:00", 1.10, 1.101, 1.099, 1.10)] * 4)
    strat = SessionReversion()
    assert strat.plan(bars) is None, "no scale yet means no trade, not a guessed one"


def test_the_stop_wins_when_a_bar_contains_both_the_stop_and_the_target():
    """A 5-minute bar does not say which came first. Assuming the good one is how a backtest flatters
    itself, and on a strategy whose target is nearer than its stop that assumption would do real work."""
    bars = _session("2026-01-05", [
        ("07:00", 1.1000, 1.1002, 1.0998, 1.1000),
        ("07:05", 1.1000, 1.1002, 1.0990, 1.0995),     # trips the entry at 1.0995
        ("07:10", 1.0995, 1.1010, 1.0980, 1.0990),     # this bar holds BOTH the target and the stop
    ])

    class Fixed:
        def sessions(self, b):
            yield "d", b

        def plan(self, s):
            return {"entry_ts": s.index[2], "entry": 1.0995, "stop": 1.0985, "target": 1.1005}

    res = ib.run(Fixed(), bars, capital=10_000.0)
    assert len(res["trades"]) == 1
    assert res["trades"][0].reason == "stop", "a bar holding both must resolve as the stop"


def test_a_target_fills_at_its_level_and_never_better():
    bars = _session("2026-01-05", [
        ("07:00", 1.1000, 1.1002, 1.0998, 1.1000),
        ("07:05", 1.1000, 1.1002, 1.0990, 1.0995),
        ("07:10", 1.0995, 1.1050, 1.0994, 1.1040),     # spikes far through the target
    ])

    class Fixed:
        def sessions(self, b):
            yield "d", b

        def plan(self, s):
            return {"entry_ts": s.index[2], "entry": 1.0995, "stop": 1.0985, "target": 1.1005}

    t = ib.run(Fixed(), bars, capital=10_000.0)["trades"][0]
    assert t.reason == "target"
    # The limit fills at 1.1005 less half the spread and slippage -- never at the 1.1050 high.
    assert t.exit < 1.1005, "a limit sell cannot fill above its level once costs are charged"
    assert t.exit > 1.1000, f"but it should be close to it, got {t.exit}"


def test_a_plan_without_a_target_still_runs_to_the_stop_or_the_session_end():
    """The breakout has no target; adding target support must not have changed its behaviour."""
    bars = _session("2026-01-05", [
        ("07:00", 1.1000, 1.1002, 1.0998, 1.1000),
        ("07:05", 1.1000, 1.1002, 1.0990, 1.0995),
        ("07:10", 1.0995, 1.1020, 1.0994, 1.1015),
    ])

    class NoTarget:
        def sessions(self, b):
            yield "d", b

        def plan(self, s):
            return {"entry_ts": s.index[2], "entry": 1.0995, "stop": 1.0985}

    t = ib.run(NoTarget(), bars, capital=10_000.0)["trades"][0]
    assert t.reason == "session end", "with no target it must ride to the close"
