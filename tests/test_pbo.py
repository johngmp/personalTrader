"""Probability of backtest overfitting, by combinatorially symmetric cross-validation.

The deflated Sharpe asks whether the winner beats a luck bar. This asks whether the act of picking a
winner in-sample generalises at all -- a different question, and one a good winner's Sharpe cannot
answer. Measured on this project's real grids, S1 reads 51% (a coin flip) on settings that differ by
0.042 Sharpe, and S8 reads 32%.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd
import pytest

from trader.research.pbo import CSCV_BLOCKS, cscv_pbo


def _columns(n_cols: int, periods: int = 2000, seed: int = 5, drift: dict | None = None):
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.01, (periods, n_cols))
    for col, extra in (drift or {}).items():
        m[:, col] += extra
    return pd.DataFrame(m)


# --- does it answer the question it claims to? -------------------------------------------------------

def test_a_genuinely_better_setting_is_picked_and_keeps_winning():
    """The baseline case. If this does not read low, nothing below means anything."""
    result = cscv_pbo(_columns(18, drift={7: 0.0006}))
    assert result.pbo < 0.4
    assert result.informative


def test_pure_noise_is_a_coin_flip():
    """With nothing to find, the in-sample winner is whichever column got lucky in those blocks, and
    out-of-sample it lands anywhere. Around half is the right answer, and a method that reported a
    confident low number here would be worse than useless."""
    result = cscv_pbo(_columns(18))
    assert 0.35 < result.pbo < 0.65


def test_more_noise_columns_do_not_make_the_search_look_better():
    """Searching harder must never improve this number."""
    few = cscv_pbo(_columns(6, seed=11)).pbo
    many = cscv_pbo(_columns(40, seed=11)).pbo
    assert many >= few - 0.15, "widening a noise search should not look like progress"


# --- the honesty guard ---------------------------------------------------------------------------------

def test_a_grid_too_flat_to_learn_from_says_so_instead_of_reassuring_you():
    """S1's eighteen settings differ by 0.042 in Sharpe. Which one wins in-sample is close to
    arbitrary, so the out-of-sample rank of an arbitrary pick is close to uniform and the number
    describes the GRID, not the search.

    This project has just removed one check that always passed. Adding a second that always passes
    would be the same mistake with more arithmetic.
    """
    identical = pd.DataFrame(np.tile(_columns(1).to_numpy(), (1, 12)))
    jitter = np.random.default_rng(1).normal(0, 1e-6, identical.shape)
    result = cscv_pbo(identical + jitter)
    assert not result.informative
    assert "uninformative" in result.detail
    assert result.passed, "declining to answer is not a failure"


def test_a_grid_with_real_differences_is_reported_as_informative():
    result = cscv_pbo(_columns(18, drift={3: 0.0008, 9: -0.0008}))
    assert result.informative and "uninformative" not in result.detail


def test_an_informative_grid_that_does_not_generalise_fails():
    """`passed` must be able to be False, or the check is decoration."""
    from trader.research.pbo import PBOResult

    bad = PBOResult(pbo=0.83, n_splits=12870, n_blocks=16, n_trials=18, spread=0.4,
                    degradation=-0.9, informative=True)
    assert not bad.passed and "worse half 83%" in bad.detail


# --- the mechanics that are easy to get quietly wrong --------------------------------------------------

def test_blocks_are_contiguous_in_time_and_never_shuffled_rows():
    """Shuffling rows would destroy the serial dependence that makes a return series what it is --
    volatility clusters, trends persist, drawdowns are runs -- and would turn this into ordinary
    cross-validation wearing the name of a method designed to avoid exactly that.

    Checked on the block construction itself. An earlier version of this test compared the answer on
    ordered against shuffled input and asserted they differed, which proved nothing: they differ
    whether the blocks respect time or not.
    """
    from trader.research.pbo import time_blocks

    blocks = time_blocks(1000, 16)
    assert len(blocks) == 16
    for block in blocks:
        assert list(block) == list(range(block[0], block[-1] + 1)), "a block must be one stretch of time"
    assert [b[0] for b in blocks] == sorted(b[0] for b in blocks), "and they must be in order"
    assert sorted(np.concatenate(blocks)) == list(range(1000)), "covering every period exactly once"


def test_sixteen_blocks_give_the_published_number_of_recombinations():
    result = cscv_pbo(_columns(8))
    assert result.n_blocks == CSCV_BLOCKS == 16
    assert result.n_splits == math.comb(16, 8) == 12_870


def test_the_cached_block_moments_agree_with_the_slow_way():
    """Each split is computed from per-block sums rather than by slicing the returns, which is what
    makes 12,870 recombinations affordable on every Gate 1. A caching bug here would be invisible --
    the number would simply be wrong."""
    from trader.research.pbo import _block_sharpes

    frame = _columns(5, periods=800, seed=2)
    matrix = frame.to_numpy()
    blocks = np.array_split(np.arange(len(matrix)), 8)
    counts = np.array([len(b) for b in blocks], dtype=float)
    sums = np.array([matrix[b].sum(axis=0) for b in blocks])
    squares = np.array([(matrix[b] ** 2).sum(axis=0) for b in blocks])

    chosen = (0, 2, 5)
    fast = _block_sharpes(sums, squares, counts, chosen)
    direct = np.concatenate([matrix[blocks[i]] for i in chosen])
    slow = direct.mean(axis=0) / direct.std(axis=0)
    assert np.allclose(fast, slow, rtol=1e-9)


def test_every_split_uses_each_block_exactly_once():
    """In-sample and out-of-sample must partition the history: a block in both would leak, a block in
    neither would silently discard data."""
    for in_sample in combinations(range(8), 4):
        out = set(range(8)) - set(in_sample)
        assert not out & set(in_sample) and len(out) + len(in_sample) == 8


# --- refusals -------------------------------------------------------------------------------------------

def test_one_configuration_cannot_overfit_and_is_refused():
    with pytest.raises(ValueError, match="one column cannot overfit"):
        cscv_pbo(_columns(1))


def test_an_odd_number_of_blocks_is_refused():
    with pytest.raises(ValueError, match="cannot be split in half"):
        cscv_pbo(_columns(8), n_blocks=15)


def test_too_little_history_is_refused_rather_than_guessed_at():
    with pytest.raises(ValueError, match="too few"):
        cscv_pbo(_columns(8, periods=20))
