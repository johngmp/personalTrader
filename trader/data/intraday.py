"""Intraday candles, for looking at rather than trading on.

Yahoo keeps roughly a day of 1-minute bars and a week of 5-minute ones. That is enough to SEE what a
session looks like and nowhere near enough to test a strategy on: no out-of-sample period, no
walk-forward, and nothing close to the 50 completed trades Gate 1 asks for. It also quotes mid prices
with no bid or ask, so on a timeframe where the spread IS the cost, the cost cannot be modelled at all.

So this feeds charts. It is deliberately not wired into the backtester, and `trader.data.history`
remains the only source a strategy ever sees.

Intervals are chosen to stay legible rather than to be the finest available: 15 minutes over a day is
~96 candles, an hour over five days is ~120. Five-minute bars over a day would be 288 in a 500px panel,
which draws as a smear.
"""

from __future__ import annotations

import time
from datetime import timezone

CACHE_TTL_S = 60
# range -> (yfinance period, interval, roughly how many candles)
RANGES: dict[str, tuple[str, str]] = {
    "1D": ("1d", "15m"),
    "5D": ("5d", "1h"),
}
_cache: dict[tuple[str, str], tuple[float, list]] = {}


class IntradayError(RuntimeError):
    pass


def candles(symbol: str, window: str) -> list[list]:
    """[iso, open, high, low, close, volume] for `window`, cached briefly.

    Raises IntradayError rather than returning an empty list, so a dead feed is visible instead of
    looking like a quiet market.
    """
    if window not in RANGES:
        raise IntradayError(f"unknown window {window!r}; expected one of {sorted(RANGES)}")
    key = (symbol, window)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]

    from trader.data.history import YAHOO_TICKERS

    ticker = YAHOO_TICKERS.get(symbol) or (symbol.replace(".", "-") if "/" not in symbol else None)
    if ticker is None:
        raise IntradayError(f"no Yahoo ticker mapped for {symbol}")
    period, interval = RANGES[window]

    import yfinance as yf

    frame = yf.Ticker(ticker).history(period=period, interval=interval)
    if frame.empty:
        raise IntradayError(f"no intraday data for {symbol} ({ticker}) over {window}; "
                            "the market may have been shut for this whole window")
    out = []
    for ts, row in frame.iterrows():
        moment = ts.to_pydatetime()
        moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
        out.append([moment.isoformat(timespec="minutes"), round(float(row["Open"]), 6),
                    round(float(row["High"]), 6), round(float(row["Low"]), 6),
                    round(float(row["Close"]), 6), float(row.get("Volume") or 0)])
    _cache[key] = (time.monotonic(), out)
    return out
