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
def page(live_page) -> str:
    """The real rendered page, built once for the whole session (see conftest.live_page)."""
    return live_page


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


# --- the phone alert for a hand-executed trade ------------------------------------------------------

def test_a_buy_alert_carries_the_stop_so_one_message_is_a_complete_instruction():
    """S9 is executed by hand, so the alert IS the trade ticket.

    The buy used to arrive alone and the stop followed on the next engine run, up to fifteen minutes
    later. In that gap you were either out of a trade the rules had taken, or in it unprotected --
    neither of which is what the backtest measured, because there the stop exists from entry.
    """
    from datetime import datetime, timezone

    from trader.engine.live import _fill_text
    from trader.models import AssetClass, Fill, Side

    fill = Fill(client_order_id="x", sleeve_id="S9", symbol="AUD/USD", side=Side.BUY, qty=3439,
                price=0.71230, cost_haircut_usd=0.0, ts=datetime.now(timezone.utc))
    text = _fill_text(fill, "trend up", AssetClass.FOREX, stop=0.69681)
    assert "0.71230" in text and "3439" in text, "what to buy and at what price"
    assert "0.69681" in text, "where the stop goes"
    assert "$53" in text, "what being stopped out costs"
    # A rate is not a dollar amount; "$0.69681" would be wrong.
    assert "$0.69681" not in text


def test_a_sell_alert_does_not_invent_a_stop():
    from datetime import datetime, timezone

    from trader.engine.live import _fill_text
    from trader.models import AssetClass, Fill, Side

    fill = Fill(client_order_id="x", sleeve_id="S9", symbol="AUD/USD", side=Side.SELL, qty=3439,
                price=0.70447, cost_haircut_usd=0.0, ts=datetime.now(timezone.utc))
    assert "Stop" not in _fill_text(fill, "trend ended", AssetClass.FOREX, stop=0.69681)


def test_the_fx_shadow_no_longer_reaches_your_phone():
    """It used to, and that was the mistake.

    S9 runs the FX rules that failed Gate 1 at an out-of-sample Sharpe of -0.184 -- they lost to leaving
    the money in T-bills. It was sending a buy, a stop and a dollar risk figure to a phone, which is a
    standing invitation to act on a strategy this system has already concluded does not work. The first
    fix labelled those messages. The right fix was to stop sending them.

    S9 still runs and still accumulates evidence on the dashboard and in the daily summary, marked as
    simulated. The forex messages that DO arrive come from the intraday watcher, which passed a clean
    out-of-sample test on EUR/USD.
    """
    from trader.engine.live import HAND_EXECUTED

    assert "shadow_fx" not in HAND_EXECUTED
    assert HAND_EXECUTED == set(), "anything in here goes straight to the phone"


# --- today's hand-executed plan, on the page -------------------------------------------------------

def _journal_with_plan(tmp_path, day, symbol="EUR/USD", state=None):
    from trader.journal.db import Journal

    j = Journal(tmp_path / "j.db")
    key = f"irv:{day:%Y-%m-%d}:{symbol.replace('/', '')}"
    j.kv_set(f"{key}:plan", "1.11894,0.005473,1.11621,1.11456,1.11894,60897.7")
    if state:
        j.kv_set(f"{key}:state", state)
    return j


def test_the_plan_you_are_meant_to_act_on_is_actually_on_the_page(settings, tmp_path):
    """It was not. The intraday watcher is the only part of this system whose output is a trade for YOU
    to place, and the dashboard had no reference to it at all -- the Forex page showed S9, the sleeve
    that failed Gate 1 and sends nothing. The answer to "what am I meant to be doing" lived only in a
    Telegram notification you might have missed.
    """
    from datetime import datetime, timezone

    from trader.engine.dashboard import intraday_card

    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    j = _journal_with_plan(tmp_path, now.date(), state="waiting")
    card = intraday_card(settings, j, now)

    assert "Today&#x27;s plan" in card or "Today's plan" in card
    assert "1.11621" in card, "the buy level must be on the page"
    assert "1.11456" in card and "1.11894" in card, "so must the stop and the target"
    assert "you place these" in card, "it must be unmistakable that nothing is sent to a broker"
    assert "Not advice" in card
    j.close()


