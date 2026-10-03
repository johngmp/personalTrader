"""Dukascopy tick data: the only intraday source here that can honestly be backtested.

Yahoo's intraday feed quotes MID prices and keeps about a week of them. On a daily strategy the spread is
a rounding error; intraday it is the whole cost, so a mid-price backtest of an intraday strategy is not
optimistic, it is meaningless. Dukascopy publishes its full tick feed free over plain HTTP with real bid
and ask, which is what makes the difference.

Format: one LZMA file per symbol per hour at
    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
where MM is ZERO-INDEXED (00 = January). Each record is 20 bytes, big-endian:
    uint32 milliseconds since the hour, uint32 ask, uint32 bid, float32 ask volume, float32 bid volume
Prices are integers scaled by 10^5 for FX majors and 10^3 for JPY pairs and metals.

The feed throttles. Hammering it earns 503s and read timeouts, so requests are limited and retried with
a backoff, and every hour already on disk is skipped.
"""

from __future__ import annotations

import lzma
import struct
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE = "https://datafeed.dukascopy.com/datafeed"
CACHE = Path("data/ticks")
RECORD = struct.Struct(">IIIff")
# The feed throttles hard and recovers slowly: after an aggressive burst the next request took 16s,
# then settled to under a second. A short retry budget gives up during exactly that recovery, so the
# backoff is long and the requests are sequential. Patience is cheaper than being blocked.
MAX_RETRIES = 6
BACKOFF_S = 4.0
PAUSE_S = 0.3                  # polite gap between requests
# Our symbol -> (Dukascopy symbol, price scale). JPY pairs and metals quote to 3 places, majors to 5.
SYMBOLS: dict[str, tuple[str, int]] = {
    "EUR/USD": ("EURUSD", 100_000),
    "GBP/USD": ("GBPUSD", 100_000),
    "AUD/USD": ("AUDUSD", 100_000),
    "USD/JPY": ("USDJPY", 1_000),
    "XAU/USD": ("XAUUSD", 1_000),
}


class DukascopyError(RuntimeError):
    pass


def _url(symbol: str, moment: datetime) -> str:
    name, _scale = SYMBOLS[symbol]
    return f"{BASE}/{name}/{moment.year}/{moment.month - 1:02d}/{moment.day:02d}/{moment.hour:02d}h_ticks.bi5"


def fetch_hour(symbol: str, moment: datetime, client=None) -> pd.DataFrame:
    """One hour of ticks as a frame of ts, bid, ask. Empty when the market was shut."""
    import httpx

    if symbol not in SYMBOLS:
        raise DukascopyError(f"{symbol} is not mapped; add it to SYMBOLS")
    _name, scale = SYMBOLS[symbol]
    owned = client is None
    client = client or httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"})
    try:
        for attempt in range(MAX_RETRIES):
            try:
                response = client.get(_url(symbol, moment))
            except Exception:
                time.sleep(BACKOFF_S * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504):   # throttled or a transient gateway
                                                        # error: back off rather than give up
                time.sleep(BACKOFF_S * (attempt + 1))
                continue
            if response.status_code == 404 or not response.content:
                return pd.DataFrame(columns=["ts", "bid", "ask"])   # shut, not broken
            if response.status_code != 200:
                raise DukascopyError(f"HTTP {response.status_code} for {symbol} {moment:%Y-%m-%d %Hh}")
            raw = lzma.LZMADecompressor().decompress(response.content)
            rows = []
            for offset in range(0, len(raw) - 19, 20):
                ms, ask, bid, _av, _bv = RECORD.unpack_from(raw, offset)
                rows.append((moment + timedelta(milliseconds=ms), bid / scale, ask / scale))
            return pd.DataFrame(rows, columns=["ts", "bid", "ask"])
        raise DukascopyError(f"gave up on {symbol} {moment:%Y-%m-%d %Hh} after {MAX_RETRIES} tries")
    finally:
        if owned:
            client.close()


def to_bars(ticks: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Tick bid/ask -> OHLC bars of the MID, carrying the mean spread so costs stay honest.

    The spread column is the point of using this source at all: it is what an intraday backtest has to
    charge, and it is the number Yahoo cannot provide.
    """
    if ticks.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "spread", "ticks"])
    frame = ticks.set_index("ts")
    mid = (frame["bid"] + frame["ask"]) / 2
    spread = frame["ask"] - frame["bid"]
    rule = f"{minutes}min"
    bars = pd.DataFrame({
        "open": mid.resample(rule).first(), "high": mid.resample(rule).max(),
        "low": mid.resample(rule).min(), "close": mid.resample(rule).last(),
        "spread": spread.resample(rule).mean(), "ticks": mid.resample(rule).count(),
    }).dropna(subset=["open"])
    return bars[bars["ticks"] > 0]


def cache_path(symbol: str, day: date, minutes: int) -> Path:
    # CSV rather than parquet: at 5-minute bars a year is ~70k rows, and avoiding a pyarrow dependency
    # for that is the better trade.
    return CACHE / f"{SYMBOLS[symbol][0]}_{minutes}m" / f"{day:%Y-%m-%d}.csv"


def download_day(symbol: str, day: date, minutes: int = 5, client=None) -> pd.DataFrame:
    """A day of bars, cached on disk. Skips the download entirely if the day is already there."""
    path = cache_path(symbol, day, minutes)
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=[0])
    frames = []
    for hour in range(24):
        moment = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        ticks = fetch_hour(symbol, moment, client)
        if not ticks.empty:
            frames.append(ticks)
        time.sleep(PAUSE_S)
    bars = to_bars(pd.concat(frames) if frames else pd.DataFrame(columns=["ts", "bid", "ask"]), minutes)
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_csv(path)           # written even when empty, so a shut day is not re-fetched forever
    return bars


def load_bars(symbol: str, minutes: int = 5) -> pd.DataFrame:
    """Every cached day for a symbol, concatenated. Downloads nothing."""
    folder = CACHE / f"{SYMBOLS[symbol][0]}_{minutes}m"
    if not folder.exists():
        return pd.DataFrame(columns=["open", "high", "low", "close", "spread", "ticks"])
    parts = [pd.read_csv(p, index_col=0, parse_dates=[0]) for p in sorted(folder.glob("*.csv"))]
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts).sort_index() if parts else pd.DataFrame(
        columns=["open", "high", "low", "close", "spread", "ticks"])
