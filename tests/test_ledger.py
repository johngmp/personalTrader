"""The trial ledger: can it be quietly edited, and does it see everything?

Every confidence this project reports is deflated by how hard we looked. These tests are what stands
between that and a number built on a count someone tidied up.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from trader.config import CostModel, SleeveLimits
from trader.research import ledger
from trader.research.backtest import run_backtest
from trader.research.ledger import (record_trial, source_fingerprint, trial_count, trial_id,
                                    trial_sharpes, trials_for, verify_chain)
from trader.strategies.buy_and_hold import BuyAndHold

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)


def _seen(strategy="S1", params=None, symbols=("X",), costs=None, span=("2000-01-01", "2020-01-01"),
          source="src", sharpe=None):
    return record_trial(strategy=strategy, params=params if params is not None else {"a": 1},
                        symbols=list(symbols), costs=costs if costs is not None else {},
                        span=span, source=source, sharpe=sharpe)


def _bars(n=120, start=100.0):
    # Noisy, not a straight ramp. A perfectly linear series has constant daily returns, so its standard
    # deviation is zero and the Sharpe is undefined -- which would make a test about recording Sharpes
    # pass for the wrong reason.
    import numpy as np
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2020-01-01", periods=n)
    close = list(start * np.cumprod(1 + rng.normal(0.001, 0.01, n)))
    return pd.DataFrame({"open": close, "high": [c * 1.01 for c in close],
                         "low": [c * 0.99 for c in close], "close": close, "volume": 1.0}, index=idx)


# --- what counts as the same look ------------------------------------------------------------------

def test_the_same_test_run_twice_is_one_trial_but_both_runs_are_kept():
    """The ledger is a record of what was done; only the counting deduplicates.

    "This was run six times" is itself evidence -- it is how rows 8, 9 and 10 of the old research log
    told us that S4's out-of-sample window was read three times in three minutes."""
    first, second = _seen(), _seen()
    assert first == second
    assert trial_count("S1") == 1
    assert len(ledger._rows()) == 2


def test_changing_the_cost_model_is_a_new_look_at_the_same_data():
    """The defect this closes. Three S4 runs inside three minutes, same grid and same symbols, with the
    benchmark moving 0.0 -> 10.521 -> -0.088 as a bug was found, went PASS -> FAIL -> FAIL -- and the old
    counter stayed at 36 throughout. Changing what you charge and looking again IS looking again."""
    _seen(costs={"X": NO_COST})
    assert trial_count("S1") == 1
    _seen(costs={"X": NO_COST.model_copy(update={"slippage_bps": 3.0})})
    assert trial_count("S1") == 2


def test_changing_the_data_span_is_a_new_look():
    _seen(span=("2000-01-01", "2020-01-01"))
    _seen(span=("2000-01-01", "2026-01-01"))
    assert trial_count("S1") == 2


def test_changing_the_strategy_itself_is_a_new_look():
    """Re-measuring after editing the rules is a fresh search, not a repeat of the old one."""
    _seen(source="before")
    _seen(source="after")
    assert trial_count("S1") == 2


def test_one_strategys_searching_is_not_anothers():
    _seen(strategy="S1")
    _seen(strategy="S8")
    assert trial_count("S1") == 1 and trial_count("S8") == 1


# --- the source fingerprint ------------------------------------------------------------------------

