"""The forex page: a pair list and a chart, and the formatting that nearly ruined it.

The chart code was written for account balances and hardcoded a dollar sign and Math.round on the axis,
so every gridline on a EUR/USD chart read "$1". That is the kind of bug that looks like a rendering
glitch and is actually a wrong number.
"""

from __future__ import annotations

import pytest

from trader.engine import dashboard as dash
from trader.engine.dashboard import fx_series


# A candle is [iso, open, high, low, close, volume].
ISO, OPEN, HIGH, LOW, CLOSE, VOL = range(6)


def test_a_pair_gets_its_price_history_as_candles():
    series = fx_series(["EUR/USD"])
    assert "EUR/USD" in series and len(series["EUR/USD"]) > 100
    first, last = series["EUR/USD"][0], series["EUR/USD"][-1]
    assert first[ISO] < last[ISO], "candles must be in time order"
    assert len(last) == 6
    assert 0.5 < last[CLOSE] < 2.0, "a EUR/USD rate should be near parity"


def test_the_high_and_low_bracket_the_open_and_close():
    """The whole reason for candles: the day's range, which a line of closes hides."""
    for candle in fx_series(["EUR/USD"])["EUR/USD"][:-1]:    # last one is still forming
        assert candle[LOW] <= min(candle[OPEN], candle[CLOSE])
        assert candle[HIGH] >= max(candle[OPEN], candle[CLOSE])


def test_the_live_quote_is_appended_as_a_forming_candle():
    plain = fx_series(["EUR/USD"])
    live = fx_series(["EUR/USD"], {"EUR/USD": 1.2345})
    assert len(live["EUR/USD"]) == len(plain["EUR/USD"]) + 1
    today = live["EUR/USD"][-1]
    assert today[CLOSE] == pytest.approx(1.2345)
    assert today[LOW] <= today[CLOSE] <= today[HIGH], "the forming candle must still bracket its price"


def test_a_symbol_with_no_history_is_skipped_rather_than_breaking_the_page():
    assert fx_series(["NOT/REAL"]) == {}


def test_gold_is_charted_in_its_own_range():
    assert fx_series(["XAU/USD"])["XAU/USD"][-1][CLOSE] > 1000


def test_spot_fx_has_no_volume_and_gold_does():
    """Spot FX is over the counter with no central exchange, so there is no volume figure to draw."""
    assert all(c[VOL] == 0 for c in fx_series(["EUR/USD"])["EUR/USD"])
    assert any(c[VOL] > 0 for c in fx_series(["XAU/USD"])["XAU/USD"])


# --- the formatting bug ----------------------------------------------------------------------------

def test_the_axis_goes_through_the_formatter_not_a_hardcoded_dollar_sign():
    assert "fmt(v, true)" in dash.JS
    assert """text-anchor="end">$' + Math.round(v)""" not in dash.JS


def test_dollars_still_round_on_the_axis():
    """$100,734.44 on every gridline would be unreadable."""
    assert "if (axis) return '$' + Math.round(v).toLocaleString();" in dash.JS


def test_rates_keep_their_decimals_on_the_axis():
    """Rounding a rate gridline makes every one read "1", which is how the bug showed up."""
    assert "v >= 1000 ? 2 : v >= 100 ? 3 : 5" in dash.FX_JS
    assert "text-anchor=\"end\">' + rate(v)" in dash.FX_JS


# --- candles and volume ------------------------------------------------------------------------------

def test_the_volume_panel_only_appears_when_there_is_volume():
    """An empty panel under every FX pair would imply the data is missing rather than nonexistent."""
    assert "var hasVol = candles.some(" in dash.FX_JS
    assert "volH = hasVol ?" in dash.FX_JS


def test_the_absence_of_fx_volume_is_explained_rather_than_left_blank():
    assert "spot FX has no exchange volume to report" in dash.FX_JS


def test_candles_become_a_line_when_they_would_be_thinner_than_their_outline():
    """At 400 candles in 500px each body is ~1px; drawn as rects they merge into a solid block."""
    assert "var thin = slot < 2.2;" in dash.FX_JS


def test_up_and_down_days_are_coloured_by_open_versus_close():
    assert "var up = c[4] >= c[1]" in dash.FX_JS


def test_the_wick_spans_the_high_and_the_low():
    """This is the point of the whole change: a stop is hit on the low."""
    assert "Y(c[2])" in dash.FX_JS and "Y(c[3])" in dash.FX_JS


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
