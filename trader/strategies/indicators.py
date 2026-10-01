"""Indicator helpers. Each value at row t uses only rows <= t (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(series: pd.Series, n: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    out[(loss == 0) & gain.notna()] = 100.0  # only gains in the window
    return out  # NaN until there is enough history


def momentum(series: pd.Series, n: int) -> pd.Series:
    """Total return over the last n bars."""
    return series / series.shift(n) - 1


def efficiency_ratio(series: pd.Series, n: int) -> pd.Series:
    """Kaufman's Efficiency Ratio: how much of the movement actually went somewhere.

    Net change over n bars divided by the total distance travelled getting there. 1.0 is a straight line;
    near 0 is a market that thrashed about and ended where it started. Used as a regime filter -- trend
    following loses money in chop, and this measures chop directly rather than inferring it.
    """
    direction = (series - series.shift(n)).abs()
    distance = series.diff().abs().rolling(n, min_periods=n).sum()
    return direction / distance.replace(0, np.nan)
