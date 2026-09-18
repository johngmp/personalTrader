---
name: research-lab
description: Design, backtest and judge a trading strategy for the personal trader (Gate 1). Use when the user asks to test, create, improve, tune or compare a strategy, or asks whether a strategy "works" / "makes money".
---

# Research lab

You are helping design and test rule-based strategies for a **paper-trading** tournament. The goal is an honest answer to "does this beat simply holding?", not a flattering backtest. Read `PLAN.md` §4 and §6 if you haven't this session.

## Hard rules

1. **Never touch `config/risk.yaml`.** Risk limits are the user's alone.
2. **Never enable a sleeve** (`enabled: true` in `config/sleeves.yaml`) or change a sleeve's `limits:` without the user's explicit approval in chat.
3. **Never tune on out-of-sample results.** Settings are picked on the in-sample period only (the code does this). If you change a strategy or its grid *after* seeing a Gate 1 report, say so plainly. The new run is a new set of trials, and the out-of-sample period is no longer untouched for that idea.
4. **Keep grids small** (≤ ~30 combinations). Every combination is a trial, and the report shows the running total. Fewer, well-motivated variants beat big sweeps.
5. **Explain *why* an idea should work before testing it** (what behaviour or risk premium it captures). Ideas with no reason are data-mining.
6. Backtests fill at the next day's open with costs from `config/costs.yaml`. Don't add intraday stops, same-bar fills or leverage to make results look better.

## Workflow

1. **Hypothesis.** Write 2–4 sentences: the idea, why it might work, which markets, which benchmark it must beat.
2. **Implement** a `Strategy` subclass in `trader/strategies/` (see `trend.py` for the pattern):
   - indicators in `prepare()` may only use data up to each date
   - `on_bar()` returns `Target`s; it never sizes orders (sizing and limits are applied by the engine)
   - set `name`, `title`, a module docstring, `default_symbols`, a small `param_grid`, `default_params`, `min_trades`
   - register it in `trader/strategies/__init__.py`, add a benchmark in `BENCHMARKS` (`trader/research/evaluate.py`), and add a sleeve entry (disabled) plus any new symbols in `config/universe.yaml` and `trader/data/history.py`
3. **Test** it: add a test in `tests/` showing a few entries/exits on synthetic bars, then `uv run --group dev pytest -q`.
4. **Quick look:** `uv run trader backtest <CODE>` (default settings only, for sanity).
5. **Gate 1:** `uv run trader gate1 <CODE>`. This writes an HTML report to `reports/backtests/` and a line to `reports/research_log.jsonl`.
6. **Explain the report** in plain language: verdict, which checks failed and why, the out-of-sample comparison with the benchmark (return *and* risk), the caveats, and what would be a principled next step. Don't recommend going live; Gate 2 (90+ days paper) decides that.
7. **If it passes**, ask the user whether to enable the sleeve for paper trading (Phase 3+). Don't enable it yourself.

## Reading results

- **Sharpe**: return per unit of volatility. The main comparison with the benchmark.
- **Out-of-sample**: the last ~30% of history, never used for picking settings. This is the number that matters in a backtest.
- **Walk-forward**: settings re-picked each year from the previous 4 years only. Closer to real use.
- **Robustness**: neighbouring settings should keep ≥ 70% of the Sharpe. A lone spike means luck.
- A strategy can pass on Sharpe while earning *less* than buy-and-hold (it takes less risk). Always say so when that's the case.
