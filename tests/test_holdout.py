"""The sealed holdout: can research reach it, and does a read really cost something?

The failure these guard against is not dramatic. It is a window that gets looked at a few more times
than anyone remembers, until it is quietly part of the training set. This project's own log shows the
old out-of-sample window read nineteen times, six for S4 alone, and PLAN.md:418 records the result in
plain words: "that was measured by peeking at out-of-sample and cannot be acted on."
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from trader.research import holdout as ho
from trader.research.holdout import (CONFIRMATION, HOLDOUT_START, HoldoutAlreadyOpened, HoldoutRefused,
                                     candidate_id, reaches_past_the_seal, request_receipt)


@pytest.fixture(autouse=True)
def receipts(tmp_path, monkeypatch):
    """Nothing in this file may touch the real receipts: they are one-shot and cannot be un-spent."""
    monkeypatch.setattr(ho, "RECEIPTS_DIR", tmp_path / "receipts")
    return tmp_path / "receipts"


@pytest.fixture
def gate1_log(tmp_path, monkeypatch, settings):
    import trader.research.gate2 as g2

    path = tmp_path / "research_log.jsonl"
    path.write_text(json.dumps({
        "ts": "2026-10-03T06:29:04+00:00", "strategy": "S1",
        "symbols": sorted(settings.sleeves["S1"].symbols), "verdict": "PASS", "oos_sharpe": 0.85,
        "chosen_params": dict(settings.sleeves["S1"].params),
    }) + "\n")
    monkeypatch.setattr(g2, "RESEARCH_LOG", path)
    return path


def _bars(start, periods):
    idx = pd.bdate_range(start, periods=periods)
    close = [100.0 + i * 0.1 for i in range(periods)]
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
                        index=idx)


# --- the fence itself ------------------------------------------------------------------------------

def test_the_seal_is_pinned_in_code_and_not_settable_from_config():
    """A seal date that can be set from a YAML file is a tunable, and the one thing this must not be is
    adjustable by whoever is disappointed by the result."""
    import subprocess

    assert HOLDOUT_START == pd.Timestamp("2022-01-01")
    found = subprocess.run(["grep", "-rn", "HOLDOUT_START", "--include=*.yaml", "--include=*.yml",
                            "--include=*.env", "config/", "."], capture_output=True, text=True)
    assert not found.stdout.strip(), f"the seal is reachable from config: {found.stdout}"


def test_research_data_stops_before_the_seal():
    everything = {"SPY": _bars("2019-01-01", 2000)}      # runs well past 2022
    assert reaches_past_the_seal(everything) == ["SPY"]
    fenced = {s: df.loc[df.index < HOLDOUT_START] for s, df in everything.items()}
    assert reaches_past_the_seal(fenced) == []
    assert fenced["SPY"].index.max() < HOLDOUT_START


def test_gate1_refuses_bars_that_reach_into_the_holdout(settings):
    """`research_bars` truncates at the door, so this only fires on a bars dict built by hand -- an
    ad-hoc script, a notebook, a well-meant shortcut. Those are the paths that produced the nineteen
    reads, so the refusal lives where the verdict is made and not only where data is loaded."""
    from trader.research.evaluate import evaluate

    with pytest.raises(RuntimeError, match="reach past the holdout seal"):
        evaluate("S1", settings, {"SPY": _bars("2019-01-01", 2000)})


# --- what a receipt costs ----------------------------------------------------------------------------

def test_the_confirmation_must_name_the_strategy_exactly(settings, gate1_log):
    """A --yes flag is something a script emits as readily as a person. Typing a sentence that names
    the strategy is a deliberate act by someone who knows what it spends."""
    for wrong in ("", "yes", "y", "open holdout", CONFIRMATION.format(strategy="S8")):
        with pytest.raises(HoldoutRefused, match="say exactly"):
            request_receipt("S1", settings, confirmation=wrong)


def test_a_strategy_running_settings_gate1_did_not_choose_cannot_spend_its_holdout(settings, gate1_log):
    """S8 trades atr_mult 2.5 against a Gate 1 that chose 4.5. Spending a one-shot test on settings you
    are not running measures nothing you could act on."""
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(
        update={"params": {**settings.sleeves["S1"].params, "atr_mult": 99.0}})
    with pytest.raises(HoldoutRefused, match="Gate 1 chose"):
        request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))


def test_a_strategy_with_no_passing_gate1_cannot_spend_its_holdout(settings, tmp_path, monkeypatch):
    import trader.research.gate2 as g2

    monkeypatch.setattr(g2, "RESEARCH_LOG", tmp_path / "empty.jsonl")
    with pytest.raises(HoldoutRefused, match="no passing Gate 1"):
        request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))


def test_an_altered_trial_ledger_blocks_the_holdout(settings, gate1_log):
    """The holdout verdict is only meaningful alongside an honest count of how hard we looked."""
    from trader.research import ledger

    ledger.TRIAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ledger.TRIAL_LEDGER.write_text('{"ts":"x","strategy":"S1","trial":"t","prev":"nope","hash":"bad"}\n')
    with pytest.raises(HoldoutRefused, match="ledger has been altered"):
        request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))


# --- the mechanism that makes it one-shot --------------------------------------------------------------

def test_a_read_that_crashes_still_spends_the_receipt(settings, gate1_log, monkeypatch):
    """The single most important test here. If a failed read were free, the holdout could be read until
    it gave a good answer, which is not a holdout -- it is a slower way of fitting."""
    receipt = request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))
    assert receipt.used_at is None

    def explode(*_args, **_kwargs):
        raise ValueError("the market data was malformed")

    monkeypatch.setattr("trader.data.history.load_many", explode)
    with pytest.raises(ValueError):
        ho.holdout_bars(["SPY"], receipt)

    assert receipt.used_at is not None, "the receipt must be stamped BEFORE any data is returned"
    assert json.loads(receipt.path.read_text())["used_at"], "and flushed to disk, not just in memory"


def test_the_same_candidate_cannot_be_read_twice(settings, gate1_log):
    request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))
    with pytest.raises(HoldoutAlreadyOpened, match="opened its holdout"):
        request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))


def test_changing_a_parameter_earns_a_new_receipt_and_both_stay_on_the_record(settings, gate1_log):
    """You CAN burn a second receipt by changing a setting. What you cannot do is burn it invisibly:
    `trader holdout --list` shows every one."""
    first = request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))

    new_params = {**settings.sleeves["S1"].params, "atr_mult": 4.5}
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(update={"params": new_params})
    gate1_log.write_text(json.dumps({
        "ts": "2026-10-04T00:00:00+00:00", "strategy": "S1",
        "symbols": sorted(settings.sleeves["S1"].symbols), "verdict": "PASS", "oos_sharpe": 0.85,
        "chosen_params": new_params}) + "\n")

    second = request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))
    assert second.candidate != first.candidate
    assert len(list(ho.RECEIPTS_DIR.glob("*.json"))) == 2, "both reads stay on the record"


def test_a_candidate_id_ignores_the_order_symbols_happen_to_be_listed_in():
    assert candidate_id("S1", {"a": 1}, ["B", "A"]) == candidate_id("S1", {"a": 1}, ["A", "B"])
    assert candidate_id("S1", {"a": 1}, ["A"]) != candidate_id("S1", {"a": 2}, ["A"])


# --- the honest caveat -----------------------------------------------------------------------------------

def test_an_existing_strategy_is_flagged_as_having_already_seen_this_period(settings, gate1_log):
    """Sealing the window today does not unsee it. S1, S4, S8 and S9 were all evaluated repeatedly
    against data from this period, so their first read is weak evidence and has to say so -- otherwise
    a fresh constant gets mistaken for a fresh slice, which is the exact self-deception being guarded.
    """
    receipt = request_receipt("S1", settings, confirmation=CONFIRMATION.format(strategy="S1"))
    assert receipt.pre_existing_exposure is True


def test_a_strategy_that_arrived_after_the_seal_is_not_flagged(settings, tmp_path, monkeypatch):
    import trader.research.gate2 as g2

    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({
        "ts": "2026-10-03T00:00:00+00:00", "strategy": "S1",
        "symbols": sorted(settings.sleeves["S1"].symbols), "verdict": "PASS", "oos_sharpe": 0.85,
        "chosen_params": dict(settings.sleeves["S1"].params)}) + "\n")
    monkeypatch.setattr(g2, "RESEARCH_LOG", log)
    assert ho._has_pre_existing_exposure("S1") is True
    assert ho._has_pre_existing_exposure("BRAND-NEW") is False
