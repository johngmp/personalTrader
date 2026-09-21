"""Sharpe must be measured in excess of cash.

Assuming cash pays 0% flatters every strategy by roughly (risk-free rate / volatility). It is how S4
passed Gate 1 on 2026-09-21 with an out-of-sample Sharpe of 0.29 while actually losing to T-bills.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.research.metrics import compute_metrics, risk_free_returns


def equity_from(daily_return: float, days: int = 1000, seed: int = 0, vol: float = 0.004) -> pd.Series:
    """An equity curve whose daily returns average EXACTLY `daily_return`.

    The noise is demeaned deliberately: a raw random draw's sample mean is off by ~vol/sqrt(days), which
    at this volatility is enough to move Sharpe by 0.8 and make these assertions meaningless.
    """
    index = pd.bdate_range("2010-01-01", periods=days)
    noise = np.random.default_rng(seed).normal(0, vol, days)
    noise -= noise.mean()
    return pd.Series(10_000 * np.cumprod(1 + daily_return + noise), index=index)


def flat_rf(index: pd.DatetimeIndex, annual: float) -> pd.Series:
    return pd.Series((1 + annual) ** (1 / 252) - 1, index=index)


def test_assuming_cash_pays_nothing_overstates_sharpe():
    eq = equity_from(0.0002)
    rf = flat_rf(eq.index[1:], 0.04)
    assert compute_metrics(eq).sharpe > compute_metrics(eq, risk_free=rf).sharpe


def test_earning_exactly_the_cash_rate_is_a_sharpe_of_zero():
    """4% a year while cash pays 4% is no achievement, and the number should say so."""
    daily = (1.04 ** (1 / 252)) - 1
    eq = equity_from(daily)
    rf = flat_rf(eq.index[1:], 0.04)
    assert compute_metrics(eq, risk_free=rf).sharpe == pytest.approx(0.0, abs=0.1)
    assert compute_metrics(eq).sharpe > 0.5      # what it looked like before the fix


def test_losing_to_cash_gives_a_negative_sharpe():
    eq = equity_from((1.01 ** (1 / 252)) - 1)    # 1% a year...
    rf = flat_rf(eq.index[1:], 0.05)             # ...while cash pays 5%
    assert compute_metrics(eq, risk_free=rf).sharpe < 0


def test_a_zero_rate_leaves_the_old_numbers_untouched():
    eq = equity_from(0.0002)
    rf = pd.Series(0.0, index=eq.index[1:])
    assert compute_metrics(eq, risk_free=rf).sharpe == pytest.approx(compute_metrics(eq).sharpe)


def test_returns_and_drawdown_are_not_changed_by_the_cash_rate():
    """Only the risk-adjusted figures net off cash; the money actually made is still the money made."""
    eq = equity_from(0.0002)
    rf = flat_rf(eq.index[1:], 0.04)
    plain, adjusted = compute_metrics(eq), compute_metrics(eq, risk_free=rf)
    assert adjusted.cagr == pytest.approx(plain.cagr)
    assert adjusted.max_drawdown == pytest.approx(plain.max_drawdown)


def test_sortino_nets_off_cash_too():
    eq = equity_from(0.0002)
    rf = flat_rf(eq.index[1:], 0.04)
    assert compute_metrics(eq, risk_free=rf).sortino < compute_metrics(eq).sortino


# --- the cash series itself -----------------------------------------------------------------------

def test_the_cash_series_covers_the_whole_index():
    index = pd.bdate_range("2015-01-01", periods=300)
    rf = risk_free_returns(index)
    assert len(rf) == len(index) and not rf.isna().any()


def test_dates_before_the_cash_etf_existed_are_treated_as_zero_not_missing():
    """BIL starts in 2007. Earlier dates must be 0, which understates the hurdle rather than breaking."""
    index = pd.bdate_range("2001-01-01", periods=200)
    rf = risk_free_returns(index)
    assert not rf.isna().any() and float(rf.abs().sum()) == pytest.approx(0.0)


def test_an_unknown_cash_symbol_degrades_to_zero_rather_than_exploding():
    index = pd.bdate_range("2015-01-01", periods=50)
    rf = risk_free_returns(index, symbol="NOT_A_REAL_SYMBOL")
    assert len(rf) == len(index) and float(rf.abs().sum()) == 0.0
