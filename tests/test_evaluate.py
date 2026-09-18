"""Gate 1 plumbing: parameter neighbours, trial counting, and an end-to-end run on synthetic data."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from trader.research.evaluate import _neighbours, distinct_trials, evaluate


def test_neighbours_step_one_setting_at_a_time():
    grid = {"a": [1, 2, 3], "b": ["x", "y"]}
    assert _neighbours({"a": 2, "b": "x"}, grid) == [{"a": 1, "b": "x"}, {"a": 3, "b": "x"}, {"a": 2, "b": "y"}]
    assert _neighbours({"a": 1, "b": "y"}, grid) == [{"a": 2, "b": "y"}, {"a": 1, "b": "x"}]


def test_rerunning_the_same_grid_does_not_inflate_trials(tmp_path):
    log = tmp_path / "log.jsonl"
    grid = [{"a": 1}, {"a": 2}]
    log.write_text(json.dumps({"strategy": "S1", "symbols": ["X"], "grid": grid}) + "\n")
    assert distinct_trials("S1", grid, ["X"], log) == 2          # same settings again
    assert distinct_trials("S1", [{"a": 3}], ["X"], log) == 3    # one new setting
    assert distinct_trials("S1", grid, ["X", "Y"], log) == 4     # new symbols = new trials
    assert distinct_trials("S2", grid, ["X"], log) == 2          # other strategies don't count


def _trending_bars(seed: int, n: int = 3000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    idx = pd.bdate_range("2005-01-03", periods=n)
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": 1.0}, index=idx)


def test_gate1_runs_end_to_end_and_never_picks_settings_with_out_of_sample_data(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("trader.research.evaluate.RESEARCH_LOG", tmp_path / "log.jsonl")
    bars = {s: _trending_bars(i) for i, s in enumerate(["BTC/USD", "ETH/USD", "QQQ", "SPY"])}
    result = evaluate("S1", settings, bars)

    assert result.eval_start < result.split_date < result.end
    best_in_sample = max(result.grid_table, key=lambda row: row["is_sharpe"])
    assert result.chosen_params == best_in_sample["params"]
    assert result.trials == 18 and result.total_trials_ever == 18
    assert {c.name for c in result.checks} >= {"Beats benchmark out-of-sample", "Enough trades",
                                               "Holds up in walk-forward", "Not a lucky parameter choice"}
    assert result.equity.index[0] == result.eval_start
