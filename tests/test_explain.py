"""Plain English on the dashboard.

A number you cannot interpret is worse than no number: it looks like information while telling you
nothing. These check the headline says the right thing, and refuses to be encouraging too early.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.engine.explain import GLOSSARY, headline

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def state(**over) -> dict:
    base = dict(
        last_run=(NOW - timedelta(minutes=5)).isoformat(), kill_switch=False, kill_reason="",
        total_pnl=723.0, start_equity=100_000.0,
        sleeves=[{"id": "B0", "benchmark": True, "broker": "alpaca", "return": 0.044},
                 {"id": "S1", "benchmark": False, "broker": "alpaca", "return": 0.028}],
        kpis={"closed": 6}, warnings=[], blocked=[],
    )
    base.update(over)
    return base


def tones(d) -> list[str]:
    return [t for t, _ in headline(d, NOW)]


def says(d, text) -> bool:
    return any(text.lower() in line.lower() for _, line in headline(d, NOW))


# --- is it alive -----------------------------------------------------------------------------------

def test_a_healthy_engine_says_so_first():
    assert tones(state())[0] == "good"
    assert says(state(), "Everything is running")


def test_a_halted_engine_leads_with_that():
    d = state(kill_switch=True, kill_reason="halted from the menu bar")
    assert tones(d)[0] == "bad"
    assert says(d, "halted from the menu bar")
    assert says(d, "stop orders stay at the broker")   # reassurance, because it matters


def test_a_stale_engine_is_flagged_without_implying_positions_are_unprotected():
    d = state(last_run=(NOW - timedelta(hours=8)).isoformat())
    assert tones(d)[0] == "warn"
    assert says(d, "8.0 hours ago") and says(d, "stop orders resting at the broker")


def test_a_missing_last_run_does_not_crash():
    assert tones(state(last_run=None))[0] == "warn"


def test_an_unparseable_timestamp_does_not_crash():
    assert tones(state(last_run="not a date"))[0] == "warn"


# --- the benchmark, which is the point ---------------------------------------------------------------

def test_losing_to_buy_and_hold_is_stated_plainly():
    assert says(state(), "No strategy is beating the benchmark")
    assert "warn" in tones(state())


def test_beating_buy_and_hold_is_stated_too():
    d = state(sleeves=[{"id": "B0", "benchmark": True, "broker": "alpaca", "return": 0.01},
                       {"id": "S1", "benchmark": False, "broker": "alpaca", "return": 0.05}])
    assert says(d, "ahead of simply buying and holding")


def test_shadow_sleeves_are_not_counted_as_the_best_strategy():
    """A shadow sleeve trades nothing, so claiming it is "doing best" would be misleading."""
    d = state(sleeves=[{"id": "B0", "benchmark": True, "broker": "alpaca", "return": 0.04},
                       {"id": "S1", "benchmark": False, "broker": "alpaca", "return": 0.02},
                       {"id": "S9", "benchmark": False, "broker": "shadow_fx", "return": 0.99}])
    assert says(d, "The best, S1")
    assert not says(d, "S9")


# --- refusing to be encouraging too early -------------------------------------------------------------

def test_too_few_trades_is_said_out_loud():
    assert says(state(), "none of the numbers above mean anything yet")


def test_the_warning_goes_away_once_there_is_real_evidence():
    assert not says(state(kpis={"closed": 120}), "mean anything yet")


def test_a_good_fortnight_is_not_celebrated():
    """Early profit must not read as success; that teaches the wrong habit."""
    d = state(total_pnl=9_000.0)
    assert says(d, "pretend money")
    assert says(d, "mean anything yet")


def test_blocked_orders_are_framed_as_the_system_working():
    assert says(state(blocked=[{}, {}]), "That is it working, not failing")


# --- the glossary -------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Closed trades", "Win rate", "Average R", "Profit factor", "Fees paid",
    "Realised", "Unrealised", "Daily loss limit used", "Drawdown limit used",
])
def test_every_jargon_label_on_the_page_is_explained(label):
    assert label in GLOSSARY
    meaning, _good = GLOSSARY[label]
    assert len(meaning) > 20, "an explanation has to actually explain"


def test_the_win_rate_explanation_prevents_the_obvious_wrong_conclusion():
    """40% sounds like failure unless you are told this kind of strategy is meant to look like that."""
    _meaning, good = GLOSSARY["Win rate"]
    assert "normal" in good.lower()
