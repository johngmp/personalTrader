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
from trader.research.deflated import DSR_THRESHOLD, deflated_sharpe
from trader.research.holdout import HOLDOUT_START, reaches_past_the_seal
from trader.research.leakage import truncation_check
from trader.research.pbo import cscv_pbo
from trader.research.ledger import trial_count, trial_sharpes, verify_chain
from trader.research.metrics import Metrics, buy_and_hold, compute_metrics, risk_free_returns
from trader.strategies import REGISTRY
from trader.strategies.base import Strategy

RESEARCH_LOG = PROJECT_ROOT / "reports" / "research_log.jsonl"

# Benchmarks per strategy: None = equal-weight buy-and-hold of the strategy's own symbols; [] = cash.
#
# "Cash" as [] is a FLAT LINE: 0% return, so anything with a positive Sharpe cleared it. That is no bar
# at all -- S4 passed on 2026-09-21 with Sharpe 0.29 while being plainly worse than doing nothing with
# the money. Real cash pays interest: BIL (1-3 month T-bills) returned 1.39%/yr at Sharpe 2.84 with a
# 0.78% drawdown, against S4's 2.09%/yr at Sharpe 0.49 with a 13.6% drawdown.
# The deeper fix is in metrics.compute_metrics, which now measures Sharpe in EXCESS of cash. With that,
# BIL's own Sharpe is ~0 and "beats benchmark" means "earned more than cash, per unit of risk" --
# which is the question. BIL starts in 2007; earlier dates contribute flat cash, understating the
# hurdle (rates were higher then) and so being generous to the strategy.
BENCHMARKS: dict[str, list[str] | None] = {"S1": None, "S2": ["SPY"], "S3": ["SPY"], "S4": ["BIL"], "S8": None}


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
    deflated_sharpe: float
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
    return compute_metrics(eq, res.trades, res.exposure, risk_free=risk_free_returns(eq.index))


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
    # Before anything is measured. Every number below is deflated by how hard we have looked, so a
    # ledger that has been edited makes the whole verdict a fiction -- and the tidying always removes
    # trials, never adds them. Refusing is the only honest response; see `ledger.py`.
    ok, why = verify_chain()
    if not ok:
        raise RuntimeError(
            f"the trial ledger has been altered: {why}. Gate 1 will not score against it -- its "
            f"confidence is built on the count of past searches, and that count can no longer be "
            f"trusted. Restore reports/trials.jsonl from git, or start a new chain deliberately.")
    # Belt and braces. `research_bars` truncates at the door, so this only fires on a bars dict
    # built by hand -- an ad-hoc script, a notebook, a well-meant shortcut. Those are exactly the paths
    # that produced the nineteen reads of the old out-of-sample window, so the refusal lives where the
    # verdict is made rather than only where the data is loaded.
    past = reaches_past_the_seal(bars)
    if past:
        raise RuntimeError(
            f"these bars reach past the holdout seal at {HOLDOUT_START.date()} ({', '.join(past[:5])}"
            f"{' and more' if len(past) > 5 else ''}). Gate 1 may not see the holdout. Load data with "
            f"`trader.research.holdout.research_bars`, or open the holdout deliberately with "
            f"`trader holdout {code} --confirm ...`, which spends it.")
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
    bench_is = _cash_adjusted(bench.loc[eval_start:split])
    bench_oos = _cash_adjusted(bench.loc[split:end])

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
        wf_m = _cash_adjusted(wf_equity)
        bench_wf = _cash_adjusted(bench.loc[wf_equity.index[1]:end])

    neighbour_sharpes = [_slice_metrics(runs[_key(n)], eval_start, split).sharpe for n in _neighbours(chosen, cls.param_grid)]
    robustness = float(np.median(neighbour_sharpes) / is_m.sharpe) if neighbour_sharpes and is_m.sharpe > 0 else 0.0

    oos_symbol_pnl = {s: 0.0 for s in symbols}
    for tr in res.trades:
        if tr.exit_date >= split:
            oos_symbol_pnl[tr.symbol] += tr.pnl
    full_trades = sum(1 for tr in res.trades if tr.exit_date >= eval_start)

    # Deflated Sharpe: is the result better than the best-of-N that luck alone would have produced?
    # N is every distinct setting ever tried for this strategy, not just this run's grid -- searching
    # across sessions is still searching. Measured on the chosen strategy's full evaluated track record,
    # in excess of cash, like every other Sharpe here.
    ever = trial_count(code)
    chosen_equity = res.equity.loc[eval_start:end]
    chosen_returns = chosen_equity.pct_change().dropna()
    chosen_returns = chosen_returns - risk_free_returns(chosen_returns.index).reindex(chosen_returns.index).fillna(0.0)
    years = max((chosen_equity.index[-1] - chosen_equity.index[0]).days / 365.25, 1e-9)
    # PBO asks a different question from the deflated Sharpe, and they are kept as separate checks
    # rather than blended: deflation asks whether the WINNER beats a luck bar, PBO asks whether the
    # act of picking a winner in-sample generalises at all. The returns matrix is free -- every grid
    # cell's equity curve is already in `runs`.
    cell_returns = pd.DataFrame({_key(p): runs[_key(p)].equity.loc[eval_start:end].pct_change()
                                 for p in grid}).dropna()
    try:
        pbo = cscv_pbo(cell_returns)
    except ValueError:
        pbo = None

    spread_sharpes, spread_source = _spread_population(code, grid_table)
    dsr = deflated_sharpe(chosen_returns, ever, spread_sharpes,
                          periods_per_year=len(chosen_returns) / years)

    dd_limit = max_drawdown_pct if max_drawdown_pct is not None else limits.max_drawdown_disable_pct
    # This slot used to hold `GateCheck("Realistic backtest", True, ...)` -- a tick hardcoded to pass,
    # describing properties that `tests/test_backtest.py` already proves and asserting nothing itself.
    # Those properties now live in the report's methodology note, where documentation belongs, and the
    # slot holds a test that can actually fail. See `leakage.py` for why this is the one check that
    # catches what the deflated Sharpe provably cannot.
    leak = truncation_check(cls(params=chosen, symbols=symbols), bars)
    checks = [
        GateCheck("No look-ahead in the indicators", leak.clean, leak.detail),
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
        GateCheck("Picking settings in-sample generalises", pbo is None or pbo.passed,
                  pbo.detail if pbo else "not enough history to recombine"),
        GateCheck("Survives how hard we looked", dsr >= DSR_THRESHOLD,
                  f"deflated Sharpe {dsr:.1%} confident after {ever} distinct looks at the data "
                  f"(need {DSR_THRESHOLD:.0%}); luck bar set from {spread_source}"),
    ]
    if cls.per_symbol_check:
        traded = {s: v for s, v in oos_symbol_pnl.items() if v != 0}
        winners = [s for s, v in traded.items() if v > 0]
        checks.append(GateCheck("Works on most of its symbols", bool(traded) and len(winners) * 2 >= len(traded),
                                f"profitable out-of-sample on {len(winners)} of {len(traded)} traded symbols"))

    return Gate1Result(
        strategy=code, symbols=symbols, benchmark=bench_label, capital=capital, eval_start=eval_start,
        split_date=split, end=end, trials=len(grid), total_trials_ever=ever, chosen_params=chosen,
        is_metrics=is_m, oos_metrics=oos_m, bench_is=bench_is, bench_oos=bench_oos, wf_metrics=wf_m,
        bench_wf=bench_wf, robustness=robustness, deflated_sharpe=dsr, neighbour_sharpes=neighbour_sharpes,
        oos_symbol_pnl=oos_symbol_pnl, full_trades=full_trades, checks=checks, grid_table=grid_table,
        equity=res.equity.loc[eval_start:], bench_equity=bench.loc[eval_start:], wf_equity=wf_equity,
    )


