"""Deflated Sharpe: a result has to beat what luck would produce as the best of N trials.

Gate 1 had two holes already (a 0% cash benchmark, and a Sharpe with no risk-free rate). This closes the
third: without it, searching harder looks like skill.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trader.research.deflated import deflated_sharpe, expected_max_sharpe, norm_cdf, norm_ppf

RNG = np.random.default_rng(7)


def returns_with(annual_sharpe: float, years: float = 10, ppy: int = 252, vol: float = 0.15,
                 skew: float = 0.0) -> pd.Series:
    """Daily returns whose mean is pinned exactly, so the tests are not at the mercy of a draw."""
    n = int(years * ppy)
    r = RNG.normal(0, vol / math.sqrt(ppy), n)
    if skew:
        r = r - skew * (r**2 - r.var())      # bend the distribution without moving its spread much
    return pd.Series(r - r.mean() + annual_sharpe * vol / ppy)


# --- the normal distribution, implemented here because scipy is not a dependency -------------------

@pytest.mark.parametrize(("p", "expected"), [
    (0.975, 1.959964), (0.99, 2.326348), (0.5, 0.0), (0.025, -1.959964), (0.001, -3.090232),
])
def test_inverse_normal_matches_published_values(p, expected):
    assert norm_ppf(p) == pytest.approx(expected, abs=1e-6)


def test_normal_cdf_matches_published_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)


@pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5])
def test_inverse_normal_refuses_impossible_probabilities(p):
    with pytest.raises(ValueError):
        norm_ppf(p)


# --- the luck benchmark ---------------------------------------------------------------------------

def test_a_single_trial_involves_no_selection_so_the_bar_is_zero():
    assert expected_max_sharpe(1, 0.05) == 0.0


def test_the_luck_bar_rises_with_the_number_of_trials():
    bars = [expected_max_sharpe(n, 0.05) for n in (2, 10, 50, 500)]
    assert bars == sorted(bars) and bars[0] > 0


def test_identical_trials_mean_nothing_was_selected():
    """If every setting scores the same, picking the best one gained you nothing."""
    assert expected_max_sharpe(500, 0.0) == 0.0


# --- the deflated Sharpe itself ---------------------------------------------------------------------

def test_searching_harder_lowers_confidence_in_the_same_result():
    """The whole point: an identical track record is less impressive after 5,000 attempts than after 1."""
    spread = [0.9 + RNG.normal(0, 0.3) for _ in range(60)]
    r = returns_with(0.9)
    scores = [deflated_sharpe(r, n, spread, 252) for n in (1, 10, 72, 500, 5000)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 0.95 and scores[-1] < 0.5


def test_a_better_strategy_scores_higher():
    spread = [0.9 + RNG.normal(0, 0.3) for _ in range(60)]
    scores = [deflated_sharpe(returns_with(sr), 72, spread, 252) for sr in (0.0, 0.4, 0.9, 1.5)]
    assert scores == sorted(scores)
    assert scores[0] < 0.1          # a strategy with no edge is not rescued by the maths
    assert scores[-1] > 0.95


def test_a_longer_track_record_earns_more_confidence():
    spread = [0.9 + RNG.normal(0, 0.3) for _ in range(60)]
    scores = [deflated_sharpe(returns_with(0.9, years=y), 72, spread, 252) for y in (1, 5, 26)]
    assert scores == sorted(scores)


def test_a_result_that_cannot_beat_the_luck_bar_scores_badly():
    """Wildly varying trial scores mean a high bar: the best of them is probably just the luckiest."""
    wild = list(RNG.normal(0, 3.0, 200))
    assert deflated_sharpe(returns_with(0.5), 500, wild, 252) < 0.5


def test_too_short_a_track_record_earns_no_confidence():
    assert deflated_sharpe(returns_with(2.0, years=0.05), 10, [0.5], 252) == 0.0


def test_a_flat_equity_curve_earns_no_confidence():
    assert deflated_sharpe(pd.Series([0.0] * 500), 10, [0.5], 252) == 0.0


def test_fat_tails_and_negative_skew_reduce_confidence():
    """Both flatter a plain Sharpe, so they must widen the error bar rather than be ignored."""
    spread = [0.9 + RNG.normal(0, 0.3) for _ in range(60)]
    clean = returns_with(0.9)
    crashy = clean.copy()
    crashy.iloc[::250] -= 0.08          # an occasional bad day: same mean-ish, much worse tail
    crashy = crashy - crashy.mean() + clean.mean()
    assert deflated_sharpe(crashy, 72, spread, 252) < deflated_sharpe(clean, 72, spread, 252)


def test_the_gate_threshold_is_a_real_bar_not_a_formality():
    from trader.research.deflated import DSR_THRESHOLD

    assert 0.9 <= DSR_THRESHOLD < 1.0