def test_rewording_a_comment_or_docstring_is_not_a_new_experiment(tmp_path):
    """This project rewrites prose constantly. If that counted as searching, the bar would climb for
    reasons that have nothing to do with searching, and nobody would believe it.

    Written to real files and imported, because that is the only way `source_fingerprint` can be
    exercised as it is actually used -- an in-test reimplementation of the stripping would pass whatever
    the real function did, which is exactly the kind of test this project keeps catching.
    """
    import importlib.util
    import sys as _sys

    def fingerprint(body: str, name: str) -> str:
        path = tmp_path / f"{name}.py"
        path.write_text(body)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        _sys.modules[name] = module
        spec.loader.exec_module(module)
        return source_fingerprint(module.S)

    plain = "class S:\n    def f(self):\n        return 1 + 2\n"
    reworded = ('class S:\n    """A much better explanation of the same thing."""\n'
                "    def f(self):\n        # and a comment nobody had written before\n"
                "        return 1 + 2\n")
    changed = "class S:\n    def f(self):\n        return 1 - 2\n"

    assert fingerprint(plain, "m_plain") == fingerprint(reworded, "m_reworded"), "prose is not logic"
    assert fingerprint(plain, "m_changed") != fingerprint(changed, "m_changed2"), "one operator is"


def test_a_strategy_with_unreadable_source_still_records_a_trial():
    """Degrading to the old behaviour beats refusing to run a backtest."""
    assert source_fingerprint(type("Dynamic", (), {})) == "unknown"


# --- the chain -------------------------------------------------------------------------------------

def test_editing_a_row_is_detected():
    _seen(params={"a": 1}), _seen(params={"a": 2}), _seen(params={"a": 3})
    assert verify_chain()[0]
    rows = [json.loads(x) for x in ledger.TRIAL_LEDGER.read_text().splitlines()]
    rows[1]["sharpe"] = 9.99
    ledger.TRIAL_LEDGER.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    ok, why = verify_chain()
    assert not ok and "row 2" in why and "edited" in why


def test_deleting_a_row_is_detected():
    """The tidying always removes trials, never adds them."""
    _seen(params={"a": 1}), _seen(params={"a": 2}), _seen(params={"a": 3})
    rows = ledger.TRIAL_LEDGER.read_text().splitlines()
    ledger.TRIAL_LEDGER.write_text(rows[0] + "\n" + rows[2] + "\n")
    ok, why = verify_chain()
    assert not ok and "chain is broken" in why


def test_reordering_two_rows_is_detected():
    _seen(params={"a": 1}), _seen(params={"a": 2}), _seen(params={"a": 3})
    rows = ledger.TRIAL_LEDGER.read_text().splitlines()
    ledger.TRIAL_LEDGER.write_text("\n".join([rows[0], rows[2], rows[1]]) + "\n")
    assert not verify_chain()[0]


def test_appending_never_changes_an_earlier_row():
    _seen(params={"a": 1})
    first = ledger.TRIAL_LEDGER.read_text()
    _seen(params={"a": 2})
    assert ledger.TRIAL_LEDGER.read_text().startswith(first)


def test_gate1_refuses_to_score_against_an_altered_ledger(settings):
    """The whole point of the chain. A verdict is a claim about how hard we looked; if the record of
    that has been edited, there is no honest verdict to give."""
    from trader.research.evaluate import evaluate

    _seen(), _seen(params={"a": 2})
    rows = [json.loads(x) for x in ledger.TRIAL_LEDGER.read_text().splitlines()]
    rows[0]["strategy"] = "S8"
    ledger.TRIAL_LEDGER.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    with pytest.raises(RuntimeError, match="trial ledger has been altered"):
        evaluate("S1", settings, {})


# --- completeness by construction --------------------------------------------------------------------

def test_a_backtest_run_outside_gate1_still_records_a_trial():
    """The defect this closes: `log_result` was reachable only through `trader gate1`, so all the
    intraday research, the earnings study, volatility targeting and every `trader backtest` run searched
    for free -- about 175 real trials against 216 counted. Recording inside `run_backtest` makes an
    unrecorded trial unreachable rather than merely discouraged."""
    bars = {"X": _bars()}
    run_backtest(BuyAndHold(params={}, symbols=["X"]), bars, 10_000, SleeveLimits(), {"X": NO_COST})
    assert trial_count("B0") == 1, "a bare run_backtest call must leave a trace"


