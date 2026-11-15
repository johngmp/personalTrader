"""Gate 1 plumbing: parameter neighbours, trial counting, and an end-to-end run on synthetic data."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trader.research.evaluate import _neighbours, distinct_trials, evaluate


def test_neighbours_step_one_setting_at_a_time():
    grid = {"a": [1, 2, 3], "b": ["x", "y"]}
    assert _neighbours({"a": 2, "b": "x"}, grid) == [{"a": 1, "b": "x"}, {"a": 3, "b": "x"}, {"a": 2, "b": "y"}]
    assert _neighbours({"a": 1, "b": "y"}, grid) == [{"a": 2, "b": "y"}, {"a": 1, "b": "x"}]


def test_rerunning_the_same_grid_does_not_inflate_trials():
    """The counter now reads the trial ledger instead of research_log.jsonl, but what it must mean is
    unchanged: re-running the same test is not more searching, and trying something new is."""
    from trader.research.ledger import record_trial

    def seen(strategy, params, symbols, **kw):
        record_trial(strategy=strategy, params=params, symbols=symbols, costs={}, span=("", ""),
                     source="t", sharpe=None, **kw)

    for params in ({"a": 1}, {"a": 2}):
        seen("S1", params, ["X"])
    assert distinct_trials("S1") == 2
    seen("S1", {"a": 1}, ["X"])
    assert distinct_trials("S1") == 2, "the same settings again is the same trial"
    seen("S1", {"a": 3}, ["X"])
    assert distinct_trials("S1") == 3, "one new setting is one new trial"
    seen("S1", {"a": 1}, ["X", "Y"])
    assert distinct_trials("S1") == 4, "the same settings on a wider basket is a new trial"
    seen("S2", {"a": 1}, ["X"])
    assert distinct_trials("S1") == 4, "another strategy's searching is not this one's"
    assert distinct_trials("S2") == 1


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
