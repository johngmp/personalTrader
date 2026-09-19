"""S6: AI investor panel portfolio.

Once a week a Claude scheduled task (the /investor-panel skill, method adapted from virattt/ai-hedge-fund)
scores a fixed list of large US companies through several famous investors' lenses and submits target
weights. This strategy only turns those weights into targets: resize each holding to its weight, exit
anything weighted 0, and put a protective stop `stop_atr` ATRs below the price on every position.
The engine sets `weights` from the latest accepted signal; without one, the strategy does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import ClassVar

import pandas as pd

from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.indicators import atr

LARGE_CAPS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "JPM", "BRK.B", "JNJ", "XOM"]


@dataclass
class AIPortfolio(Strategy):
    name: ClassVar[str] = "S6"
    title: ClassVar[str] = "AI investor panel"
    default_symbols: ClassVar[list[str]] = LARGE_CAPS
    default_params: ClassVar[dict] = {"stop_atr": 3.0}
    min_trades: ClassVar[int] = 0
    per_symbol_check: ClassVar[bool] = False
    weights: dict[str, float] | None = None   # set by the engine from the investor panel's signal

    @property
    def warmup_bars(self) -> int:
        return 15

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            df = bars[symbol]
            self.indicators[symbol] = pd.DataFrame({"close": df["close"], "atr": atr(df, 14)})

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        if self.weights is None:
            return {}
        k = float(self.params["stop_atr"])
        targets = {}
        for symbol in self.symbols:
            row = self.row(symbol, date)
            if row is None or isnan(row["atr"]):
                continue
            w = float(self.weights.get(symbol, 0.0))
            if w <= 0:
                if symbol in holdings:
                    targets[symbol] = Target(weight=0.0, reason="investor panel: dropped")
                continue
            targets[symbol] = Target(weight=w, stop=row["close"] - k * row["atr"], resize=True,
                                     reason=f"investor panel: {w:.0%}")
        return targets