def test_every_backtest_records_the_sharpe_the_luck_bar_is_measured_from():
    # require_stop defaults to True and BuyAndHold sets no stop, so the default limits would refuse
    # every entry and the backtest would record a flat curve -- passing this test for the wrong reason.
    result = run_backtest(BuyAndHold(params={}, symbols=["X"]), {"X": _bars()}, 10_000,
                          SleeveLimits(require_stop=False), {"X": NO_COST})
    assert result.n_orders > 0, "nothing was bought, so there is no Sharpe to record"
    assert trial_sharpes("B0") and trial_sharpes("B0")[0] != 0.0


def test_a_flat_backtest_records_a_trial_with_no_sharpe_rather_than_a_fake_one():
    flat = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
                        index=pd.bdate_range("2020-01-01", periods=30))
    run_backtest(BuyAndHold(params={}, symbols=["X"]), {"X": flat}, 10_000, SleeveLimits(),
                 {"X": NO_COST})
    assert trial_count("B0") == 1
    assert trial_sharpes("B0") == [], "no return variation means no Sharpe, not a zero"


# --- what the deflated Sharpe is fed ------------------------------------------------------------------

def test_the_luck_bar_is_measured_from_the_ledger_once_it_has_enough_history():
    """Not from the current run's grid. Eighteen moving-average pairs over the same prices agree with
    each other almost by construction -- S8's spread across them is 0.076 -- and a small spread means a
    small bar however large N is. That is why the check only ever caught S4."""
    from trader.research.evaluate import MIN_SPREAD_SAMPLE, _spread_population

    grid = [{"is_sharpe": 0.70 + 0.001 * i} for i in range(18)]
    for i in range(MIN_SPREAD_SAMPLE):
        _seen(params={"a": i}, sharpe=1.0 + 0.05 * i)
    population, source = _spread_population("S1", grid)
    assert len(population) == MIN_SPREAD_SAMPLE and "past trials" in source
    assert min(population) >= 1.0, "the current grid's near-identical cells must not dilute it"
    import numpy as np
    assert np.std(population) > 4 * np.std([r["is_sharpe"] for r in grid]), \
        "a real search moves results far more than one grid of near-identical settings does"


def test_a_thin_ledger_falls_back_to_the_grid_and_says_the_result_is_weak():
    from trader.research.evaluate import _spread_population

    grid = [{"is_sharpe": 0.7}, {"is_sharpe": 0.8}]
    _seen(sharpe=0.5)
    population, source = _spread_population("S1", grid)
    assert sorted(population) == [0.5, 0.7, 0.8]
    assert "weak" in source, "the report has to say when the bar means little"


def test_the_counter_sees_every_trial_not_just_gate1_runs():
    _seen(params={"a": 1}), _seen(params={"a": 2})
    assert trial_count("S1") == 2 == len(trials_for("S1"))


def test_a_trial_id_is_stable_across_processes():
    """It is a content hash, not a counter -- so backfilling twice adds nothing the second time."""
    args = ("S1", {"a": 1}, ["X", "Y"], {}, ("2000-01-01", "2020-01-01"), "src")
    assert trial_id(*args) == trial_id("S1", {"a": 1}, ["Y", "X"], {}, ("2000-01-01", "2020-01-01"), "src")


def test_the_chain_head_is_read_from_the_end_but_still_matches_the_whole_file():
    """`last_hash` reads a window off the end instead of parsing everything, because recording is
    O(rows) otherwise and `evaluate` records eighteen trials per run. The shortcut has to agree with
    the slow path on a ledger long enough that the window is not the whole file."""
    from trader.research.ledger import last_hash

    for i in range(60):
        _seen(params={"a": i, "padding": "x" * 200})
    assert ledger.TRIAL_LEDGER.stat().st_size > 8192, "too small to exercise the window"
    assert last_hash() == ledger._rows()[-1]["hash"]
    assert verify_chain()[0]
