"""Strategy interface, shared by the backtester (Phase 2) and the live engine (Phase 3).

A strategy sees daily bars, precomputes its indicators in `prepare`, then at each day's close says
what it wants to hold via `on_bar`. It never sizes orders or touches a broker: position sizing and
risk limits are applied by the caller, the same way in backtests and in live trading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd


@dataclass
class Target:
    """What the strategy wants to hold in one symbol after today's close."""

    weight: float                 # desired fraction of sleeve equity (0 = flat); risk caps may shrink it
    stop: float | None = None     # protective stop price (may trail upward day by day)
    resize: bool = False          # True = rebalance an existing position to `weight` (rotation strategies)
    reason: str = ""


@dataclass
class Holding:
    """What the sleeve currently holds, as seen by the strategy."""

    qty: float
    entry_price: float
    entry_date: pd.Timestamp
    stop: float | None = None
    high_since_entry: float = 0.0
    bars_held: int = 0


@dataclass
class Strategy(ABC):
    params: dict[str, Any]
    symbols: list[str]

    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    # Parameter grid searched in-sample during research. Keep it small: every combination is a "trial"
    # and more trials make lucky results more likely.
    param_grid: ClassVar[dict[str, list[Any]]] = {}
    default_params: ClassVar[dict[str, Any]] = {}
    min_trades: ClassVar[int] = 50            # Gate 1 minimum over the full history
    per_symbol_check: ClassVar[bool] = True   # False for rotation strategies (symbols aren't independent)

    indicators: dict[str, pd.DataFrame] = field(default_factory=dict, init=False)
    _rows: dict[str, dict[pd.Timestamp, dict[str, float]]] = field(default_factory=dict, init=False, repr=False)

    def setup(self, bars: dict[str, pd.DataFrame]) -> None:
        """Compute indicators, then index them by date for fast daily lookups."""
        self.indicators = {}
        self.prepare(bars)
        self._rows = {s: df.to_dict("index") for s, df in self.indicators.items()}

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars of history needed before the first decision."""

    def first_decision_date(self, bars: dict[str, pd.DataFrame]) -> pd.Timestamp:
        """First date the strategy could act: the earliest symbol with enough history."""
        ready = [bars[s].index[self.warmup_bars] for s in self.symbols if len(bars[s]) > self.warmup_bars]
        return min(ready)

    @abstractmethod
    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        """Fill self.indicators. Values at date t may only use data up to and including t."""

    @abstractmethod
    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        """Decide targets at the close of `date`. Symbols left out keep their current position."""

    def row(self, symbol: str, date: pd.Timestamp) -> dict[str, float] | None:
        """This symbol's indicator values at `date`, or None if it didn't trade that day."""
        return self._rows[symbol].get(date)
