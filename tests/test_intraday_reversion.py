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


# --- the short side, tested and rejected -----------------------------------------------------------

def test_the_harness_can_price_a_short_and_mirrors_every_pessimistic_assumption():
    """The short side had to be TESTED before it could be rejected, and testing it needed a harness that
    does not quietly flatter it: sold into the bid, bought back at the ask, stopped on the HIGH, and
    gapped fills taken at the worse of the stop and the bar's open.

    It was rejected on the evidence, not on the absence of it -- see PLAN.md. The support stays because
    the next idea of this shape should be measurable without rebuilding it.
    """
    import inspect

    from trader.research import intraday_backtest

    src = inspect.getsource(intraday_backtest.run)
    assert 'plan.get("side", "long") == "short"' in src, "a plan must be able to say which way it goes"
    assert 'bar["high"] >= stop if short else bar["low"] <= stop' in src
    assert "max(float(bar[\"open\"]), stop) if short else min(float(bar[\"open\"]), stop)" in src
    assert "entry - spread_at_entry / 2 - slip if short" in src, "a short is sold into the bid"
    assert "exit_price + spread_at_exit / 2 + slip if short" in src, "and bought back at the ask"
    assert "qty * (fill_in - fill_out) if short" in src


def test_a_short_trade_sizes_off_its_own_stop_distance():
    """Sizing used `entry - stop`, which is negative for a short and would have skipped every one."""
    import inspect

    from trader.research import intraday_backtest

    src = inspect.getsource(intraday_backtest.run)
    assert "risk_per_unit = (stop - entry) if short else (entry - stop)" in src
    assert "if after.empty or risk_per_unit <= 0:" in src

    trade = intraday_backtest.IntradayTrade(
        day=None, entry_ts=None, exit_ts=None, entry=1.1200, exit=1.1180, qty=10_000,
        stop=1.1220, spread_cost=1.0, pnl=19.0, reason="target", side="short")
    assert trade.r_multiple == pytest.approx(19.0 / (10_000 * 0.0020), rel=1e-6)


# --- the rule being measured must be the rule being run --------------------------------------------

def test_the_backtest_stops_at_the_session_close_the_live_watcher_enforces():
    """It did not, and that is where most of the measured edge came from.

    `session` is the whole UTC day. The harness scanned all of it, so a "session end" exit landed as
    late as 23:55 while the live watcher goes flat at 16:00. On EUR/USD that was 37 of 84 trades exiting
    at or after 16:00, carrying $1,193 of the $1,941 total -- 61% of the profit from a rule the live
    system does not run. Enforcing the close took the out-of-sample result from +17.2%/yr to +4.0%.

    Both strategies had always computed `exit_deadline`. Nothing had ever read it.
    """
    import inspect

    from trader.research import intraday_backtest
    from trader.strategies.intraday import OpeningRangeBreakout
    from trader.strategies.intraday_reversion import SessionReversion

    src = inspect.getsource(intraday_backtest.run)
    assert 'deadline = plan.get("exit_deadline")' in src
    assert "after = after[after.index <= deadline]" in src

    for strategy in (SessionReversion, OpeningRangeBreakout):
        assert "exit_deadline" in inspect.getsource(strategy.plan), \
            f"{strategy.__name__} must say when it has to be flat"


def test_no_trade_outlives_its_own_session():
    """The property that matters, checked on the real bars rather than on the source."""
    import glob

    import pandas as pd

    from trader.research.intraday_backtest import run as intraday_run
    from trader.strategies.intraday_reversion import SessionReversion

    files = sorted(glob.glob("data/ticks/EURUSD_5m/*.csv"))
    if not files:
        pytest.skip("no tick data on this machine")
    bars = pd.concat([pd.read_csv(f, parse_dates=[0], index_col=0) for f in files]).sort_index()
    bars = bars[~bars.index.duplicated()]

    result = intraday_run(SessionReversion(drop_atr=0.5, stop_atr=0.3, target_frac=1.0), bars)
    trades = result["trades"]
    assert trades, "no trades to check"
    assert all(t.exit_ts.hour < 16 for t in trades), \
        "a trade exited at or after 16:00, which the live watcher would already have closed"
    assert all(t.entry_ts.date() == t.exit_ts.date() for t in trades), "nothing may be held overnight"