def benchmark_symbols(code: str, symbols: list[str]) -> list[str]:
    bench = BENCHMARKS.get(code)
    return symbols if bench is None else bench


def _cash_adjusted(equity: pd.Series) -> Metrics:
    """Metrics whose Sharpe is measured in excess of cash, like every other figure in a Gate 1 report."""
    return compute_metrics(equity, risk_free=risk_free_returns(equity.index))


def _safe_sharpe(equity: pd.Series) -> float:
    try:
        return _cash_adjusted(equity).sharpe
    except ValueError:
        return float("-inf")


MIN_SPREAD_SAMPLE = 20


def _spread_population(code: str, grid_table: list[dict]) -> tuple[list[float], str]:
    """The trial Sharpes whose SPREAD sets the luck bar, and where they came from.

    This argument used to be `[row["is_sharpe"] for row in grid_table]` -- the current run's own grid.
    Those are eighteen moving-average pairs over the same prices, and they agree with each other almost
    by construction: measured on S8 their spread is 0.076. A small spread means a small luck bar no
    matter how large N is, which is why the check only ever caught S4, and caught it for having a Sharpe
    near zero rather than for the searching.

    The population has to be the same one N counts. The ledger's trials span different symbol baskets,
    cost models and versions of the strategy, so their spread is a far better estimate of how much a
    search of this kind can move a result. The grid is used only when the ledger cannot answer, and the
    report says so, because a luck bar derived from eighteen near-identical cells should not be read as
    though it meant something.
    """
    history = [s for s in trial_sharpes(code) if s is not None]
    if len(history) >= MIN_SPREAD_SAMPLE:
        return history, f"{len(history)} past trials of {code}"
    grid = [row["is_sharpe"] for row in grid_table]
    return history + grid, (f"only {len(history)} past trials recorded, so this run's own grid is "
                            f"included -- treat the deflated Sharpe as weak")


def distinct_trials(code: str, current: list[dict] | None = None, symbols: list[str] | None = None,
                    log: Path | None = None) -> int:
    """How many distinct looks this strategy has ever taken at the data.

    Now a thin read of the trial ledger, which `run_backtest` writes on every single backtest. The old
    version counted (settings, symbols) pairs out of `research_log.jsonl`, and so saw only the runs that
    went through `trader gate1`: 216 against roughly 427 real ones. The arguments are kept so existing
    callers and tests still work, and are ignored.
    """
    return trial_count(code)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=PROJECT_ROOT, check=True).stdout.strip()
    except Exception:
        return "unknown"


def log_result(result: Gate1Result, report_path: Path | None, log: Path | None = None) -> None:
    """Append one line per Gate 1 run, so the total number of variants ever tried stays visible."""
    log = log if log is not None else RESEARCH_LOG
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
