"""Gate 1: does a strategy deserve paper money? (PLAN.md §6)

1. Run every parameter combination over the full history (each run only ever sees the past).
2. Pick parameters using the IN-SAMPLE period only (first 70%).
3. Judge those parameters on the OUT-OF-SAMPLE period they never saw (last 30%).
4. Walk-forward: re-pick parameters each year using only the previous years, and stitch the results.
5. Check the chosen parameters aren't a lucky spike (their neighbours should do almost as well).
"""

from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from trader.config import PROJECT_ROOT, CostModel, Settings, SleeveLimits
from trader.research.backtest import BacktestResult, run_backtest
from trader.research.metrics import Metrics, buy_and_hold, compute_metrics
from trader.strategies import REGISTRY
from trader.strategies.base import Strategy

RESEARCH_LOG = PROJECT_ROOT / "reports" / "research_log.jsonl"

# Benchmarks per strategy: None = equal-weight buy-and-hold of the strategy's own symbols; [] = cash.
BENCHMARKS: dict[str, list[str] | None] = {"S1": None, "S2": ["SPY"], "S3": ["SPY"], "S4": []}


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class Gate1Result:
    strategy: str
    symbols: list[str]
    benchmark: str
    capital: float
    eval_start: pd.Timestamp
    split_date: pd.Timestamp
    end: pd.Timestamp
    trials: int
    total_trials_ever: int
    chosen_params: dict
    is_metrics: Metrics
    oos_metrics: Metrics
    bench_is: Metrics
    bench_oos: Metrics
    wf_metrics: Metrics | None
    bench_wf: Metrics | None
    robustness: float
    neighbour_sharpes: list[float]
    oos_symbol_pnl: dict[str, float]
    full_trades: int
    checks: list[GateCheck]
    grid_table: list[dict]               # every combination with its in-sample and out-of-sample Sharpe
    equity: pd.Series                    # chosen parameters, full period
    bench_equity: pd.Series
    wf_equity: pd.Series | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


def _grid(cls: type[Strategy]) -> list[dict]:
    keys = list(cls.param_grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(cls.param_grid[k] for k in keys))]


def _slice_metrics(res: BacktestResult, start, end) -> Metrics:
    eq = res.equity.loc[start:end]
    return compute_metrics(eq, res.trades, res.exposure)


def _neighbours(params: dict, grid: dict[str, list]) -> list[dict]:
    out = []
    for key, values in grid.items():
        i = values.index(params[key])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                out.append({**params, key: values[j]})
    return out


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def strategy_setup(code: str, settings: Settings) -> tuple[type[Strategy], list[str], float, SleeveLimits]:
    cls = REGISTRY[code]
    sleeve = settings.sleeves.get(code)
    symbols = list(sleeve.symbols) if sleeve else list(cls.default_symbols)  # type: ignore[attr-defined]
    capital = sleeve.capital_usd if sleeve else 10_000.0
    limits = settings.sleeve_limits(code) if sleeve else settings.risk.sleeve_defaults
    return cls, symbols, capital, limits


def symbol_costs(symbols: list[str], settings: Settings) -> dict[str, CostModel]:
    out = {}
    for s in symbols:
        asset_class = settings.asset_class(s)
        if asset_class is None:
            raise KeyError(f"{s} is missing from config/universe.yaml")
        out[s] = settings.costs[asset_class]
    return out


