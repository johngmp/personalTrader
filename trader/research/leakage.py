"""Does any indicator know something it could not have known yet?

This replaces a `GateCheck("Realistic backtest", True, ...)` -- a check hardcoded to pass, which asserted
nothing and rendered as a green tick in every report this project has ever produced.

It matters more than it looks, because of the sharpest finding in the 2026 work on automated trading
research: a deliberately planted look-ahead oracle scored a Deflated Sharpe of **1.00**. Deflation
cannot catch leakage. Neither can the probability of backtest overfitting. Both ask "is this result too
good for the amount of searching?", and a strategy that can see tomorrow is not lucky -- it is right,
every time, for a reason no statistic about luck will ever notice. Only a structural test catches it,
and `tests/test_leakage_oracle.py` proves that claim inside this repo rather than asserting it.

The test is mechanical rather than statistical, which is what makes it worth having. For a handful of
sampled dates t, recompute the indicators from bars that STOP at t, and compare the row at t with the
one computed from the whole history. If a value moves, `prepare` read the future, and the result names
the symbol, the date and the column rather than reporting a suspicion.

Removing the future is the right direction to truncate, and the only safe one. Wilder's ATR here is an
exponentially weighted mean -- a recursive indicator, whose value genuinely does depend on how much
history preceded it. Truncating the START would change it for an honest reason and the check would cry
wolf on every strategy in the registry. Truncating the END cannot, because nothing before t depends on
anything after it unless something is wrong. That asymmetry is the whole design.

What this does NOT catch, and what therefore still belongs in the report's caveats:

- **Survivorship bias.** S8's forty symbols were chosen in 2026 from markets that still exist, and
  backtested to 1995. No statistic in this project fixes that.
- **Retroactive adjustment.** Yahoo back-propagates dividend and split adjustments, so a 1998 close in
  today's file is not the 1998 close anyone saw.
- **Point-in-time universe membership** -- a market that was delisted and quietly is not in the list.

A green tick that implies more than it proves is how the hardcoded one happened in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from trader.strategies.base import Strategy

# Enough dates to catch a leak without re-running `prepare` more than a handful of times. A peek at the
# future is not subtle: it is in every row, so sampling a dozen finds it with certainty.
SAMPLE_DATES = 12
# Rolling means over arrays of different length can disagree in the last bits of a float. Anything a
# look-ahead produces is many orders of magnitude larger than this.
TOLERANCE = 1e-9


@dataclass
class Offender:
    symbol: str
    date: pd.Timestamp
    column: str
    with_future: float
    without_future: float

    def __str__(self) -> str:
        return (f"{self.column} on {self.symbol} at {self.date.date()}: {self.with_future:.6g} with the "
                f"future, {self.without_future:.6g} without it")


@dataclass
class LeakageResult:
    clean: bool
    dates_checked: int
    offenders: list[Offender] = field(default_factory=list)

    @property
    def detail(self) -> str:
        if self.clean:
            return (f"every indicator value at {self.dates_checked} sampled dates is unchanged when the "
                    f"future is deleted")
        return f"{len(self.offenders)} value(s) changed, e.g. {self.offenders[0]}"


def _differs(a: float, b: float) -> bool:
    a_nan, b_nan = (a != a), (b != b)        # NaN is the honest answer during warm-up, and equals NaN
    if a_nan or b_nan:
        return a_nan != b_nan
    return not np.isclose(a, b, rtol=TOLERANCE, atol=TOLERANCE)


def truncation_check(strategy: Strategy, bars: dict[str, pd.DataFrame], *,
                     n_dates: int = SAMPLE_DATES, seed: int = 0) -> LeakageResult:
    """Recompute the indicators with the future deleted and see whether anything moves."""
    full = type(strategy)(params=dict(strategy.params), symbols=list(strategy.symbols))
    full.setup(bars)
    if not full.indicators:
        return LeakageResult(clean=True, dates_checked=0)

    # Only dates every symbol has, and only after the longest warm-up, so NaN-vs-number differences are
    # about the future rather than about one market having started later than another.
    common = None
    for symbol in strategy.symbols:
        idx = bars[symbol].index
        common = idx if common is None else common.intersection(idx)
    common = common[strategy.warmup_bars:] if common is not None else pd.DatetimeIndex([])
    if len(common) < 2:
        return LeakageResult(clean=True, dates_checked=0)

    rng = np.random.default_rng(seed)
    # Never the last date: with nothing after it there is no future to delete, so it could not fail.
    picks = sorted(rng.choice(len(common) - 1, size=min(n_dates, len(common) - 1), replace=False))
    offenders: list[Offender] = []

    for i in picks:
        t = common[i]
        past_only = type(strategy)(params=dict(strategy.params), symbols=list(strategy.symbols))
        past_only.setup({s: df.loc[:t] for s, df in bars.items()})
        for symbol, truncated in past_only.indicators.items():
            whole = full.indicators.get(symbol)
            if whole is None or t not in truncated.index or t not in whole.index:
                continue
            then, now = truncated.loc[t], whole.loc[t]
            for column in truncated.columns:
                if column in whole.columns and _differs(float(now[column]), float(then[column])):
                    offenders.append(Offender(symbol, t, column, float(now[column]), float(then[column])))

    return LeakageResult(clean=not offenders, dates_checked=len(picks), offenders=offenders)
