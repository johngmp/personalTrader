"""Backtest an intraday strategy, charging the spread that was actually quoted.

This exists separately from `run_backtest` for one reason: the daily backtester charges a fixed cost in
basis points from config, which is right when the spread is a rounding error against a multi-week hold
and wrong when it IS the cost. Dukascopy bars carry the mean spread for each bar, so an entry pays half
the spread quoted at that moment and so does the exit. Nothing is assumed.

Everything else follows the daily backtester's rules, because they are what stop a backtest lying:
  - a breakout fills at the level, never at the bar's close
  - a stop fills at the worse of the stop and the bar's open, so a gap through it costs what it cost
  - position size comes from the stop distance, so the 1% risk rule holds here too
  - long only, matching the risk gate
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class IntradayTrade:
    day: object
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry: float
    exit: float
    qty: float
    stop: float
    spread_cost: float      # dollars, both sides, from the quoted spread
    pnl: float              # after spread
    reason: str

    @property
    def r_multiple(self) -> float | None:
        risk = self.qty * (self.entry - self.stop)
        return self.pnl / risk if risk > 0 else None


def run(strategy, bars: pd.DataFrame, capital: float = 10_000.0,
        risk_per_trade: float = 0.01, slippage_pips: float = 0.1) -> dict:
    """Run `strategy` over `bars` (tz-aware UTC, with open/high/low/close/spread).

    `slippage_pips` is charged on top of the quoted spread. A breakout order is filled by definition in
    a fast market, and assuming you always get the exact level is the single most flattering assumption
    an intraday backtest can make.
    """
    equity = capital
    trades: list[IntradayTrade] = []
    curve: list[tuple[pd.Timestamp, float]] = []

    for day, session in strategy.sessions(bars):
        plan = strategy.plan(session)
        if plan is None:
            if curve:
                curve.append((session.index[-1], equity))
            continue

        entry_ts, entry, stop = plan["entry_ts"], plan["entry"], plan["stop"]
        after = session[session.index >= entry_ts]
        if after.empty or entry <= stop:
            continue

        # Size from the stop distance: the same 1% rule the live risk gate applies.
        qty = (equity * risk_per_trade) / (entry - stop)
        spread_at_entry = float(after["spread"].iloc[0])
        slip = slippage_pips / 1e4
        fill_in = entry + spread_at_entry / 2 + slip        # pay the ask, plus slippage

        # A mean-reverting plan has somewhere to go; a trend-following one does not. When both a stop and
        # a target sit inside the same bar we take the STOP, because a 5-minute bar does not say which
        # came first and assuming the good one is how a backtest flatters itself.
        target = plan.get("target")
        exit_price, exit_ts, reason = None, after.index[-1], "session end"
        for ts, bar in after.iterrows():
            if bar["low"] <= stop:
                exit_price = min(float(bar["open"]), stop)   # gapped through: take the worse
                exit_ts, reason = ts, "stop"
                break
            if target is not None and bar["high"] >= target:
                exit_price = target                          # a limit fills AT its level, never better
                exit_ts, reason = ts, "target"
                break
        if exit_price is None:
            exit_price = float(after["close"].iloc[-1])
            exit_ts = after.index[-1]

        spread_at_exit = float(after.loc[exit_ts, "spread"]) if exit_ts in after.index else spread_at_entry
        fill_out = exit_price - spread_at_exit / 2 - slip   # sell into the bid, plus slippage

        spread_cost = qty * (spread_at_entry / 2 + spread_at_exit / 2 + 2 * slip)
        pnl = qty * (fill_out - fill_in)
        equity += pnl
        trades.append(IntradayTrade(day=day, entry_ts=entry_ts, exit_ts=exit_ts, entry=fill_in,
                                    exit=fill_out, qty=qty, stop=stop, spread_cost=spread_cost,
                                    pnl=pnl, reason=reason))
        curve.append((exit_ts, equity))

    return {"trades": trades, "equity": equity, "capital": capital,
            "curve": pd.Series([v for _t, v in curve], index=[t for t, _v in curve]) if curve else pd.Series(dtype=float)}


def summarise(result: dict) -> dict:
    """Headline numbers, including what the spread took -- the figure this whole exercise is about."""
    import numpy as np

    trades = result["trades"]
    if not trades:
        return {"trades": 0}
    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    spread = sum(t.spread_cost for t in trades)
    gross = float(pnls.sum() + spread)
    days = (trades[-1].exit_ts - trades[0].entry_ts).days or 1
    years = days / 365.25
    total = result["equity"] / result["capital"] - 1
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "net_pnl": float(pnls.sum()),
        "gross_pnl": gross,
        "spread_paid": spread,
        "spread_share_of_gross": spread / abs(gross) if gross else float("inf"),
        "total_return": total,
        "cagr": (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else 0.0,
        "avg_r": float(np.mean(rs)) if rs else 0.0,
        "years": years,
    }
