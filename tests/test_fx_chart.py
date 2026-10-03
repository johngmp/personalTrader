"""The forex page: a pair list and a chart, and the formatting that nearly ruined it.

The chart code was written for account balances and hardcoded a dollar sign and Math.round on the axis,
so every gridline on a EUR/USD chart read "$1". That is the kind of bug that looks like a rendering
glitch and is actually a wrong number.
"""

from __future__ import annotations

import pytest

from trader.engine import dashboard as dash
from trader.engine.dashboard import fx_series


def test_a_pair_gets_its_price_history():
    series = fx_series(["EUR/USD"])
    assert "EUR/USD" in series and len(series["EUR/USD"]) > 100
    first, last = series["EUR/USD"][0], series["EUR/USD"][-1]
    assert first[0] < last[0], "points must be in time order"
    assert 0.5 < last[1] < 2.0, "a EUR/USD rate should be near parity"


def test_the_live_quote_is_appended_as_the_final_point():
    plain = fx_series(["EUR/USD"])
    live = fx_series(["EUR/USD"], {"EUR/USD": 1.2345})
    assert len(live["EUR/USD"]) == len(plain["EUR/USD"]) + 1
    assert live["EUR/USD"][-1][1] == pytest.approx(1.2345)


def test_a_symbol_with_no_history_is_skipped_rather_than_breaking_the_page():
    assert fx_series(["NOT/REAL"]) == {}


def test_gold_is_charted_in_its_own_range():
    series = fx_series(["XAU/USD"])
    assert series["XAU/USD"][-1][1] > 1000


# --- the formatting bug ----------------------------------------------------------------------------

def test_the_axis_goes_through_the_formatter_not_a_hardcoded_dollar_sign():
    assert "fmt(v, true)" in dash.JS
    assert """text-anchor="end">$' + Math.round(v)""" not in dash.JS


def test_dollars_still_round_on_the_axis():
    """$100,734.44 on every gridline would be unreadable."""
    assert "if (axis) return '$' + Math.round(v).toLocaleString();" in dash.JS


def test_rates_keep_their_decimals_on_the_axis():
    """Rounding a rate gridline makes every one read "1", which is how the bug showed up."""
    assert "argument is ignored on purpose" in dash.FX_JS
    assert "v >= 1000 ? 2 : v >= 100 ? 3 : 5" in dash.FX_JS


def test_the_chart_is_drawn_only_once_the_section_is_visible():
    """A hidden element has no width, so drawing into it produces a chart one pixel wide."""
    assert 'rail-btn[data-tab="forex"]' in dash.FX_JS and "setTimeout" in dash.FX_JS


def test_clicking_a_pair_switches_the_chart():
    assert "e.target.closest('.pair')" in dash.FX_JS


def test_the_percentage_in_the_list_says_what_period_it_covers():
    """A bare "-3.39%" is unreadable: over a day, a month, a year? Checked on the real page."""
    import inspect

    source = inspect.getsource(dash)
    assert '<span class="subtle"> 1M</span>' in source
    assert "series[-22][1]" in source, "the label must match the window actually measured"
