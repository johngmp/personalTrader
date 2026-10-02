"""The menu bar reads the journal and can halt; it must never be able to place an order."""

from __future__ import annotations

import inspect

import pytest

from trader import menubar
from trader.menubar import STALE_AFTER_MIN, TraderBar, _money


@pytest.mark.parametrize(("value", "signed", "expected"), [
    (100596.51, False, "$100,596.51"),
    (596.51, True, "+$596.51"),
    (-42.0, True, "−$42.00"),
    (0.0, True, "+$0.00"),
])
def test_money_formatting(value, signed, expected):
    assert _money(value, signed=signed) == expected


def test_the_menu_bar_cannot_place_an_order():
    """Its whole safety story: it reads, it opens things, it can halt. Nothing reaches a broker."""
    source = inspect.getsource(menubar)
    for forbidden in ("submit_order", "OrderRequest", "Executor", "replace_stop", "cancel_order"):
        assert forbidden not in source, f"{forbidden} must not appear in the menu bar"


def test_it_reads_the_journal_rather_than_a_broker():
    """Keeps working during a network outage, and costs no API calls."""
    source = inspect.getsource(menubar)
    assert "Journal" in source
    for broker in ("AlpacaBroker", "get_quote", "get_account"):
        assert broker not in source


def test_halting_asks_first_but_resuming_does_not():
    """Halting is the destructive direction, so a stray click must not do it."""
    source = inspect.getsource(TraderBar.toggle_kill)
    before_else = source.split("else:")[0]
    assert "release()" in before_else          # resuming: no prompt
    assert "rumps.alert" in source.split("else:")[1]   # halting: prompt


def test_a_stale_engine_is_flagged_within_three_missed_runs():
    assert 15 < STALE_AFTER_MIN <= 60


def test_a_journal_failure_is_caught_and_shown_rather_than_swallowed():
    """A blank or stale number looks exactly like a quiet market, which is the dangerous failure.

    Driving the real refresh needs a live NSApp, so this pins the contract instead: refresh catches
    whatever _snapshot raises, and says so in the title rather than leaving the last good number up.
    """
    source = inspect.getsource(TraderBar.refresh)
    assert "except Exception" in source
    assert "⚠️" in source and "Could not read the journal" in source


def test_an_empty_journal_shows_no_history_rather_than_a_zero_pnl(tmp_path):
    """Journal() creates an empty database rather than raising, so the menu must distinguish
    "nothing has happened yet" from "the account is flat"."""
    bar = TraderBar.__new__(TraderBar)
    bar.settings = type("S", (), {"journal_path": tmp_path / "fresh" / "x.db", "sleeves": {}})()
    snap = bar._snapshot()
    assert snap["rows"] == [] and snap["sleeves"] == [] and snap["last_run"] is None
    assert "No account history yet" in inspect.getsource(TraderBar._render)
