"""B0: buy-and-hold benchmark.

Buys equal shares of its symbols once and never sells, so every strategy can be compared with simply
holding, under the same paper-trading conditions and costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from trader.strategies.base import Holding, Strategy, Target


@dataclass
class BuyAndHold(Strategy):
    name: ClassVar[str] = "B0"
    title: ClassVar[str] = "Buy-and-hold benchmark"
    default_symbols: ClassVar[list[str]] = ["SPY", "QQQ", "BTC/USD"]
    min_trades: ClassVar[int] = 0

    @property
    def warmup_bars(self) -> int:
        return 0

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            self.indicators[symbol] = pd.DataFrame({"close": bars[symbol]["close"]})

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        weight = 1.0 / len(self.symbols)
        return {s: Target(weight=weight, reason="benchmark: buy and hold")
                for s in self.symbols if self.row(s, date) is not None and s not in holdings}
