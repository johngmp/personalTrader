"""The per-pair trade plan on the Forex page.

It answers two questions a beginner actually asks -- what did it pay, and when will it sell -- and the
honest answer to the second is counter-intuitive, so these tests pin it. The holding branch especially:
the FX sleeve is usually flat, so that half of the code never renders on the live page and would rot
unnoticed.
"""

from __future__ import annotations

import re

import pytest

from trader.engine.dashboard import _distance, _units, fx_plan


@pytest.fixture(scope="module")
def page() -> str:
    from pathlib import Path

    from trader.config import load_settings
    from trader.engine.brokers_setup import configured_brokers
    from trader.engine.dashboard import dashboard_html
    from trader.engine.live import Engine
    from trader.journal.db import Journal

    settings = load_settings()
    journal = Journal(Path("data/journal.db"))
    engines = [Engine(settings, b, journal, bars) for b, bars in configured_brokers(settings, journal)]
    try:
        return dashboard_html(settings, journal, engines)
    finally:
        journal.close()

COMMON = {"fast_n": 50, "slow_n": 150, "atr_mult": 3.5, "risk_pct": 1.0, "capital": 10_000.0}


def _text(html_: str) -> str:
    """Tags stripped and entities decoded, so assertions match what a reader actually sees."""
    import html as _h

    return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", html_))).strip()


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


# --- the plan follows the pair you click ------------------------------------------------------------

class _Trip:
    """Just the attributes _fx_history reads, so the test does not depend on the journal."""

    symbol = "AUD/USD"

    def __init__(self, is_open=False, **kw):
        self.is_open = is_open
        self.entry_ts, self.entry_price = "2026-09-21T21:06:36+00:00", 0.71230
        self.exit_ts, self.exit_price = "2026-09-23T21:03:53+00:00", 0.70447
        self.buy_qty, self.exit_reason, self.entry_reason = 3439.0, "trend ended", "trend up"
        self.__dict__.update(kw)

    def pnl(self, now=None):
        out = (now if self.is_open else self.exit_price) - self.entry_price
        return out * self.buy_qty


def test_a_closed_trade_is_listed_with_both_prices_and_why_it_ended():
    out = _text(fx_plan(flat_row(symbol="AUD/USD"), [_Trip()], lambda s: 0.70447))
    assert "2026-09-21" in out and "0.71230" in out, "when and at what price it bought"
    assert "2026-09-23" in out and "0.70447" in out, "when and at what price it sold"
    assert "3,439" in out
    assert "trend ended" in out, "why it ended is the whole point of logging it"
    # (0.70447 - 0.71230) * 3439 = -$26.93. The live journal shows -$26.92 because a real Trip also
    # subtracts the cost haircut; this stub is raw arithmetic on purpose.
    assert "−$26.93" in out, out


def test_an_open_trade_is_marked_open_and_valued_at_the_live_rate():
    out = _text(fx_plan(flat_row(symbol="AUD/USD"), [_Trip(is_open=True)], lambda s: 0.72000))
    assert "open" in out
    # (0.72000 - 0.71230) * 3439 = +$26.48, at the live rate rather than a stale exit price
    assert "$26.48" in out, out


def test_a_pair_with_no_trades_says_so_rather_than_an_empty_table():
    out = _text(fx_plan(flat_row(), [], lambda s: 1.1))
    assert "No trades in this pair yet." in out
    assert "<table" not in fx_plan(flat_row(), [], lambda s: 1.1)


def test_each_plan_is_addressable_by_pair_and_starts_hidden():
    """The pair list reveals one; they cannot all be visible at once or the page is four plans long."""
    html_ = fx_plan(flat_row(symbol="EUR/USD"), [], lambda s: 1.1)
    assert 'data-plan="EUR/USD"' in html_
    assert "hidden" in html_.split(">")[0], "plans must start hidden; show() reveals the selected one"


def test_selecting_a_pair_switches_its_plan_and_is_scoped_to_one_panel():
    """Scoped on purpose: an unscoped querySelectorAll would let the stocks list, which has no plans,
    hide the forex one the moment you clicked a stock."""
    from trader.engine import dashboard as dash

    assert "selectPlan" in dash.FX_JS
    assert "if (!opts.plans) return;" in dash.FX_JS, "must no-op for panels without plans"
    assert "plans: '#fx-plans'" in dash.FX_JS, "the forex panel opts in"
    assert dash.FX_JS.count("selectPlan(pair)") >= 1, "show() must call it"


