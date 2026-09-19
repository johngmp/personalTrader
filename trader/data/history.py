"""Daily price history for research, cached as CSV in data/history/.

Source: Yahoo Finance via yfinance, dividend-adjusted ("total return") so ETFs like TLT are judged fairly.
Yahoo data is for personal research; live trading uses the broker's own prices.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from trader.config import PROJECT_ROOT

# Our symbol -> Yahoo ticker.
YAHOO_TICKERS: dict[str, str] = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "EFA": "EFA",
    "TLT": "TLT",
    "GLD": "GLD",
    "BIL": "BIL",
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
    "EUR/USD": "EURUSD=X",
    "AUD/USD": "AUDUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "XAU/USD": "GC=F",  # gold futures as a proxy for spot gold history
}

COLUMNS = ["open", "high", "low", "close", "volume"]
CACHE_DIR = PROJECT_ROOT / "data" / "history"


def _cache_path(symbol: str, cache_dir: Path) -> Path:
    return cache_dir / f"{symbol.replace('/', '_')}.csv"


def _download(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    # US stocks use their own ticker (BRK.B -> BRK-B); crypto/forex need an explicit mapping.
    ticker = YAHOO_TICKERS.get(symbol) or (symbol.replace(".", "-") if "/" not in symbol else None)
    if ticker is None:
        raise KeyError(f"no Yahoo ticker mapped for {symbol}; add it to YAHOO_TICKERS")
    df = yf.download(ticker, start="1995-01-01", auto_adjust=True, progress=False, multi_level_index=False)
    if df.empty:
        raise RuntimeError(f"Yahoo returned no data for {symbol} ({ticker})")
    df = df.rename(columns=str.lower)[COLUMNS]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    return clean(df)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate/invalid rows and make high/low consistent with open/close."""
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    return df


def load_history(symbol: str, refresh: bool = False, max_age: timedelta = timedelta(hours=20),
                 cache_dir: Path = CACHE_DIR) -> pd.DataFrame:
    """Daily OHLCV bars, oldest first. Downloads when the cache is missing, stale, or refresh=True."""
    path = _cache_path(symbol, cache_dir)
    fresh = path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < max_age
    if fresh and not refresh:
        return pd.read_csv(path, index_col="date", parse_dates=["date"])
    df = _download(symbol)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def load_many(symbols: list[str], refresh: bool = False) -> dict[str, pd.DataFrame]:
    return {s: load_history(s, refresh=refresh) for s in symbols}
