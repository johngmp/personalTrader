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
from trader.strategies.indicators import atr, efficiency_ratio, sma


@dataclass
class TrendFollowing(Strategy):
    name: ClassVar[str] = "S1"
    title: ClassVar[str] = "Trend following"
    default_symbols: ClassVar[list[str]] = ["BTC/USD", "ETH/USD", "QQQ", "SPY"]
    param_grid: ClassVar[dict] = {"fast": [20, 50], "slow": [100, 150, 200], "atr_mult": [2.5, 3.5, 4.5]}
    default_params: ClassVar[dict] = {"fast": 50, "slow": 200, "atr_mult": 3.5}
    atr_n: ClassVar[int] = 20
    # Regime filter: skip NEW entries when the market is chopping. Kept OUT of param_grid and defaulting
    # to 0.0 (off) because it failed Gate 1 on 2026-10-01: in-sample selection turned it off for S1, and
    # for S8 it chose 0.25 but came out slightly worse out-of-sample (0.778 -> 0.768) and in walk-forward
    # (0.671 -> 0.638), while tripling the grid from 18 to 54 trials.
    # Worth keeping the code: at FIXED parameters it cut out-of-sample drawdown a lot (S1 -16.3% -> -8.2%,
    # S8 -22.7% -> -13.1%). That was measured by looking at the out-of-sample period, so it cannot be
    # acted on -- but it suggests retesting against a drawdown-aware objective rather than Sharpe, which
    # does not reward the thing this filter actually does.
    er_n: ClassVar[int] = 20

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
                "er": efficiency_ratio(df["close"], self.er_n),
            })

    def on_bar(self, date: pd.Timestamp, holdings: dict[str, Holding]) -> dict[str, Target]:
        k = float(self.params["atr_mult"])
        min_er = float(self.params.get("min_er", 0.0))
        targets: dict[str, Target] = {}
        for symbol in self.symbols:
            row = self.row(symbol, date)
            if row is None or isnan(row["slow"]) or isnan(row["atr"]):
                continue
            in_trend = row["close"] > row["slow"] and row["fast"] > row["slow"]
            held = holdings.get(symbol)
            if held is None or held.qty <= 0:
                trending_cleanly = min_er <= 0 or (not isnan(row["er"]) and row["er"] >= min_er)
                if in_trend and trending_cleanly:
                    targets[symbol] = Target(weight=1.0, stop=row["close"] - k * row["atr"], reason="trend up")
            elif not in_trend:
                targets[symbol] = Target(weight=0.0, reason="trend ended")
            else:
                trail = max(held.high_since_entry, row["close"]) - k * row["atr"]
                targets[symbol] = Target(weight=1.0, stop=max(held.stop or 0.0, trail), reason="trail stop")
        return targets


@dataclass
class BroadTrend(TrendFollowing):
    """S8: the same trend rules across ~20 markets that tend not to move together.

    Research on trend following (e.g. Hurst, Ooi & Pedersen, "A Century of Evidence on Trend-Following
    Investing") finds its edge comes mostly from diversification across many markets, not from one asset.
    """

    name: ClassVar[str] = "S8"
    title: ClassVar[str] = "Trend following, diversified"
    # Widened 19 -> 40 on 2026-10-03. Breadth is this strategy's only real edge, and it is also what
    # sets how fast it can be judged: measured over 31 years, 19 markets produce 7.0 completed trades a
    # month and 40 produce 15.1, so the 50 trades Gate 2 wants arrive in 3.3 months instead of 7.2.
    default_symbols: ClassVar[list[str]] = [
        "SPY", "QQQ", "IWM", "MDY",                  # US equity: large, tech, small, mid
        "EFA", "EEM", "EWJ", "EWZ", "FXI", "EWU", "EWA",   # developed, emerging, Japan, Brazil, China, UK, Australia
        "XLF", "XLK", "XLI", "XLP", "XLY", "XLB", "XLE", "XLU", "XLV",   # all nine US sectors
        "TLT", "IEF", "SHY", "TIP",                  # US government bonds: long, medium, short, inflation-linked
        "LQD", "HYG",                                # investment-grade and high-yield credit
        "GLD", "SLV", "GDX",                         # gold, silver, gold miners
        "DBC", "USO", "UNG", "DBA", "VNQ",           # broad commodities, oil, natural gas, agriculture, real estate
        "UUP", "FXE", "FXY",                         # US dollar, euro, yen
        "BTC/USD", "ETH/USD", "SOL/USD",             # crypto
    ]


@dataclass
class FXTrend(TrendFollowing):
    """S4: the same trend rules on currency pairs and gold (goes live with OANDA in Phase 4b)."""

    name: ClassVar[str] = "S4"
    title: ClassVar[str] = "Forex & gold trend following"
    default_symbols: ClassVar[list[str]] = ["EUR/USD", "AUD/USD", "GBP/USD", "XAU/USD"]