def test_a_live_trade_is_the_only_state_that_raises_its_voice(settings, tmp_path):
    """Rule 4: space goes to what needs attention. A trade you are holding is the only state you can
    still get it wrong in, so it is the only one that gets an emphasis treatment."""
    from datetime import datetime, timezone

    from trader.engine.dashboard import intraday_card

    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    held = _journal_with_plan(tmp_path / "a", now.date(), state="in")
    (tmp_path / "a").mkdir(exist_ok=True)
    waiting = _journal_with_plan(tmp_path / "b", now.date(), state="waiting")
    (tmp_path / "b").mkdir(exist_ok=True)

    assert "irv-in" in intraday_card(settings, held, now)
    assert "HOLDING" in intraday_card(settings, held, now)
    assert "irv-in" not in intraday_card(settings, waiting, now)
    held.close()
    waiting.close()


def test_when_the_next_plan_is_due_is_said_in_the_readers_own_clock(settings, tmp_path):
    """07:00 UTC is 6pm in Sydney. Quoting only UTC makes the reader do timezone arithmetic to find out
    whether a message is overdue or simply not due yet."""
    from datetime import datetime, timezone

    from trader.engine.dashboard import intraday_card, intraday_today
    from trader.journal.db import Journal

    now = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)      # well before the open
    j = Journal(tmp_path / "j.db")
    card = intraday_card(settings, j, now)
    local = intraday_today(settings, j, now)["open_local"]

    assert "07:00 UTC" in card, "the UTC time the watcher works in is still stated"
    assert f"{local:%-I%p}".lower() in card, "and so is the same moment on the reader's own clock"
    j.close()


def test_an_empty_day_falls_back_to_the_last_session(settings, tmp_path):
    """The setup appears on about one day in three, and before 07:00 UTC there is never a plan. An
    empty card most of the time would teach you not to look at it."""
    from datetime import datetime, timedelta, timezone

    from trader.engine.dashboard import intraday_card

    now = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
    j = _journal_with_plan(tmp_path, (now - timedelta(days=1)).date(), state="done")
    card = intraday_card(settings, j, now)

    assert "No plan yet" in card
    assert "1.11621" in card, "yesterday's levels are more use than an empty card"
    assert "Tue 7 Oct" in card or "7 Oct" in card
    j.close()


def test_the_card_disappears_entirely_when_the_watcher_is_off(settings, tmp_path):
    from trader.journal.db import Journal

    settings.intraday = settings.intraday.model_copy(update={"enabled": False})
    j = Journal(tmp_path / "j.db")
    assert intraday_card_of(settings, j) == ""
    j.close()


def intraday_card_of(settings, journal):
    from trader.engine.dashboard import intraday_card

    return intraday_card(settings, journal)


# --- the risk shown per pair is the sleeve's own configured setting, not a hardcoded figure ---------

