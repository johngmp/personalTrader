"""A strategy that can see the future, and which defences notice.

The order of these tests is the argument, so read them in order.

The 2026 work on automated trading research found that a deliberately planted look-ahead oracle scored
a Deflated Sharpe of 1.00. That is the single most important negative result for a system like this one,
because this project's headline defence IS the deflated Sharpe, and a reader who did not know that
result would reasonably assume it covers leakage. It does not, it cannot, and the tests below reproduce
that failure here rather than taking it on trust.

`LookAheadOracle` lives in this file deliberately. It is never imported into `trader/strategies/` and
never appears in `REGISTRY`, so no sleeve can select it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research.backtest import run_backtest
from trader.research.deflated import DSR_THRESHOLD, deflated_sharpe
from trader.research.leakage import truncation_check
from trader.strategies import REGISTRY
from trader.strategies.base import Holding, Strategy, Target

PEEK = 5
NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)
LOOSE = SleeveLimits(require_stop=False)


class LookAheadOracle(Strategy):
    """Buys whenever the next five days are going to be up. Not reachable from any sleeve."""

    name = "ORACLE"
    title = "Look-ahead oracle (test fixture)"
    param_grid = {}
    default_params: dict = {}
    min_trades = 0

    @property
    def warmup_bars(self) -> int:
        return 1

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            close = bars[symbol]["close"]
            self.indicators[symbol] = pd.DataFrame({
                "close": close,
                # The crime, in one line: shift(-PEEK) pulls a price from the future back to today.
                "future_return": close.shift(-PEEK) / close - 1,
            })

    def on_bar(self, date, holdings: dict[str, Holding]) -> dict[str, Target]:
        out: dict[str, Target] = {}
        for symbol in self.symbols:
            row = self.row(symbol, date)
            if row is None or row["future_return"] != row["future_return"]:
                continue
            out[symbol] = (Target(weight=1.0, reason="the future is up")
                           if row["future_return"] > 0 else Target(weight=0.0, reason="the future is down"))
        return out


class Honest(LookAheadOracle):
    """The same strategy with the peek removed, as a control."""

    name = "HONEST"

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            close = bars[symbol]["close"]
            self.indicators[symbol] = pd.DataFrame({
                "close": close,
                "future_return": close.pct_change(PEEK),      # the PAST five days, not the next five
            })


class FullSampleZScore(LookAheadOracle):
    """A subtler leak, and a far more common one in real research: normalising by whole-sample statistics.

    Nothing here is shifted backwards, so it looks innocent. But `close.mean()` is the mean of every day
    including the ones that have not happened yet, so the value at t quietly encodes the future's level.
    It matters for this file because it fails DIFFERENTLY from the oracle: the oracle's peek evaluates to
    NaN once the future is deleted, while this one stays a perfectly ordinary number that simply moves.
    Without it the numeric comparison in `_differs` was never exercised, and a test that claimed to guard
    it passed with the comparison deleted.
    """

    name = "ZLEAK"

    def prepare(self, bars: dict[str, pd.DataFrame]) -> None:
        for symbol in self.symbols:
            close = bars[symbol]["close"]
            self.indicators[symbol] = pd.DataFrame({
                "close": close,
                "z": (close - close.mean()) / close.std(),
                "future_return": close.pct_change(PEEK),        # honest, so `z` is the only offender
            })


@pytest.fixture
def bars() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(11)
    close = 100 * np.cumprod(1 + rng.normal(0.0002, 0.012, 1500))
    idx = pd.bdate_range("2015-01-01", periods=1500)
    return {"SPY": pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                               "close": close, "volume": 1e9}, index=idx)}


def _sharpe(equity: pd.Series) -> float:
    r = equity.pct_change().dropna()
    return float(r.mean() / r.std() * np.sqrt(252))


# --- 1. the oracle has to actually work, or everything below is vacuous ------------------------------

def test_the_planted_oracle_earns_an_impossible_sharpe(bars):
    """Without this, every later assertion could pass because the oracle was broken.

    It also establishes something about the backtester worth knowing: it faithfully passes look-ahead
    through. It is not secretly protecting us, which is why the check has to exist.
    """
    result = run_backtest(LookAheadOracle(params={}, symbols=["SPY"]), bars, 100_000, LOOSE, {"SPY": NO_COST})
    assert _sharpe(result.equity) > 3.0, "the oracle is not peeking, so this file proves nothing"


def test_the_honest_control_does_not(bars):
    result = run_backtest(Honest(params={}, symbols=["SPY"]), bars, 100_000, LOOSE, {"SPY": NO_COST})
    assert _sharpe(result.equity) < 1.5


# --- 2. the defence we rely on cannot catch it ------------------------------------------------------

def test_deflation_alone_does_not_catch_the_planted_oracle(bars):
    """Asserting the FAILURE of a defence, which is unusual in a test suite and the point of this one.

    If someone later reads "deflated Sharpe 99.3% confident" and concludes the result is sound, this is
    the test that says what that number does and does not mean. The deflated Sharpe asks whether a
    result is too good for the amount of searching. A strategy that can see tomorrow is not lucky; it is
    right every time, and no statistic about luck will ever notice.
    """
    result = run_backtest(LookAheadOracle(params={}, symbols=["SPY"]), bars, 100_000, LOOSE, {"SPY": NO_COST})
    returns = result.equity.pct_change().dropna()
    # A punishing setting: fifty thousand trials and a wide spread between them.
    spread = list(np.linspace(0.1, 1.2, 40))
    dsr = deflated_sharpe(returns, n_trials=50_000, trial_sharpes=spread, periods_per_year=252)
    assert dsr >= DSR_THRESHOLD, "if deflation ever does catch this, say so here -- do not delete it"


def test_the_overfitting_probability_does_not_catch_it_either(bars):
    """The same point for the other statistic, so neither is mistaken for a leakage check.

    PBO asks whether picking a winner in-sample generalises out-of-sample. A strategy that can read
    tomorrow wins in BOTH halves of every recombination, so it ranks top every time and its PBO is
    excellent. Being consistently right is exactly what cheating looks like to a method that measures
    consistency.
    """
    from trader.research.pbo import cscv_pbo

    rng = np.random.default_rng(4)
    oracle = run_backtest(LookAheadOracle(params={}, symbols=["SPY"]), bars, 100_000, LOOSE,
                          {"SPY": NO_COST}).equity.pct_change()
    honest = pd.DataFrame({f"h{i}": rng.normal(0, 0.01, len(oracle)) for i in range(17)},
                          index=oracle.index)
    honest["oracle"] = oracle.to_numpy()
    result = cscv_pbo(honest.dropna())
    assert result.pbo < 0.2 and result.passed, \
        "if PBO ever does catch a look-ahead oracle, say so here -- do not delete this"


# --- 3. the structural check does ---------------------------------------------------------------------

def test_the_truncation_check_catches_the_planted_oracle(bars):
    result = truncation_check(LookAheadOracle(params={}, symbols=["SPY"]), bars)
    assert not result.clean
    assert all(o.column == "future_return" for o in result.offenders), "it should name the guilty column"
    assert "future_return" in result.detail and "with the future" in result.detail


def test_the_truncation_check_catches_a_leak_that_is_still_a_perfectly_ordinary_number(bars):
    """The oracle's peek becomes NaN once the future is gone, so it is caught by the NaN branch alone.
    This one does not: every value stays finite and merely moves, which is the case the numeric
    comparison exists for."""
    result = truncation_check(FullSampleZScore(params={}, symbols=["SPY"]), bars)
    assert not result.clean
    offenders = {o.column for o in result.offenders}
    assert offenders == {"z"}, f"expected only the whole-sample z-score, got {offenders}"
    worst = max(result.offenders, key=lambda o: abs(o.with_future - o.without_future))
    assert np.isfinite(worst.with_future) and np.isfinite(worst.without_future), \
        "this leak must be caught as a moved number, not as a NaN"
    assert abs(worst.with_future - worst.without_future) > 1e-6


def test_the_truncation_check_clears_the_honest_control(bars):
    """A check that fails everything is not a check. The control differs from the oracle by the sign of
    one shift, so this is the narrowest possible test of its discrimination."""
    assert truncation_check(Honest(params={}, symbols=["SPY"]), bars).clean


@pytest.mark.parametrize("code", sorted(REGISTRY))
def test_every_registered_strategy_is_clean(code, settings):
    """The regression net. A future strategy that peeks fails at `uv run pytest`, not in production."""
    from trader.data.history import load_many
    from trader.research.evaluate import strategy_setup

    cls, symbols, _, _ = strategy_setup(code, settings)
    result = truncation_check(cls(params=dict(cls.default_params), symbols=symbols), load_many(symbols))
    assert result.clean, f"{code}: {result.detail}"
    assert result.dates_checked > 0, "a check that sampled no dates has not checked anything"


# --- 4. and Gate 1 actually uses it --------------------------------------------------------------------

def test_gate1_fails_a_strategy_whose_indicators_read_the_future(bars, settings, monkeypatch):
    """End to end. The slot this occupies used to be `GateCheck("Realistic backtest", True, ...)`, so
    before this change Gate 1 would have handed a look-ahead oracle a green tick and passed it."""
    from trader.research import evaluate as ev

    monkeypatch.setitem(ev.REGISTRY, "ORACLE", LookAheadOracle)
    monkeypatch.setitem(settings.sleeves, "ORACLE", settings.sleeves["S1"].model_copy(
        update={"id": "ORACLE", "strategy": "ORACLE", "symbols": ["SPY"], "params": {}}))
    monkeypatch.setitem(ev.BENCHMARKS, "ORACLE", [])

    result = ev.evaluate("ORACLE", settings, bars)
    look_ahead = [c for c in result.checks if c.name == "No look-ahead in the indicators"]
    assert look_ahead and not look_ahead[0].passed, "Gate 1 is not running the leakage check"
    assert not result.passed, "a strategy that reads the future must not pass Gate 1"
