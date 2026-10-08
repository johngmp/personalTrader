"""Intraday candles: for looking at, never for deciding on.

Yahoo keeps ~1 day of 1-minute bars and ~1 week of 5-minute ones, with mid prices and no bid/ask. That
cannot be backtested -- no out-of-sample period, no walk-forward, and the spread, which IS the cost on
these timeframes, cannot be modelled at all. These tests pin that it stays out of the strategy path.
"""

from __future__ import annotations

import inspect

import pytest

from trader.data import intraday
from trader.data.intraday import RANGES, IntradayError, candles

ISO, OPEN, HIGH, LOW, CLOSE, VOL = range(6)


@pytest.fixture(autouse=True)
def _clear():
    intraday._cache.clear()
    yield
    intraday._cache.clear()


def test_a_day_of_candles_comes_back_at_a_legible_spacing():
    """288 five-minute candles in a 500px panel draw as a smear; 15-minute bars give ~96.

    This asserts the SPACING, not the count. It used to require `30 < len(got) < 200`, where the lower
    bound stood in for "these are 15-minute bars and not hourly ones" -- and that quietly depended on
    the time of day. The FX week rolls over at 21:00 UTC, so a run a few hours later has only a handful
    of candles so far and the test failed with nothing to suggest the clock was the reason.

    The most common gap is the real invariant: it survives a half-finished session, and it still fails
    if the interval is ever changed to 5 minutes or an hour.
    """
    from collections import Counter
    from datetime import datetime

    got = candles("EUR/USD", "1D")
    assert got, "no candles came back at all"
    assert len(got[0]) == 6
    assert len(got) < 200, "a day must not come back as hundreds of candles"
    stamps = [datetime.fromisoformat(c[ISO]) for c in got]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])]
    assert gaps, "a single candle says nothing about the interval"
    assert Counter(gaps).most_common(1)[0][0] == 15, f"expected 15-minute bars, gaps were {set(gaps)}"


def test_the_high_and_low_bracket_the_open_and_close():
    for c in candles("EUR/USD", "1D"):
        assert c[LOW] <= min(c[OPEN], c[CLOSE]) and c[HIGH] >= max(c[OPEN], c[CLOSE])


def test_candles_are_in_time_order():
    got = candles("EUR/USD", "5D")
    assert [c[ISO] for c in got] == sorted(c[ISO] for c in got)


def test_an_unknown_window_is_refused():
    with pytest.raises(IntradayError, match="unknown window"):
        candles("EUR/USD", "99Y")


def test_an_unmapped_symbol_is_refused_before_any_network_call():
    with pytest.raises(IntradayError, match="no Yahoo ticker"):
        candles("NOT/REAL", "1D")


def test_an_empty_response_raises_rather_than_returning_nothing():
    """A dead feed must be visible, not look like a quiet market."""
    source = inspect.getsource(candles)
    assert "if frame.empty:" in source and "raise IntradayError" in source


def test_results_are_cached_so_clicking_about_does_not_refetch():
    first = candles("EUR/USD", "1D")
    assert candles("EUR/USD", "1D") is first      # same object: served from cache


def test_every_window_is_coarse_enough_to_draw():
    for window, (_period, interval) in RANGES.items():
        assert interval in {"15m", "1h"}, f"{window} uses {interval}, which is too fine to render"


# --- the line that must not be crossed ---------------------------------------------------------------

def test_intraday_data_never_reaches_the_backtester():
    """Gate 1 would be meaningless on a week of history: no out-of-sample, no walk-forward."""
    from trader.research import backtest, evaluate

    for module in (backtest, evaluate):
        assert "intraday" not in inspect.getsource(module)


def test_intraday_data_never_reaches_a_strategy():
    from trader.strategies import trend

    assert "intraday" not in inspect.getsource(trend)


def test_the_module_says_why_it_is_only_for_charts():
    assert "never" in intraday.__doc__.lower() or "not wired into the backtester" in intraday.__doc__


def test_the_endpoint_only_serves_symbols_in_the_universe():
    """A query string must not be able to choose what the server goes and fetches."""
    from trader.engine import serve

    source = inspect.getsource(serve)
    assert "known = set(settings.universe.symbols)" in source
    assert "if symbol not in known" in source