def test_the_gate_1_failure_is_stated_before_the_trade_detail(page):
    """Spelling out exactly what each trade would do and only then mentioning that the rules failed
    their own test gets the order of those two facts backwards."""
    body = page[page.index('data-panel="forex"'):]
    verdict, plans = body.find("fxverdict"), body.find('id="fx-plans"')
    assert verdict != -1 and plans != -1, "both must be on the page"
    assert verdict < plans, "the Gate 1 verdict must come before the per-pair plans"


# --- the stop ladder --------------------------------------------------------------------------------

def test_the_ladder_places_entry_and_now_between_the_stop_and_the_best_rate():
    """The track runs from the level that ends the trade to the best rate since buying, so "how much
    room is left" is a distance you can see instead of a subtraction you have to do."""
    import re

    out = fx_plan(holding_row(price=0.71980, stop=0.70850, entry_price=0.71230,
                              high_since=0.72310, qty=3439.0), [], lambda s: 0.71980)
    # span 0.70850 -> 0.72310 = 0.01460; entry sits 0.00380 in (26.0%), now 0.01130 in (77.4%)
    assert re.search(r'ladder-mark entry" style="left:26\.0%', out), out
    assert re.search(r'ladder-mark now up" style="left:77\.4%', out), out
    assert re.search(r'ladder-fill up" style="width:77\.4%', out), out


def test_the_ladder_names_all_three_of_its_reference_points():
    """A bare tick mark on a track is only meaningful if you are told what it marks."""
    out = _text(fx_plan(holding_row(price=0.71980, stop=0.70850, entry_price=0.71230,
                                    high_since=0.72310), [], lambda s: 0.71980))
    assert "0.70850 · sells here" in out
    assert "bought 0.71230" in out
    assert "best 0.72310" in out


def test_a_losing_trade_colours_the_ladder_down():
    out = fx_plan(holding_row(price=0.70900, entry_price=0.71230, stop=0.70850,
                              high_since=0.71400), [], lambda s: 0.70900)
    assert 'ladder-mark now down' in out and 'ladder-fill down' in out


def test_the_ladder_is_skipped_when_it_would_divide_by_zero():
    """A stop at or above every other level has no span to draw, and 1/0 beats a wrong picture."""
    assert "ladder-track" not in fx_plan(
        holding_row(price=0.70000, entry_price=0.70000, stop=0.70000, high_since=0.70000),
        [], lambda s: 0.70000)


def test_an_open_trade_does_not_claim_a_reason_for_ending():
    """It used to fall back to the ENTRY reason, so an open position read "trend up" under the heading
    "Why it ended" -- stating that a live trade had finished, and why."""
    out = _text(fx_plan(flat_row(symbol="AUD/USD"), [_Trip(is_open=True)], lambda s: 0.72000))
    assert "still open" in out
    assert "trend up" not in out, "the entry reason must not appear under 'why it ended'"


# --- the entry conditions, as rule cards ------------------------------------------------------------

def test_each_entry_condition_states_what_it_needs_and_what_it_is():
    # Both conditions fail on this fixture, so both pills read "not yet".
    out = _text(fx_plan(flat_row(), [], lambda s: 1.1))
    assert "Needs" in out and "Now" in out
    assert "above 1.15603" in out, "the level being waited for, not just a yes/no"
    assert out.count("not yet") == 2 and "met" not in out

    # One of each: price still below the slow average, but the fast one has crossed above it.
    mixed = _text(fx_plan(flat_row(price=1.12575, fast=1.16000, slow=1.15603), [], lambda s: 1.1))
    assert "not yet" in mixed and "met" in mixed


def test_the_size_and_the_worst_case_are_figures_not_a_sentence():
    out = _text(fx_plan(flat_row(), [], lambda s: 1.1))
    for label in ("Buys", "At", "Stop goes at", "Most it can lose"):
        assert label in out, f"{label} should be its own figure"
    assert "$100.00" in out
