"""S2: cross-asset momentum rotation.

On the first trading day of each month, rank the risky assets by their return over `lookback` days.
Hold the best `top_n` equally, but only those beating cash (BIL); the rest of the sleeve sits in BIL.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import ClassVar

import pandas as pd

from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.indicators import momentum


@dataclass
class Rotation(Strategy):
    name: ClassVar[str] = "S2"
    title: ClassVar[str] = "Momentum rotation"
    default_symbols: ClassVar[list[str]] = ["SPY", "EFA", "TLT", "GLD", "BIL"]
    param_grid: ClassVar[dict] = {"lookback": [63, 126, 252], "top_n": [1, 2]}
    default_params: ClassVar[dict] = {"lookback": 126, "top_n": 1}
    min_trades: ClassVar[int] = 20
    per_symbol_check: ClassVar[bool] = False
    cash_symbol: ClassVar[str] = "BIL"

    @property
    def warmup_bars(self) -> int:
        return int(self.params["lookback"]) + 1

    def first_decision_date(self, bars: dict[str, pd.DataFrame]) -> pd.Timestamp:
        common = sorted(set.intersection(*(set(bars[s].index) for s in self.symbols)))
        return common[self.warmup_bars]

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        # Rotation compares assets on the same day, so use only dates where every asset traded.
        common = sorted(set.intersection(*(set(bars[s].index) for s in self.symbols)))
        n = int(self.params["lookback"])
        for symbol in self.symbols:
            close = bars[symbol]["close"].reindex(common)
            self.indicators[symbol] = pd.DataFrame({"close": close, "mom": momentum(close, n)})
        index = pd.DatetimeIndex(common)
        prev = pd.Series(index, index=index).shift(1)
        self._month_start = dict(zip(index, index.month != prev.dt.month))

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        if not self._month_start.get(date, False):
            return {}
        rows = {s: self.row(s, date) for s in self.symbols}
        if any(r is None or isnan(r["mom"]) for r in rows.values()):
            return {}
        moms = {s: r["mom"] for s, r in rows.items()}  # type: ignore[index]
        cash_mom = moms[self.cash_symbol]
        risky = sorted((s for s in self.symbols if s != self.cash_symbol), key=lambda s: moms[s], reverse=True)
        top_n = int(self.params["top_n"])
        chosen = [s for s in risky[:top_n] if moms[s] > cash_mom]
        weights = {s: 0.0 for s in self.symbols}
        for s in chosen:
            weights[s] = 1.0 / top_n
        weights[self.cash_symbol] = 1.0 - sum(weights[s] for s in chosen)
        return {s: Target(weight=w, resize=True, reason=f"momentum {moms[s]:+.1%}") for s, w in weights.items()}