def _flat_trend_history(rows: int = 200) -> "pd.DataFrame":
    """Daily bars, flat enough that _fx_levels' call into the strategy never raises -- the figure under
    test is risk_pct, which comes from config, not from anything the market did."""
    import pandas as pd

    dates = pd.date_range("2025-01-01", periods=rows, freq="D")
    return pd.DataFrame({"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 0.0}, index=dates)


def test_risk_pct_comes_from_the_sleeves_own_configured_setting(settings, monkeypatch):
    """_fx_levels used to hardcode risk_pct to 1.0 regardless of what the sleeve was actually configured
    to risk. That number feeds both the risk-budget arithmetic and the "costs X% of the sleeve" text, so
    a sleeve configured to risk 2.5% would still have shown 1.0% -- a wrong number on the one figure that
    states exactly how much a trade can lose.
    """
    from trader.config import SleeveLimits
    from trader.engine.dashboard import _fx_levels

    monkeypatch.setattr("trader.data.history.load_history", lambda symbol: _flat_trend_history())

    sleeve = settings.sleeves["S9"]
    assert sleeve.limits is None or sleeve.limits.risk_per_trade_pct == 1, "fixture must start non-trivial"
    custom = sleeve.model_copy(update={"limits": SleeveLimits(risk_per_trade_pct=2.5)})
    settings.sleeves["S9"] = custom

    out = _fx_levels(settings, custom, ["EUR/USD"])
    assert out["EUR/USD"]["risk_pct"] == 2.5, "must reflect the sleeve's own configured risk, not 1.0"


def test_a_default_sleeve_still_shows_its_real_one_percent(settings, monkeypatch):
    """The default happens to equal the old hardcoded literal, so this pins that the code path reads
    config rather than coincidentally matching it."""
    from trader.engine.dashboard import _fx_levels

    monkeypatch.setattr("trader.data.history.load_history", lambda symbol: _flat_trend_history())

    sleeve = settings.sleeves["S9"]
    out = _fx_levels(settings, sleeve, ["EUR/USD"])
    assert out["EUR/USD"]["risk_pct"] == settings.sleeve_limits("S9").risk_per_trade_pct == 1


# --- every timestamp shown to the reader is in Sydney time, regardless of the host OS clock ---------

def _minimal_dashboard_dict(now):
    """Just enough of collect()'s output for render() to run, so the header timestamp can be checked
    without the cost of building real engines, brokers and a journal."""
    return {
        "now": now, "accounts": [], "total": 0.0, "start_equity": 0.0, "total_pnl": 0.0,
        "day_pnl": 0.0, "day_pct": 0.0, "drawdown": 0.0, "worst_broker": "",
        "day_used": 0.0, "dd_used": 0.0, "risk_level": "Low risk",
        "kill_switch": False, "kill_reason": None,
        "last_run": None, "orders_today": 0, "max_orders": 60,
        "realized": 0.0, "unrealized": 0.0, "fees": 0.0,
        "kpis": {"closed": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                 "avg_r": None, "profit_factor": None, "trading_days": 0, "open": 0},
        "sleeves": [], "trips": [], "price": lambda s: None,
        "states": {}, "watchlist": [], "equities": {}, "orders": [], "rate": None, "fx": {},
        "research": [], "gate2": [], "health": [], "blocked": [],
        "warnings": [], "blocked_today": [],
        "charts": {"account": {}, "sleeves": {}, "shadow": {}},
        "_limits_day": "3", "_limits_dd": "15",
    }


@pytest.mark.parametrize("host_tz", ["UTC", "America/New_York"])
def test_the_header_clock_is_sydney_time_regardless_of_host_timezone(host_tz):
    """`.astimezone()` with no argument converts to whatever timezone the HOST MACHINE happens to be
    set to -- which could be anything depending on where this engine runs -- not necessarily Sydney,
    where the person reading the page actually is.
    """
    import os
    import time
    from datetime import datetime, timezone

    from trader.engine.dashboard import render

    original = os.environ.get("TZ")
    os.environ["TZ"] = host_tz
    time.tzset()
    try:
        now = datetime(2026, 6, 15, 8, 30, tzinfo=timezone.utc)  # 18:30 in Sydney (AEST, UTC+10)
        page = render(_minimal_dashboard_dict(now))
        assert "Monday, 15 June 2026 · 18:30" in page, page[page.index("<header"):page.index("<header") + 300]
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_the_intraday_session_times_are_sydney_time_regardless_of_host_timezone(settings, tmp_path):
    """intraday_today's local_open/local_close feed the "today's plan" card -- the same bug (bare
    `.astimezone()`) would have shown the host machine's own timezone there too.
    """
    import os
    import time
    from datetime import datetime, timezone

    from trader.engine.dashboard import intraday_today
    from trader.journal.db import Journal

    original = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        now = datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc)
        j = Journal(tmp_path / "j.db")
        out = intraday_today(settings, j, now)
        j.close()
        # 07:00 UTC in June is 17:00 in Sydney (AEST, UTC+10 -- no daylight saving in the southern
        # hemisphere winter), and would be 03:00 in New York (EDT, UTC-4) if the host tz leaked in.
        assert out["open_local"].strftime("%H:%M") == "17:00", out["open_local"]
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
