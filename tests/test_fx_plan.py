"""The per-pair trade plan on the Forex page.

It answers two questions a beginner actually asks -- what did it pay, and when will it sell -- and the
honest answer to the second is counter-intuitive, so these tests pin it. The holding branch especially:
the FX sleeve is usually flat, so that half of the code never renders on the live page and would rot
unnoticed.
"""

from __future__ import annotations

import re

from trader.engine.dashboard import _distance, _units, fx_plan

COMMON = {"fast_n": 50, "slow_n": 150, "atr_mult": 3.5, "risk_pct": 1.0, "capital": 10_000.0}


def _text(html_: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_)).strip()


def holding_row(**over):
    row = {"symbol": "AUD/USD", "price": 0.70447, "qty": 3439.0, "entry_price": 0.71230,
           "entry_date": "2026-09-21", "stop": 0.69681, "initial_stop": 0.69681,
           "fast": 0.70912, "slow": 0.70687, "atr": 0.00494, **COMMON}
    row.update(over)
    return row


def flat_row(**over):
    row = {"symbol": "EUR/USD", "price": 1.12575, "qty": 0.0, "stop": None,
           "fast": 1.15307, "slow": 1.15603, "atr": 0.00582, **COMMON}
    row.update(over)
    return row


def test_a_held_position_states_what_it_paid_and_what_would_make_it_sell():
    out = _text(fx_plan(holding_row()))
    assert "0.71230" in out, "the entry price is the first thing asked for"
    assert "2026-09-21" in out, "when it bought"
    assert "3,439 units" in out
    assert "0.69681" in out, "the stop is the sell level"
    # 0.70447 - 0.69681 = 0.00766 -> 77 pips, and 0.00766 * 3439 = $26.34 still at risk
    assert "77 pips away" in out, out
    assert "$26" in out, out


def test_it_says_plainly_that_there_is_no_target_price():
    """The actual answer to "when is it planning to sell". A trend strategy has no planned sell level,
    and a panel that invented one would describe a different strategy from the one running."""
    out = _text(fx_plan(holding_row()))
    assert "no target price" in out.lower()
    assert "trend ends" in out.lower(), "the second exit must be stated too"
    assert "only ever moves up" in out.lower(), "a trailing stop that could fall would be a worse trade"


def test_a_stop_that_has_already_moved_up_says_so():
    moved = _text(fx_plan(holding_row(stop=0.70100, initial_stop=0.69681)))
    assert "moved up from 0.69681" in moved, moved
    fresh = _text(fx_plan(holding_row(stop=0.69681, initial_stop=0.69681)))
    assert "moved up from" not in fresh, "an untouched stop must not claim to have moved"


def test_a_flat_pair_shows_both_entry_conditions_and_the_size_it_would_take():
    out = _text(fx_plan(flat_row()))
    assert "1.15603" in out, "the 150-day average is the level being waited for"
    assert "not yet" in out
    # risk $100 / (3.5 * 0.00582) = 4,909.18 units; stop 1.12575 - 0.02037 = 1.10538
    assert "4,909 units" in out, out
    assert "1.10538" in out, out
    assert "204 pips below" in out, out
    assert "$100.00" in out, "the point of the size is that the stop costs exactly the risk budget"


def test_the_pill_only_says_conditions_met_when_both_actually_are():
    assert "waiting" in fx_plan(flat_row())
    both = flat_row(price=1.20000, fast=1.18000, slow=1.15603)
    assert "conditions met" in fx_plan(both)
    # One of two is not enough: price is above the slow average but the fast one is not.
    half = flat_row(price=1.20000, fast=1.14000, slow=1.15603)
    assert "waiting" in fx_plan(half), "a single condition must not read as a buy signal"


def test_gold_is_sized_and_measured_in_its_own_units():
    """A $100 risk against a $347 stop buys 0.29 ounces. Rounded to whole units that renders as
    "0 units", which reads as "it cannot trade this"; and gold is quoted in dollars, not pips."""
    out = _text(fx_plan(flat_row(symbol="XAU/USD", price=4162.30, fast=4366.27,
                                 slow=4477.60, atr=99.2684)))
    assert "0.288 units" in out, out
    assert "0 units" not in out
    assert "$347.44 below" in out, out
    assert "pips" not in out, "nobody trading gold thinks in pips"


def test_units_and_distance_pick_the_right_precision():
    assert _units(0.2878) == "0.288" and _units(5789.3) == "5,789" and _units(4.2) == "4.20"
    assert _distance(0.02037, 1.12575) == "204 pips"      # a major, quoted to 5 places
    assert _distance(1.75, 147.2) == "175 pips"           # a JPY pair, quoted to 3
    assert _distance(347.44, 4162.30) == "$347.44"        # gold, quoted to 2


def test_a_pair_with_no_data_says_so_rather_than_rendering_blanks():
    assert "no data" in fx_plan({"symbol": "EUR/USD", "price": None, "qty": 0.0})
    assert "no data" in fx_plan({"symbol": "EUR/USD", "price": 1.1, "qty": 0.0, "slow": None})
