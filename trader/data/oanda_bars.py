"""Completed daily candles from OANDA for live forex decisions.

OANDA's daily candles close at 5pm New York time (7am AEST). Only complete candles are used, and each is
labelled with the New York date on which it closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from trader.brokers.oanda import PRACTICE_URL, parse_time, to_instrument
from trader.data.history import clean
from trader.models import AssetClass, MarketClock

EASTERN = ZoneInfo("America/New_York")


def candles_to_frame(candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles:
        if not c.get("complete"):
            continue
        close_time = parse_time(c["time"]) + timedelta(days=1)  # type: ignore[operator]
        mid = c["mid"]
        rows.append({"date": pd.Timestamp(close_time.astimezone(EASTERN).date()), "open": float(mid["o"]),
                     "high": float(mid["h"]), "low": float(mid["l"]), "close": float(mid["c"]),
                     "volume": float(c.get("volume", 0))})
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return clean(pd.DataFrame(rows).set_index("date"))


class OandaDailyBars:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=PRACTICE_URL, timeout=15,
                                              headers={"Authorization": f"Bearer {token}",
                                                       "Accept-Datetime-Format": "RFC3339"})

    def completed_bars(self, symbol: str, asset_class: AssetClass, now: datetime,
                       market: MarketClock | None) -> pd.DataFrame:
        r = self._client.get(f"/v3/instruments/{to_instrument(symbol)}/candles",
                             params={"granularity": "D", "count": 320, "price": "M"})
        r.raise_for_status()
        return candles_to_frame(r.json().get("candles", []))
