"""Probability of backtest overfitting: does picking a setting in-sample work out-of-sample at all?

This asks a different question from the deflated Sharpe, and the difference is worth being clear about
because the project now reports both.

The deflated Sharpe asks: is THIS result better than the best of N random trials would have been? It
judges the winner against a luck bar.

PBO asks: does the PROCEDURE work? Chop the history into blocks, deal half of them into an in-sample
set and half into an out-of-sample set, pick the configuration that looks best in-sample, and see where
it lands out-of-sample. Do that over every balanced deal and count how often the in-sample winner ends
up in the worse half. If that happens most of the time, the search is not finding an edge -- it is
finding whichever cell got lucky in the half you happened to look at, and no amount of care about the
winner's Sharpe fixes a selection method that does not generalise.

Bailey, Borwein, Lopez de Prado and Zhu (2015), combinatorially symmetric cross-validation.

Two honest limits, both of which belong in the report rather than only here:

- **A homogeneous grid makes this uninformative, in either direction.** When every cell is nearly the
  same strategy, which one wins in-sample is close to arbitrary, and the out-of-sample rank of an
  arbitrary pick is close to uniform. The number that comes out describes the grid, not the search. So
  `informative` is reported alongside, and a result on a flat grid says so instead of offering a
  reassuring percentage. The whole point of adding a statistic is to learn something, and a second
  green tick that always passes is worse than no tick at all -- this project has just removed one.
- **It cannot see look-ahead.** A strategy that can read tomorrow wins in-sample AND out-of-sample, so
  its rank is always top and its PBO is zero. `tests/test_leakage_oracle.py` asserts exactly that, for
  the same reason it asserts the deflated Sharpe passes an oracle: so nobody later mistakes either
  number for a leakage check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd

CSCV_BLOCKS = 16
PBO_THRESHOLD = 0.5
# Below this spread of in-sample Sharpes across the grid, which cell "wins" is mostly noise and the
# rank of the winner says nothing about the search. Measured on this project's grids: S8's eighteen
# cells sit at 0.076 and S1's at 0.068, so the shipped grids are near this line and the flag will
# usually fire -- which is the finding, not a defect.
INFORMATIVE_SPREAD = 0.05


@dataclass
class PBOResult:
    pbo: float
    n_splits: int
    n_blocks: int
    n_trials: int
    spread: float                  # standard deviation of the configurations' full-sample Sharpes
    degradation: float             # mean out-of-sample minus mean in-sample Sharpe of the winner
    informative: bool

    @property
    def detail(self) -> str:
        if not self.informative:
            return (f"uninformative: the {self.n_trials} settings differ by only {self.spread:.3f} in "
                    f"Sharpe, so which one wins in-sample is mostly noise and its out-of-sample rank "
                    f"says little either way (PBO reads {self.pbo:.0%})")
        return (f"the in-sample winner lands in the worse half {self.pbo:.0%} of the time over "
                f"{self.n_splits:,} train/test recombinations (need under {PBO_THRESHOLD:.0%}); it "
                f"gives up {abs(self.degradation):.2f} Sharpe out-of-sample")

    @property
    def passed(self) -> bool:
        """An uninformative result is not a failure. It is a statistic declining to answer."""
        return (not self.informative) or self.pbo < PBO_THRESHOLD


def time_blocks(periods: int, n_blocks: int) -> list[np.ndarray]:
    """The history cut into contiguous stretches of time, in order.

    Contiguous, never shuffled rows. Shuffling would destroy the serial dependence that makes a return
    series what it is -- volatility clusters, trends persist, drawdowns are runs -- and would quietly
    turn this into ordinary cross-validation wearing the name of a method designed to avoid exactly
    that. The recombination happens over whole blocks, which keeps each one a real piece of market
    history rather than a bag of unrelated days.
    """
    return np.array_split(np.arange(periods), n_blocks)


def _block_sharpes(sums: np.ndarray, squares: np.ndarray, counts: np.ndarray,
                   which: tuple[int, ...]) -> np.ndarray:
    """Per-configuration Sharpe over a set of blocks, from cached moments.

    Caching the per-block sum and sum-of-squares once turns each of the 12,870 recombinations into a
    handful of additions instead of a pass over the returns. Without it, sixteen blocks would be too
    slow to run on every Gate 1 and the number of blocks would have to be compromised to something the
    literature does not use.
    """
    idx = list(which)
    n = counts[idx].sum()
    total = sums[idx].sum(axis=0)
    total_sq = squares[idx].sum(axis=0)
    mean = total / n
    var = np.maximum(total_sq / n - mean ** 2, 1e-300)
    return mean / np.sqrt(var)


def cscv_pbo(returns: pd.DataFrame, n_blocks: int = CSCV_BLOCKS) -> PBOResult:
    """`returns` is dates x configurations of per-period returns. One column per setting tried."""
    matrix = returns.to_numpy(dtype=float)
    matrix = matrix[~np.isnan(matrix).any(axis=1)]
    periods, n_trials = matrix.shape
    if n_trials < 2:
        raise ValueError("PBO compares configurations against each other; one column cannot overfit")
    if periods < n_blocks * 2:
        raise ValueError(f"{periods} periods is too few to cut into {n_blocks} blocks")
    if n_blocks % 2:
        raise ValueError("an odd number of blocks cannot be split in half")

    blocks = time_blocks(periods, n_blocks)
    counts = np.array([len(b) for b in blocks], dtype=float)
    sums = np.array([matrix[b].sum(axis=0) for b in blocks])
    squares = np.array([(matrix[b] ** 2).sum(axis=0) for b in blocks])

    all_blocks = set(range(n_blocks))
    worse_half, degradations = 0, []
    splits = list(combinations(range(n_blocks), n_blocks // 2))
    for in_sample in splits:
        out_sample = tuple(sorted(all_blocks - set(in_sample)))
        is_sharpe = _block_sharpes(sums, squares, counts, in_sample)
        oos_sharpe = _block_sharpes(sums, squares, counts, out_sample)
        winner = int(np.argmax(is_sharpe))
        # Relative rank in (0, 1): how many configurations the winner beat out-of-sample.
        rank = float((oos_sharpe < oos_sharpe[winner]).sum()) / (n_trials - 1)
        if rank < 0.5:
            worse_half += 1
        degradations.append(oos_sharpe[winner] - is_sharpe[winner])

    full = matrix.mean(axis=0) / np.maximum(matrix.std(axis=0), 1e-300)
    spread = float(np.std(full) * math.sqrt(252))          # annualised, to compare with the reports
    return PBOResult(pbo=worse_half / len(splits), n_splits=len(splits), n_blocks=n_blocks,
                     n_trials=n_trials, spread=spread,
                     degradation=float(np.mean(degradations)),
                     informative=spread >= INFORMATIVE_SPREAD)
