"""Deflated Sharpe Ratio: does a result survive knowing how hard we looked for it?

Gate 1 picks the best of a parameter grid. Test enough variants and a flattering Sharpe arrives by luck
alone -- the expected MAXIMUM of N random trials is positive and grows with N. S1 is at 72 distinct
trials, so "it scored 0.89" means little without knowing that 0.89 beats what luck would have produced.

Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
Overfitting and Non-Normality". Two steps:

1. Work out the Sharpe luck alone would have produced as the best of N trials (`expected_max_sharpe`),
   from how much the trial Sharpes varied.
2. Ask how confident we are the real Sharpe beats that, correcting for the fact that returns are skewed
   and fat-tailed -- both of which flatter a plain Sharpe ratio (`deflated_sharpe`).

The answer is a probability. Above 0.95 means the result is unlikely to be luck.

scipy is not a dependency, so the two normal-distribution functions are implemented here.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329
DSR_THRESHOLD = 0.95


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, accurate to ~1e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf needs 0 < p < 1, got {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """The Sharpe that luck alone produces as the best of `n_trials`, given how much trials varied.

    Both `sharpe_std` and the result are per-period, not annualised. With one trial there is no
    selection, so the bar is 0.
    """
    if n_trials < 2 or sharpe_std <= 0:
        return 0.0
    a = norm_ppf(1 - 1 / n_trials)
    b = norm_ppf(1 - 1 / (n_trials * math.e))
    return float(sharpe_std * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(returns: pd.Series, n_trials: int, trial_sharpes: list[float],
                    periods_per_year: float) -> float:
    """Probability the true Sharpe is above zero, after correcting for selection and non-normality.

    `trial_sharpes` are the annualised Sharpes of every setting tried, used only for their spread.
    Returns a probability in [0, 1]; compare against DSR_THRESHOLD.
    """
    r = pd.Series(returns).dropna()
    n = len(r)
    sd = float(r.std())
    if n < 30 or sd <= 0:
        return 0.0
    observed = float(r.mean()) / sd                      # per-period Sharpe
    spread = float(np.std([s for s in trial_sharpes if np.isfinite(s)] or [0.0]))
    benchmark = expected_max_sharpe(n_trials, spread / math.sqrt(periods_per_year))

    skew = float(r.skew())
    kurtosis = float(r.kurtosis()) + 3.0                 # pandas reports EXCESS kurtosis
    # Negative skew and fat tails both make a plain Sharpe flattering, so they widen the error bar.
    variance = 1 - skew * observed + (kurtosis - 1) / 4 * observed**2
    if variance <= 0:
        return 0.0
    z = (observed - benchmark) * math.sqrt(n - 1) / math.sqrt(variance)
    return float(norm_cdf(z))
