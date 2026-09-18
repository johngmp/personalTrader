"""Performance statistics from an equity curve and a trade list."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from trader.research.backtest import TradeRecord


@dataclass
class Metrics:
    start: str
    end: str
    years: float
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float       # negative, e.g. -0.23
    calmar: float
    exposure: float           # average fraction of equity invested
    n_trades: int
    win_rate: float
    profit_factor: float
    avg_r: float
    avg_bars_held: float

    def as_dict(self) -> dict:
        return asdict(self)


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0


def compute_metrics(equity: pd.Series, trades: list[TradeRecord] | None = None,
                    exposure: pd.Series | None = None) -> Metrics:
    equity = equity.dropna()
    if len(equity) < 2 or equity.iloc[0] <= 0:
        raise ValueError("need at least two positive equity points")
    returns = equity.pct_change().dropna()
    days = (equity.index[-1] - equity.index[0]).days
    years = max(days / 365.25, 1e-9)
    periods_per_year = len(returns) / years  # ~252 for stocks, ~365 when crypto trades every day
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    std = returns.std()
    downside = returns[returns < 0].std()
    sharpe = float(returns.mean() / std * np.sqrt(periods_per_year)) if std > 0 else 0.0
    sortino = float(returns.mean() / downside * np.sqrt(periods_per_year)) if downside and downside > 0 else 0.0
    mdd = max_drawdown(equity)

    trades = [t for t in trades or [] if equity.index[0] <= t.exit_date <= equity.index[-1]]
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    return Metrics(
        start=str(equity.index[0].date()), end=str(equity.index[-1].date()), years=round(years, 2),
        total_return=float(total), cagr=float(cagr), volatility=float(std * np.sqrt(periods_per_year)),
        sharpe=sharpe, sortino=sortino, max_drawdown=mdd,
        calmar=float(cagr / abs(mdd)) if mdd < 0 else 0.0,
        exposure=float(exposure.loc[equity.index[0]:equity.index[-1]].mean()) if exposure is not None else 1.0,
        n_trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=float(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0.0,
        avg_r=float(np.mean(rs)) if rs else 0.0,
        avg_bars_held=float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
    )


def buy_and_hold(bars: dict[str, pd.DataFrame], symbols: list[str], calendar: pd.DatetimeIndex,
                 capital: float, cost_bps: dict[str, float]) -> pd.Series:
    """Equal-weight buy-and-hold of `symbols` on `calendar`, bought at the first close, entry costs paid.

    Assets without data yet at the start are bought on their first available day with their share of cash.
    """
    if not symbols:
        return pd.Series(capital, index=calendar, name="benchmark")
    closes = pd.DataFrame({s: bars[s]["close"] for s in symbols}).reindex(calendar).ffill()
    share = capital / len(symbols)
    total = pd.Series(0.0, index=calendar)
    for s in symbols:
        col = closes[s]
        first = col.first_valid_index()
        if first is None:
            total += share
            continue
        qty = share * (1 - cost_bps[s] / 10_000) / col.loc[first]
        total += (col * qty).where(col.index >= first, share).fillna(share)
    return total.rename("benchmark")
