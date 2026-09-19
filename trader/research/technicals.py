"""A compact technical snapshot of one symbol, for the AI analyst team (`trader technicals SYMBOL --json`)."""

from __future__ import annotations

import pandas as pd

from trader.data.history import load_history
from trader.strategies.indicators import atr, rsi, sma


def snapshot(symbol: str, bars: pd.DataFrame | None = None) -> dict:
    df = bars if bars is not None else load_history(symbol)
    c = df["close"]
    last = float(c.iloc[-1])

    def ret(n: int) -> float | None:
        return round(last / float(c.iloc[-n - 1]) - 1, 4) if len(c) > n else None

    def val(series: pd.Series) -> float | None:
        v = series.iloc[-1]
        return None if pd.isna(v) else round(float(v), 6)

    sma20, sma50, sma200 = val(sma(c, 20)), val(sma(c, 50)), val(sma(c, 200))
    year = c.iloc[-252:]
    return {
        "symbol": symbol, "as_of": str(df.index[-1].date()), "close": round(last, 6),
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "above_sma200": None if sma200 is None else last > sma200,
        "rsi14": val(rsi(c, 14)), "atr14": val(atr(df, 14)),
        "atr14_pct": None if val(atr(df, 14)) is None else round(val(atr(df, 14)) / last, 4),  # type: ignore[operator]
        "return_5d": ret(5), "return_20d": ret(20), "return_60d": ret(60), "return_252d": ret(252),
        "pct_below_52w_high": round(last / float(year.max()) - 1, 4),
        "pct_above_52w_low": round(last / float(year.min()) - 1, 4),
    }
