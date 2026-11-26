"""Direct unit tests for the indicator math every strategy's entry signal is built on.

`atr()` in particular feeds stop-loss distance and therefore position sizing everywhere (see
trader/strategies/trend.py, mean_reversion.py): a quiet off-by-one here would silently mis-size risk
across the whole system without ever raising an error.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategies.indicators import atr, momentum, rsi, sma


def make_ohlc(highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


# --- sma ---------------------------------------------------------------------------------------

def test_sma_matches_a_hand_computed_average():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    got = sma(s, 3)
    assert got.iloc[2] == pytest.approx(2.0)   # mean(1, 2, 3)
    assert got.iloc[3] == pytest.approx(3.0)   # mean(2, 3, 4)
    assert got.iloc[4] == pytest.approx(4.0)   # mean(3, 4, 5)


def test_sma_lookback_window_is_not_off_by_one():
    """With n=3 the average must not appear until the THIRD bar -- one bar early or late is exactly
    the kind of off-by-one that would silently mis-size every stop built on top of it."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    got = sma(s, 3)
    assert np.isnan(got.iloc[0])
    assert np.isnan(got.iloc[1]), "only two bars of history -- must not have an average yet"
    assert not np.isnan(got.iloc[2]), "the third bar is exactly enough history"


def test_sma_of_flat_prices_is_the_flat_price():
    flat = pd.Series([5.0] * 5)
    got = sma(flat, 2)
    assert got.iloc[1:].tolist() == pytest.approx([5.0, 5.0, 5.0, 5.0])


def test_sma_with_a_single_data_point():
    assert sma(pd.Series([5.0]), 1).iloc[0] == pytest.approx(5.0)
    assert np.isnan(sma(pd.Series([5.0]), 3).iloc[0]), "one bar cannot satisfy a 3-bar lookback"


def test_sma_a_gap_in_the_data_poisons_every_window_that_touches_it():
    """There is no missing-data handling in sma(): a NaN bar makes every window containing it NaN too,
    it does not get skipped over."""
    s = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
    got = sma(s, 3)
    assert got.isna().all(), "a window spanning the gap never has 3 real observations"


# --- atr ---------------------------------------------------------------------------------------

def test_atr_matches_a_hand_computed_ewm_of_true_range():
    """True range per bar: 2, 4, 2, 6 (first bar has no previous close, so it is just high - low).
    With n=2 (alpha=0.5, Wilder-style ewm, adjust=False): y1=3.0, y2=2.5, y3=4.25, hand-verified."""
    df = make_ohlc(highs=[10, 13, 12, 15], lows=[8, 9, 10, 9], closes=[9, 12, 11, 14])
    got = atr(df, 2)
    assert np.isnan(got.iloc[0])
    assert got.iloc[1] == pytest.approx(3.0)
    assert got.iloc[2] == pytest.approx(2.5)
    assert got.iloc[3] == pytest.approx(4.25)


def test_atr_lookback_window_is_not_off_by_one():
    df = make_ohlc(highs=[10, 10, 10, 10], lows=[8, 8, 8, 8], closes=[9, 9, 9, 9])
    got = atr(df, 3)
    assert np.isnan(got.iloc[1]), "only two bars of history -- must not have an ATR yet"
    assert not np.isnan(got.iloc[2]), "the third bar is exactly enough history"


def test_atr_of_a_flat_market_is_zero():
    """No range, no gaps -- true range is zero every day, so ATR must settle at exactly zero, not
    some small positive artefact of the smoothing."""
    df = make_ohlc(highs=[10] * 5, lows=[10] * 5, closes=[10] * 5)
    got = atr(df, 3)
    assert got.iloc[2:].tolist() == pytest.approx([0.0, 0.0, 0.0])


def test_atr_with_a_single_bar_needs_more_history_than_that():
    df = make_ohlc(highs=[10], lows=[8], closes=[9])
    assert np.isnan(atr(df, 3).iloc[0])
    assert atr(df, 1).iloc[0] == pytest.approx(2.0), "n=1 needs only the one bar it has"


# --- rsi ---------------------------------------------------------------------------------------

def test_rsi_matches_a_hand_computed_wilder_smoothing():
    """Closes 10, 12, 11, 14, 13 -> deltas +2, -1, +3, -1. With n=2 (alpha=0.5) the gain/loss ewms and
    RSI at each bar were hand-computed: 66.67, 88.89, 61.54."""
    s = pd.Series([10.0, 12.0, 11.0, 14.0, 13.0])
    got = rsi(s, 2)
    assert np.isnan(got.iloc[0]) and np.isnan(got.iloc[1])
    assert got.iloc[2] == pytest.approx(66.66666666666666)
    assert got.iloc[3] == pytest.approx(88.88888888888889)
    assert got.iloc[4] == pytest.approx(61.53846153846154)


def test_rsi_lookback_window_is_not_off_by_one():
    s = pd.Series([10.0, 12.0, 11.0, 14.0, 13.0, 16.0])
    got = rsi(s, 2)
    assert np.isnan(got.iloc[1]), "only one delta of history -- must not have an RSI yet"
    assert not np.isnan(got.iloc[2]), "the second delta is exactly enough history"


def test_rsi_is_100_when_every_move_is_a_gain():
    up = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    assert rsi(up, 2).iloc[2:].tolist() == pytest.approx([100.0] * 5)


def test_rsi_is_0_when_every_move_is_a_loss():
    down = pd.Series([7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    assert rsi(down, 2).iloc[2:].tolist() == pytest.approx([0.0] * 5)


def test_rsi_with_a_single_data_point():
    assert np.isnan(rsi(pd.Series([5.0]), 2).iloc[0])


# --- momentum ----------------------------------------------------------------------------------

def test_momentum_matches_a_hand_computed_return():
    s = pd.Series([100.0, 110.0, 121.0, 100.0])
    got = momentum(s, 2)
    assert got.iloc[2] == pytest.approx(0.21)              # 121 / 100 - 1
    assert got.iloc[3] == pytest.approx(100.0 / 110.0 - 1)  # 100 / 110 - 1


def test_momentum_lookback_window_is_not_off_by_one():
    """With n=2, the first bar able to look back 2 bars is index 2 -- index 1 must still be NaN even
    though it has one prior bar, because one bar is not two."""
    s = pd.Series([100.0, 110.0, 121.0, 100.0])
    got = momentum(s, 2)
    assert np.isnan(got.iloc[1])
    assert not np.isnan(got.iloc[2])


def test_momentum_of_flat_prices_is_zero():
    flat = pd.Series([50.0] * 5)
    assert momentum(flat, 2).iloc[2:].tolist() == pytest.approx([0.0, 0.0, 0.0])


def test_momentum_with_a_single_data_point():
    assert np.isnan(momentum(pd.Series([100.0]), 2).iloc[0])
