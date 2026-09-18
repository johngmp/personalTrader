"""S1 / S4: trend following with an ATR trailing stop.

Long when price is above the slow moving average and the fast average is above the slow one.
Exit when that trend ends or the trailing stop (highest close since entry - k x ATR) is hit.
Position size comes from the stop distance, so volatile assets get smaller positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import ClassVar

import pandas as pd

from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.indicators import atr, sma


@dataclass
class TrendFollowing(Strategy):
    name: ClassVar[str] = "S1"
    title: ClassVar[str] = "Trend following"
    default_symbols: ClassVar[list[str]] = ["BTC/USD", "ETH/USD", "QQQ", "SPY"]
    param_grid: ClassVar[dict] = {"fast": [20, 50], "slow": [100, 150, 200], "atr_mult": [2.5, 3.5, 4.5]}
    default_params: ClassVar[dict] = {"fast": 50, "slow": 200, "atr_mult": 3.5}
    atr_n: ClassVar[int] = 20

    @property
    def warmup_bars(self) -> int:
        return int(self.params["slow"]) + 1

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            df = bars[symbol]
            self.indicators[symbol] = pd.DataFrame({
                "close": df["close"],
                "fast": sma(df["close"], int(self.params["fast"])),
                "slow": sma(df["close"], int(self.params["slow"])),
                "atr": atr(df, self.atr_n),
            })

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        k = float(self.params["atr_mult"])
        targets: dict[str, Target] = {}
        for symbol in self.symbols:
            row = self.row(symbol, date)
            if row is None or isnan(row["slow"]) or isnan(row["atr"]):
                continue
            in_trend = row["close"] > row["slow"] and row["fast"] > row["slow"]
            held = holdings.get(symbol)
            if held is None or held.qty <= 0:
                if in_trend:
                    targets[symbol] = Target(weight=1.0, stop=row["close"] - k * row["atr"], reason="trend up")
            elif not in_trend:
                targets[symbol] = Target(weight=0.0, reason="trend ended")
            else:
                trail = max(held.high_since_entry, row["close"]) - k * row["atr"]
                targets[symbol] = Target(weight=1.0, stop=max(held.stop or 0.0, trail), reason="trail stop")
        return targets


@dataclass
class FXTrend(TrendFollowing):
    """S4: the same trend rules on currency pairs and gold (goes live with OANDA in Phase 4b)."""

    name: ClassVar[str] = "S4"
    title: ClassVar[str] = "Forex & gold trend following"
    default_symbols: ClassVar[list[str]] = ["EUR/USD", "AUD/USD", "GBP/USD", "XAU/USD"]