def evaluate(code: str, settings: Settings, bars: dict[str, pd.DataFrame], oos_fraction: float = 0.3,
             wf_train_years: int = 4, wf_test_years: int = 1, max_drawdown_pct: float | None = None) -> Gate1Result:
    cls, symbols, capital, limits = strategy_setup(code, settings)
    costs = symbol_costs(symbols, settings)
    grid = _grid(cls)
    runs = {_key(p): run_backtest(cls(params=p, symbols=symbols), bars, capital, limits, costs) for p in grid}

    # Measure from the first day every parameter set is able to trade (no idle warm-up period).
    eval_start = max(cls(params=p, symbols=symbols).first_decision_date(bars) for p in grid)
    calendar = next(iter(runs.values())).equity.index
    calendar = calendar[calendar >= eval_start]
    end = calendar[-1]
    split = eval_start + (end - eval_start) * (1 - oos_fraction)
    split = calendar[calendar >= split][0]

    grid_table = []
    for p in grid:
        r = runs[_key(p)]
        grid_table.append({"params": p, "is_sharpe": _slice_metrics(r, eval_start, split).sharpe,
                           "oos_sharpe": _slice_metrics(r, split, end).sharpe})
    best = max(grid_table, key=lambda row: row["is_sharpe"])
    chosen = best["params"]
    res = runs[_key(chosen)]
    is_m, oos_m = _slice_metrics(res, eval_start, split), _slice_metrics(res, split, end)

    bench_syms = benchmark_symbols(code, symbols)
    bench_label = "cash (0%)" if not bench_syms else "buy & hold " + ", ".join(bench_syms)
    bench_costs = {s: c.total_bps + c.broker_fee_bps for s, c in symbol_costs(bench_syms, settings).items()}
    bench = buy_and_hold(bars, bench_syms, calendar, capital, bench_costs)
    bench_is = compute_metrics(bench.loc[eval_start:split])
    bench_oos = compute_metrics(bench.loc[split:end])

    # Walk-forward: choose on the previous `wf_train_years`, trade the next `wf_test_years`.
    wf_returns = []
    t = eval_start + pd.DateOffset(years=wf_train_years)
    while t < end:
        t_end = min(t + pd.DateOffset(years=wf_test_years), end)
        train_start = t - pd.DateOffset(years=wf_train_years)
        pick = max(grid, key=lambda p: _safe_sharpe(runs[_key(p)].equity.loc[train_start:t]))
        wf_returns.append(runs[_key(pick)].equity.loc[t:t_end].pct_change().dropna())
        t = t_end
    wf_equity = wf_m = bench_wf = None
    if wf_returns:
        rets = pd.concat(wf_returns)
        rets = rets[~rets.index.duplicated(keep="first")]
        wf_equity = capital * (1 + rets).cumprod()
        wf_equity = pd.concat([pd.Series([capital], index=[rets.index[0] - pd.Timedelta(days=1)]), wf_equity])
        wf_m = compute_metrics(wf_equity)
        bench_wf = compute_metrics(bench.loc[wf_equity.index[1]:end])

    neighbour_sharpes = [_slice_metrics(runs[_key(n)], eval_start, split).sharpe for n in _neighbours(chosen, cls.param_grid)]
    robustness = float(np.median(neighbour_sharpes) / is_m.sharpe) if neighbour_sharpes and is_m.sharpe > 0 else 0.0

    oos_symbol_pnl = {s: 0.0 for s in symbols}
    for tr in res.trades:
        if tr.exit_date >= split:
            oos_symbol_pnl[tr.symbol] += tr.pnl
    full_trades = sum(1 for tr in res.trades if tr.exit_date >= eval_start)

    dd_limit = max_drawdown_pct if max_drawdown_pct is not None else limits.max_drawdown_disable_pct
    checks = [
        GateCheck("Realistic backtest", True, "next-day fills, spread + slippage + fees on every trade, stops can gap"),
        GateCheck("Beats benchmark out-of-sample", oos_m.sharpe > bench_oos.sharpe,
                  f"Sharpe {oos_m.sharpe:.2f} vs {bench_label} {bench_oos.sharpe:.2f}"),
        GateCheck(f"Out-of-sample drawdown ≤ {dd_limit:.0f}%", oos_m.max_drawdown >= -dd_limit / 100,
                  f"worst drop {oos_m.max_drawdown:.1%}"),
        GateCheck("Enough trades", full_trades >= cls.min_trades,
                  f"{full_trades} completed trades (need {cls.min_trades})"),
        GateCheck("Holds up in walk-forward", bool(wf_m and wf_m.sharpe > 0 and wf_m.sharpe >= 0.5 * is_m.sharpe),
                  f"walk-forward Sharpe {wf_m.sharpe:.2f} vs in-sample {is_m.sharpe:.2f}" if wf_m else "not enough history"),
        GateCheck("Not a lucky parameter choice", robustness >= 0.7,
                  f"neighbouring settings keep {robustness:.0%} of the Sharpe (need 70%)"),
    ]
    if cls.per_symbol_check:
        traded = {s: v for s, v in oos_symbol_pnl.items() if v != 0}
        winners = [s for s, v in traded.items() if v > 0]
        checks.append(GateCheck("Works on most of its symbols", bool(traded) and len(winners) * 2 >= len(traded),
                                f"profitable out-of-sample on {len(winners)} of {len(traded)} traded symbols"))

    return Gate1Result(
        strategy=code, symbols=symbols, benchmark=bench_label, capital=capital, eval_start=eval_start,
        split_date=split, end=end, trials=len(grid), total_trials_ever=distinct_trials(code, grid, symbols), chosen_params=chosen,
        is_metrics=is_m, oos_metrics=oos_m, bench_is=bench_is, bench_oos=bench_oos, wf_metrics=wf_m,
        bench_wf=bench_wf, robustness=robustness, neighbour_sharpes=neighbour_sharpes,
        oos_symbol_pnl=oos_symbol_pnl, full_trades=full_trades, checks=checks, grid_table=grid_table,
        equity=res.equity.loc[eval_start:], bench_equity=bench.loc[eval_start:], wf_equity=wf_equity,
    )


