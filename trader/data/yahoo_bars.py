"""Completed daily bars from Yahoo, for sleeves that run without a broker (shadow FX).

Same contract as the Alpaca and OANDA bar sources: only FINISHED days come back, so a live decision sees
exactly what a backtest would have seen at that close. Yahoo labels an FX day by its own date and only
publishes it once the day is done, so "completed" here means "not today's still-forming bar".

Bars are cached on disk by `load_history`, which is the same cache the research backtests read. One
download per symbol per day is plenty for a strategy that only decides at the close.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from trader.data.history import load_history
from trader.models import AssetClass, MarketClock

EASTERN = ZoneInfo("America/New_York")
MAX_CACHE_AGE = timedelta(hours=6)


def drop_forming_bar(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Drop the bar for the current FX day, which is still being written.

    The forex day rolls at 5pm New York, so before that the date to drop is today's New York date and
    after it the day has closed and the bar can be used.
    """
    if df.empty:
        return df
    ny = now.astimezone(EASTERN)
    rollover = ny.replace(hour=17, minute=0, second=0, microsecond=0)
    current_day = ny.date() if ny < rollover else (ny + timedelta(days=1)).date()
    return df[df.index < pd.Timestamp(current_day)]


class YahooDailyBars:
    def __init__(self, max_age: timedelta = MAX_CACHE_AGE) -> None:
        self._max_age = max_age

    def completed_bars(self, symbol: str, asset_class: AssetClass, now: datetime,
                       market: MarketClock | None) -> pd.DataFrame:
        df = load_history(symbol, max_age=self._max_age)
        return drop_forming_bar(df, now or datetime.now(timezone.utc))
