"""Completed daily bars from Alpaca for live decisions.

Only FINISHED days are returned, so live decisions see exactly what the backtest saw at each close:
- stocks: a day counts once its regular session has closed (+15 min for the bar to settle)
- crypto: a day is the UTC calendar day, finished at 00:00 UTC (10am AEST)
Stock bars are split- and dividend-adjusted like the Yahoo data used in research.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from trader.data.history import clean
from trader.models import AssetClass, MarketClock

EASTERN = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 450  # ~300 stock sessions: enough for a 200-day average plus margin


class BarSource(Protocol):
    def completed_bars(self, symbol: str, asset_class: AssetClass, now: datetime,
                       market: MarketClock | None) -> pd.DataFrame: ...


def completed_only(df: pd.DataFrame, asset_class: AssetClass, now: datetime, market: MarketClock | None) -> pd.DataFrame:
    """Drop today's still-forming bar."""
    if df.empty:
        return df
    if asset_class is AssetClass.CRYPTO:
        today = pd.Timestamp(now.astimezone(timezone.utc).date())
        return df[df.index < today]
    today = pd.Timestamp(now.astimezone(EASTERN).date())
    session_done = market is not None and not market.is_open and market.last_close.astimezone(EASTERN).date() == today.date() \
        and now >= market.last_close + timedelta(minutes=15)
    return df[(df.index < today) | ((df.index == today) & session_done)]


class AlpacaDailyBars:
    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

        from trader.brokers.alpaca import with_timeout

        self._stocks = with_timeout(StockHistoricalDataClient(api_key, secret_key))
        self._crypto = with_timeout(CryptoHistoricalDataClient(api_key, secret_key))

    def completed_bars(self, symbol: str, asset_class: AssetClass, now: datetime,
                       market: MarketClock | None) -> pd.DataFrame:
        from trader.brokers.alpaca import _retry_reads

        return _retry_reads(self._completed_bars)(symbol, asset_class, now, market)

    def _completed_bars(self, symbol: str, asset_class: AssetClass, now: datetime,
                       market: MarketClock | None) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start = now - timedelta(days=LOOKBACK_DAYS)
        if asset_class is AssetClass.CRYPTO:
            raw = self._crypto.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                                                 start=start)).df
            tz = timezone.utc
        elif asset_class is AssetClass.US_EQUITY:
            # The free data plan allows full-market (SIP) history up to 15 minutes ago.
            raw = self._stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=now - timedelta(minutes=16),
                adjustment=Adjustment.ALL, feed=DataFeed.SIP)).df
            tz = EASTERN
        else:
            raise ValueError(f"Alpaca has no {asset_class.value} data")
        if raw.empty:
            return raw
        df = raw.xs(symbol, level="symbol")[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.DatetimeIndex([pd.Timestamp(ts.astimezone(tz).date()) for ts in df.index], name="date")
        return completed_only(clean(df), asset_class, now, market)