def benchmark_symbols(code: str, symbols: list[str]) -> list[str]:
    bench = BENCHMARKS.get(code)
    return symbols if bench is None else bench


def _safe_sharpe(equity: pd.Series) -> float:
    try:
        return compute_metrics(equity).sharpe
    except ValueError:
        return float("-inf")


def distinct_trials(code: str, current: list[dict], symbols: list[str], log: Path = RESEARCH_LOG) -> int:
    """Distinct (settings, symbols) combinations ever tested for a strategy, including this run.

    Re-running the same test doesn't add trials; trying new settings or symbols does.
    """
    seen = {_key({**p, "_symbols": sorted(symbols)}) for p in current}
    if log.exists():
        for line in log.read_text().splitlines():
            entry = json.loads(line)
            if entry.get("strategy") == code:
                for p in entry.get("grid", []):
                    seen.add(_key({**p, "_symbols": sorted(entry.get("symbols", []))}))
    return len(seen)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=PROJECT_ROOT, check=True).stdout.strip()
    except Exception:
        return "unknown"


def log_result(result: Gate1Result, report_path: Path | None, log: Path = RESEARCH_LOG) -> None:
    """Append one line per Gate 1 run, so the total number of variants ever tried stays visible."""
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy": result.strategy, "symbols": result.symbols, "trials": result.trials,
        "grid": [row["params"] for row in result.grid_table],
        "chosen_params": result.chosen_params, "verdict": "PASS" if result.passed else "FAIL",
        "is_sharpe": round(result.is_metrics.sharpe, 3), "oos_sharpe": round(result.oos_metrics.sharpe, 3),
        "benchmark_oos_sharpe": round(result.bench_oos.sharpe, 3),
        "wf_sharpe": round(result.wf_metrics.sharpe, 3) if result.wf_metrics else None,
        "failed_checks": [c.name for c in result.checks if not c.passed],
        "report": str(report_path.relative_to(PROJECT_ROOT)) if report_path else None,
        "git_commit": _git_commit(),
    }
    with log.open("a") as f:
        f.write(json.dumps(entry) + "\n")
