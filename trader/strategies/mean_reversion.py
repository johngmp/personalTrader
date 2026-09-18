"""S3: short-term mean reversion ("buy the dip in an uptrend").

Only in a long-term uptrend (price above its 200-day average), buy when the 2-day RSI is very low,
then sell on the first close back above the 5-day average or after `max_hold` days.
A protective stop sits `stop_atr` ATRs below the entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import ClassVar

import pandas as pd

from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.indicators import atr, rsi, sma


@dataclass
class MeanReversion(Strategy):
    name: ClassVar[str] = "S3"
    title: ClassVar[str] = "Buy the dip in an uptrend"
    default_symbols: ClassVar[list[str]] = ["SPY", "QQQ"]
    param_grid: ClassVar[dict] = {"rsi_entry": [5, 10, 15], "max_hold": [5, 10], "stop_atr": [2.0, 3.0]}
    default_params: ClassVar[dict] = {"rsi_entry": 10, "max_hold": 5, "stop_atr": 3.0}

    @property
    def warmup_bars(self) -> int:
        return 201

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            df = bars[symbol]
            self.indicators[symbol] = pd.DataFrame({
                "close": df["close"],
                "sma200": sma(df["close"], 200),
                "sma5": sma(df["close"], 5),
                "rsi2": rsi(df["close"], 2),
                "atr": atr(df, 14),
            })

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        targets: dict[str, Target] = {}
        for symbol in self.symbols:
            row = self.row(symbol, date)
            if row is None or any(isnan(row[c]) for c in ("sma200", "sma5", "rsi2", "atr")):
                continue
            held = holdings.get(symbol)
            if held is None or held.qty <= 0:
                if row["close"] > row["sma200"] and row["rsi2"] < float(self.params["rsi_entry"]):
                    stop = row["close"] - float(self.params["stop_atr"]) * row["atr"]
                    targets[symbol] = Target(weight=1.0, stop=stop, reason=f"RSI2 {row['rsi2']:.1f}")
            elif row["close"] > row["sma5"]:
                targets[symbol] = Target(weight=0.0, reason="bounced above 5-day average")
            elif held.bars_held >= int(self.params["max_hold"]):
                targets[symbol] = Target(weight=0.0, reason="max hold reached")
        return targets
