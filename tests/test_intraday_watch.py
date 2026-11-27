"""The intraday watcher: the thing that actually texts you a trade.

It sends no orders, so the risk gate never sees it. That makes these tests the only thing standing
between a bug and a message telling you to buy at the wrong price.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from trader.config import IntradayConfig, load_settings
from trader.engine.intraday_watch import (LOT, MIN_LOT, IntradayWatcher, Plan, build_plan,
                                          plan_text)
from trader.journal.db import Journal

CFG = IntradayConfig(enabled=True, symbols=["EUR/USD"], drop_atr=0.5, stop_atr=0.3, target_frac=1.0,
                     atr_days=20, capital_usd=10_000.0, risk_pct=1.0)
DAY = date(2026, 10, 7)
NEXT_DAY = datetime(2026, 10, 8, 7, 5, tzinfo=timezone.utc)
RANGES = [0.0062] * 20
# Real daily ranges are not all equal, and the difference matters: a flat 0.0062 makes drop_atr * atr
# land exactly on five decimals by luck, which hides every rounding bug in the levels. These are the
# shape of actual EUR/USD days.
ODD_RANGES = [0.01197, 0.00619, 0.00951, 0.00723, 0.00901, 0.00488, 0.00672, 0.00534, 0.00812, 0.00466,
              0.00739, 0.00588, 0.00641, 0.00503, 0.00877, 0.00412, 0.00694, 0.00556, 0.00783, 0.00627]


class Spy:
    """A notifier that delivers. `delivers=False` is the one that does not, for the rollback tests.

    Returning a truthy value is not decoration: the watcher now treats a falsy send as a delivery
    failure and puts the day's state back, so a fake that forgot to return anything would quietly make
    every test here run against a watcher that never advances.
    """

    def __init__(self, delivers: bool = True):
        self.sent: list[str] = []
        self.delivers = delivers

    def send(self, text):
        self.sent.append(text)
        return self.delivers


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "j.db")
    yield j
    j.close()


def _watcher(journal, at: str, rate: float, cfg=CFG, ranges=None, delivers: bool = True):
    now = datetime.fromisoformat(f"2026-10-07T{at}+00:00")
    s = load_settings().model_copy(update={"intraday": cfg})
    return IntradayWatcher(s, journal, Spy(delivers), clock=lambda: now, price=lambda _s: rate,
                           ranges=RANGES if ranges is None else ranges)


def test_the_levels_are_the_ones_the_backtest_used():
    p = build_plan(CFG, 1.16420, RANGES, DAY)
    assert p.trigger == pytest.approx(1.16420 - 0.5 * 0.0062)
    assert p.stop == pytest.approx(p.trigger - 0.3 * 0.0062)
    assert p.target == pytest.approx(1.16420), "target_frac 1.0 means all the way back to the open"
    # Size is worked backwards from the stop so being stopped out costs exactly the risk budget.
    assert p.qty * (p.trigger - p.stop) == pytest.approx(100.0)


def test_the_scale_never_uses_todays_own_range():
    """If today's range set the trigger, the level would be chosen already knowing how far the day went."""
    p = build_plan(CFG, 1.16420, RANGES, DAY)
    huge = build_plan(CFG, 1.16420, RANGES + [0.05], DAY)
    assert huge.atr != p.atr, "a new EARLIER day should move the scale"
    # That the watcher only ever hands it earlier days is checked against behaviour, in
    # test_the_atr_comes_from_whole_utc_days_of_hourly_bars. It used to be asserted by grepping this
    # module for the string "index.date < today", which broke the moment the source of the ranges
    # changed -- while the property it was guarding was still perfectly intact.


def test_a_plan_goes_out_once_at_the_open_and_is_not_repeated(journal):
    w = _watcher(journal, "07:05", 1.16420)
    assert w.run_once() and len(w.notifier.sent) == 1
    assert "Market Execution to BUY LIMIT" in w.notifier.sent[0]
    w2 = _watcher(journal, "07:20", 1.16420)
    w2.run_once()
    assert w2.notifier.sent == [], "the plan must not be re-sent every fifteen minutes"


def test_it_says_buy_only_when_the_rate_reaches_the_level(journal):
    _watcher(journal, "07:05", 1.16420).run_once()          # lay down the plan
    near = _watcher(journal, "08:00", 1.16200)               # above the 1.16110 trigger
    near.run_once()
    assert near.notifier.sent == [], "close is not the same as reached"
    hit = _watcher(journal, "08:15", 1.16100)                # through it
    hit.run_once()
    assert len(hit.notifier.sent) == 1 and "BUY" in hit.notifier.sent[0]
    assert "1.15924" in hit.notifier.sent[0], "the buy message must carry the stop"


def test_it_says_sell_at_the_target(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()
    out = _watcher(journal, "11:00", 1.16430)
    out.run_once()
    assert len(out.notifier.sent) == 1
    assert "CLOSE" in out.notifier.sent[0] and "target" in out.notifier.sent[0]


def test_it_says_sell_at_the_stop(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()
    out = _watcher(journal, "11:00", 1.15900)
    out.run_once()
    assert "CLOSE" in out.notifier.sent[0] and "stop" in out.notifier.sent[0]


def test_one_buy_and_one_sell_per_day_however_often_it_runs(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    for at in ("08:15", "08:30", "08:45"):
        _watcher(journal, at, 1.16100).run_once()
    for at in ("11:00", "11:15"):
        _watcher(journal, at, 1.16430).run_once()
    later = _watcher(journal, "12:00", 1.16000)
    later.run_once()
    assert later.notifier.sent == [], "the day is done; a later dip must not start it again"


def test_nothing_is_sent_outside_the_session_or_at_the_weekend(journal):
    assert _watcher(journal, "05:00", 1.16420).run_once() == []
    assert _watcher(journal, "17:00", 1.16420).run_once() == []
    sat = IntradayWatcher(load_settings().model_copy(update={"intraday": CFG}), journal, Spy(),
                          clock=lambda: datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc),
                          price=lambda _s: 1.16420)
    assert sat.run_once() == [], "FX is shut at the weekend"


def test_an_open_position_is_closed_when_the_session_ends(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()
    end = _watcher(journal, "16:00", 1.16250)
    end.close_out()
    assert "CLOSE" in end.notifier.sent[0] and "session end" in end.notifier.sent[0]


def test_a_disabled_watcher_is_silent(journal):
    w = _watcher(journal, "08:00", 1.16100, cfg=CFG.model_copy(update={"enabled": False}))
    assert w.run_once() == [] and w.notifier.sent == []


def test_the_plan_says_it_is_a_test_and_not_advice():
    text = plan_text(build_plan(CFG, 1.16420, RANGES, DAY), CFG)
    assert "Not advice" in text
    assert "Shadow" in text


def test_a_halted_system_stops_texting_you_trades():
    """It places nothing, so it cannot breach a limit -- but a halted system that keeps sending you
    trades to place by hand has not really halted."""
    import inspect

    from trader.engine import live

    src = inspect.getsource(live.Coordinator._intraday)
    assert "kill_switch.active" in src


def test_a_day_whose_open_was_missed_is_skipped_not_improvised(journal):
    """The levels are measured FROM the session open. If the first run of the day lands hours later --
    a restart, an outage, a sleeping laptop -- then treating that moment's rate as "the open" trades a
    different setup from the one that was tested, while looking identical in the message."""
    late = _watcher(journal, "11:20", 1.11919)
    actions = late.run_once()
    assert actions and "missed" in actions[0]

    # A skip is reported, not improvised over. What must never go out is a PLAN built from 11:20's rate
    # dressed up as the open -- that would look identical to a real one and be a different trade.
    assert len(late.notifier.sent) == 1
    assert "No EUR/USD plan today" in late.notifier.sent[0]
    assert "Market Execution to BUY LIMIT" not in late.notifier.sent[0]

    # And it stays skipped for the rest of that day, however many times the engine runs. The notice is
    # sent once: one message explaining the day, not one every fifteen minutes until midnight.
    for at in ("11:35", "12:00", "14:00"):
        w = _watcher(journal, at, 1.11000)
        assert w.run_once() == [] and w.notifier.sent == []


def test_a_plan_built_inside_the_window_is_still_allowed(journal):
    w = _watcher(journal, "07:25", 1.16420)
    assert w.run_once() and "Market Execution to BUY LIMIT" in w.notifier.sent[0]


def test_a_trade_left_open_by_a_missed_close_is_raised_not_forgotten(journal):
    """Being told to buy and never told to sell is the worst way this can fail.

    `close_out` needs a run after 16:00 UTC. This Mac sleeps at 3am Sydney, which is exactly that. Slept
    through, the day stays "in" forever and the next morning's plan is built over the top, so the trade
    you are holding by hand is silently abandoned.
    """
    yesterday = _watcher(journal, "07:05", 1.11919)
    yesterday.run_once()                                 # plan goes out
    entered = _watcher(journal, "09:00", 1.11500)        # through the trigger, short of the stop
    entered.run_once()
    assert "BUY" in entered.notifier.sent[0]

    # Nothing runs again until the next morning: the 16:00 close never happened.
    later = _watcher(journal, "07:05", 1.12500)
    later.clock = lambda: NEXT_DAY
    actions = later.sweep_unfinished()
    assert actions and "was left open" in actions[0]
    assert len(later.notifier.sent) == 1
    assert "never closed" in later.notifier.sent[0]
    assert "close it now" in later.notifier.sent[0]

    # Said once, not on every run for the rest of time.
    again = _watcher(journal, "07:20", 1.12500)
    again.clock = lambda: NEXT_DAY
    assert again.sweep_unfinished() == [] and again.notifier.sent == []


def test_the_sweep_leaves_today_alone(journal):
    """Today still belongs to close_out, which knows the rate and can quote a real exit."""
    w = _watcher(journal, "07:05", 1.11919)
    w.run_once()
    held = _watcher(journal, "09:00", 1.11500)
    held.run_once()
    mid = _watcher(journal, "12:00", 1.11500)
    assert mid.sweep_unfinished() == [], "a live position must not be declared stranded"
    assert mid.notifier.sent == []


# --- the three bugs the sell path had ---------------------------------------------------------------

def test_the_target_you_are_told_is_the_target_that_triggers_the_sell():
    """The levels are rounded to the precision they are printed in, and watched at that precision.

    Unrounded, a target of 1.118943691 printed as "Target 1.11894" and a rate of exactly 1.11894 then
    failed `rate >= target` by four ten-millionths. The day you watched the rate touch your own number
    was the day nothing was sent -- and with target_frac 1.0 the target IS the session open, so this was
    not a rare corner.
    """
    p = build_plan(CFG, 1.11894369125, ODD_RANGES, DAY)
    for name in ("trigger", "stop", "target", "session_open"):
        level = getattr(p, name)
        assert level == float(format(level, ".5f")), \
            f"{name} is {level!r} but a message would print {level:.5f}"


def test_a_rate_at_the_printed_target_sells(journal):
    w = _watcher(journal, "07:05", 1.11894369125, ranges=ODD_RANGES)
    w.run_once()
    plan = w._stored_plan(DAY, "EUR/USD")
    # Exactly the figures a message prints, not the floats behind them.
    trigger, target = float(f"{plan.trigger:.5f}"), float(f"{plan.target:.5f}")
    _watcher(journal, "09:00", trigger, ranges=ODD_RANGES).run_once()          # in
    out = _watcher(journal, "12:00", target, ranges=ODD_RANGES)
    assert out.run_once(), "the rate reached the stated target and nothing was sent"
    assert "CLOSE" in out.notifier.sent[0] and "target" in out.notifier.sent[0]


def test_a_setup_that_gapped_past_its_own_stop_is_not_a_buy(journal):
    """Both levels crossed inside one 15-minute window, as a news spike does.

    This used to send "BUY now" quoting a stop ABOVE the entry, so the next run told you to sell at a
    loss. The tested rule enters with a resting limit at the trigger: either that filled and the stop
    closed it, or it never happened. A fresh entry below the stop is a different trade.
    """
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    plan = w._stored_plan(DAY, "EUR/USD")

    gap = _watcher(journal, "09:00", plan.stop - 0.0020)
    actions = gap.run_once()
    assert actions and "gapped" in actions[0]
    assert "BUY" not in gap.notifier.sent[0]
    assert "do NOT buy now" in gap.notifier.sent[0]

    # And the day is over: no late entry once the move has already happened.
    assert _watcher(journal, "11:00", plan.trigger).run_once() == []


def test_the_sell_measures_from_what_the_buy_actually_quoted(journal):
    """It measured from the trigger, which is not where the buy message said to buy.

    The buy fires on the first observation at or under the trigger, so a window that steps past it
    quotes a better entry than the trigger -- and the sell then understated the gain. Wrong in your
    favour is still wrong, and it reads as a mistake the first time you check it against your broker.
    """
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    plan = w._stored_plan(DAY, "EUR/USD")
    entry = plan.trigger - 0.0005                 # stepped straight past the trigger, still above the stop
    assert entry > plan.stop

    _watcher(journal, "09:00", entry).run_once()
    out = _watcher(journal, "12:00", plan.target)
    out.run_once()
    message = out.notifier.sent[0]
    assert f"Bought {entry:.5f}" in message, f"the sell must name the entry it signalled: {message}"
    expected = round((plan.target - entry) / 1e-4)
    assert f"{expected:+.0f} pips" in message


# --- what the sizing does on bad data --------------------------------------------------------------

def test_an_impossible_position_size_is_never_sent():
    """A tiny ATR used to produce a message saying to trade 3,333,333 units.

    That is $3.7m of EUR/USD on a $10,000 account behind a 0.3 pip stop the spread alone would take out,
    and nothing stands between that message and you acting on it -- the watcher sends no orders, so the
    risk gate never sees any of this. Measured over 5,896 windows of 23 years the lowest 20-day ATR ever
    seen is 15.8 pips, so an ATR of one pip is a data fault and the only safe plan is no plan.
    """
    assert build_plan(CFG, 1.1200, [0.0001] * 20, DAY) is None


def test_the_quietest_market_ever_recorded_still_gets_a_plan():
    """The floor must reject faults, not real markets, or it is just a different bug."""
    p = build_plan(CFG, 1.1200, [0.00158] * 20, DAY)          # 15.8 pips, the historical minimum
    assert p is not None
    assert p.qty * p.trigger < CFG.capital_usd * 30, "within the retail leverage a position could hold"


def test_size_is_capped_at_retail_leverage_and_says_what_it_then_risks():
    """Risk-based sizing says nothing about whether the position can be held.

    Capping it is safe -- a smaller size loses less -- but then quoting the nominal 1% would OVERSTATE
    the loss, so the figure in the message is derived from the size actually being suggested.
    """
    p = build_plan(CFG, 1.1200, [0.00105] * 20, DAY)
    assert p.qty * p.trigger == pytest.approx(CFG.capital_usd * 30, rel=1e-3)
    assert p.risk_usd < CFG.capital_usd * CFG.risk_pct / 100
    assert p.risk_usd == pytest.approx((p.trigger - p.stop) * p.sized_qty), \
        "derived from the volume the message tells you to type, not from the unrounded quantity"
    assert f"${p.risk_usd:,.0f}" in plan_text(p, CFG)


def test_a_partial_history_is_not_an_atr():
    """The mean of three days is not the statistic the levels were chosen against, and one freak
    300-pip day on its own would set a 90-pip stop and size the trade off it."""
    assert build_plan(CFG, 1.1200, [0.0062, 0.0058], DAY) is None
    assert build_plan(CFG, 1.1200, [0.0300], DAY) is None
    assert build_plan(CFG, 1.1200, [0.0062] * CFG.atr_days, DAY) is not None


def test_a_stale_quote_is_not_acted_on(journal, monkeypatch):
    """The feed stamps the bar's own timestamp so a stale price LOOKS stale. This watcher was reading
    .price and discarding .ts, and it is the one consumer with no risk gate in front of it."""
    from datetime import timedelta

    from trader.models import Quote

    now = datetime.fromisoformat("2026-10-07T09:00+00:00")
    s = load_settings().model_copy(update={"intraday": CFG})
    limit = s.risk.guards.stale_forex_seconds

    class Feed:
        def __init__(self, age_s):
            self.age_s = age_s

        def get_quote(self, symbol, asset_class):
            return Quote(symbol=symbol, price=1.11000, ts=now - timedelta(seconds=self.age_s))

    def watcher(age_s):
        import trader.data.fx_quotes as fq
        monkeypatch.setattr(fq, "YahooFXFeed", lambda *a, **k: Feed(age_s))
        return IntradayWatcher(s, journal, Spy(), clock=lambda: now, ranges=RANGES)

    assert watcher(limit + 60)._rate("EUR/USD") is None, "a quote past the forex staleness limit must not be used"
    assert watcher(60)._rate("EUR/USD") == pytest.approx(1.11000), "a fresh quote must still be used"


def test_the_atr_comes_from_whole_utc_days_of_hourly_bars(journal, monkeypatch):
    """Measured against the Dukascopy bars the research used, over 268 overlapping days, Yahoo's own
    DAILY bar puts the 20-day ATR out by a median of 6.9 pips and a mean of 8.3 -- always in the same
    direction, so it does not wash out. Hourly bars grouped into UTC days are out by 1.2.

    Eight pips of ATR moves the buy level ~3.5 pips and tightens the stop ~9%, so the daily-bar version
    entered earlier and stopped out more often than the run that produced +17.2%/yr.
    """
    import numpy as np
    import pandas as pd

    now = datetime(2026, 10, 8, 7, 5, tzinfo=timezone.utc)
    rows, want = [], {}
    for d in range(1, 9):
        day = datetime(2026, 10, d, tzinfo=timezone.utc)
        hours = 24 if d % 7 not in (6, 0) else 6        # two short days stand in for a weekend
        for h in range(hours):
            rows.append((day.replace(hour=h), 1.10 + 0.001 * h, 1.10 + 0.001 * h - 0.0005))
        if hours == 24 and day.date() < now.date():      # today's own range must never be in the scale
            want[day.date()] = (1.10 + 0.001 * 23) - (1.10 - 0.0005)

    frame = pd.DataFrame({"High": [r[1] for r in rows], "Low": [r[2] for r in rows]},
                         index=pd.DatetimeIndex([r[0] for r in rows]))

    class Ticker:
        def __init__(self, *a, **k): pass
        def history(self, period, interval):
            assert interval == "1h", "the daily bar is the thing being avoided"
            return frame

    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        type("m", (), {"Ticker": Ticker, "__spec__": None}))
    s = load_settings().model_copy(update={"intraday": CFG})
    w = IntradayWatcher(s, journal, Spy(), clock=lambda: now)
    got = w._daily_ranges("EUR/USD")

    # 8 October is today and is excluded; the two short days are not whole days.
    assert len(got) == len(want), f"expected {sorted(want)}, got {len(got)} ranges"
    assert got == [pytest.approx(v) for _, v in sorted(want.items())]
    assert all(abs(g - 0.0235) < 1e-9 for g in got), "a day's range is its own high minus its own low"


def test_no_price_history_means_no_plan_rather_than_a_guess(journal, monkeypatch):
    """Falling back to the daily bars would quietly resume trading the biased version."""
    class Ticker:
        def __init__(self, *a, **k): pass
        def history(self, period, interval):
            raise RuntimeError("yahoo is down")

    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        type("m", (), {"Ticker": Ticker, "__spec__": None}))
    s = load_settings().model_copy(update={"intraday": CFG})
    w = IntradayWatcher(s, journal, Spy(), clock=lambda: datetime(2026, 10, 8, 7, 5, tzinfo=timezone.utc),
                        price=lambda _s: 1.11894)
    assert w.run_once() == [] and w.notifier.sent == []
    assert w._stored_plan(date(2026, 10, 8), "EUR/USD") is None


def test_a_day_that_never_triggered_says_so_at_the_close(journal):
    """You asked why no messages were arriving. The answer was that the setup had not happened -- which
    the system knew all day and never said. Silence should not need interpreting."""
    w = _watcher(journal, "07:05", 1.11919)
    w.run_once()
    assert "Market Execution to BUY LIMIT" in w.notifier.sent[0]

    quiet = _watcher(journal, "16:05", 1.12100)          # drifted up all day, never near the trigger
    actions = quiet.run_once() + quiet.close_out()
    assert actions and "no EUR/USD setup today" in actions[0]
    assert len(quiet.notifier.sent) == 1
    assert "No EUR/USD trade today" in quiet.notifier.sent[0]
    assert "one day in three" in quiet.notifier.sent[0], "the frequency is the reassurance"

    # Once, not on every run after the close.
    again = _watcher(journal, "16:20", 1.12100)
    assert again.run_once() + again.close_out() == [] and again.notifier.sent == []


def test_a_day_that_did_trade_still_gets_a_sell_not_a_quiet_note(journal):
    """The quiet note must not swallow the session-end exit."""
    _watcher(journal, "07:05", 1.11919).run_once()
    plan = _watcher(journal, "07:05", 1.0, )._stored_plan(DAY, "EUR/USD")
    _watcher(journal, "09:00", plan.trigger).run_once()   # in
    out = _watcher(journal, "16:05", plan.trigger + 0.0010)
    out.run_once()
    actions = out.close_out()
    assert actions and "SELL" in actions[0]
    assert "CLOSE EUR/USD now" in out.notifier.sent[0] and "session end" in out.notifier.sent[0]
    assert "No EUR/USD trade today" not in "".join(out.notifier.sent)


# --- two pairs -------------------------------------------------------------------------------------

TWO = CFG.model_copy(update={"symbols": ["EUR/USD", "GBP/USD"]})


def _two(journal, at: str, rates: dict, ranges=None):
    now = datetime.fromisoformat(f"2026-10-07T{at}+00:00")
    s = load_settings().model_copy(update={"intraday": TWO})
    return IntradayWatcher(s, journal, Spy(), clock=lambda: now, price=lambda sym: rates[sym],
                           ranges=RANGES if ranges is None else ranges)


def test_both_pairs_get_their_own_plan():
    """GBP/USD was added on the strength of surviving EUR/USD's parameters unchanged. If the loop ever
    silently watched one pair, the other would simply never message and nothing would say so."""
    from trader.journal.db import Journal
    import tempfile

    j = Journal(pathlib_tmp := __import__("pathlib").Path(tempfile.mkdtemp()) / "j.db")
    w = _two(j, "07:05", {"EUR/USD": 1.11919, "GBP/USD": 1.34500})
    actions = w.run_once()
    assert len(actions) == 2, actions
    assert any("EUR/USD" in a for a in actions) and any("GBP/USD" in a for a in actions)
    assert len(w.notifier.sent) == 2
    assert any("EUR/USD plan" in m for m in w.notifier.sent)
    assert any("GBP/USD plan" in m for m in w.notifier.sent)

    # The levels differ because the rates differ: one plan is not being reused for both.
    eur = w._stored_plan(DAY, "EUR/USD")
    gbp = w._stored_plan(DAY, "GBP/USD")
    assert eur.trigger != gbp.trigger and gbp.session_open == pytest.approx(1.34500)
    j.close()


def test_one_pair_triggering_leaves_the_other_alone():
    """Per-pair state. A shared key would have had GBP's buy mark EUR as "in", so EUR would never fire
    again and a position you were never told to open would later be closed."""
    from trader.journal.db import Journal
    import tempfile

    j = Journal(__import__("pathlib").Path(tempfile.mkdtemp()) / "j.db")
    _two(j, "07:05", {"EUR/USD": 1.11919, "GBP/USD": 1.34500}).run_once()
    gbp = j.kv_get("irv:2026-10-07:GBPUSD:plan")
    assert gbp, "the key must carry the pair"

    plan = IntradayWatcher(load_settings().model_copy(update={"intraday": TWO}), j, Spy(),
                           clock=lambda: datetime.fromisoformat("2026-10-07T09:00+00:00"),
                           price=lambda s: 1.0, ranges=RANGES)._stored_plan(DAY, "GBP/USD")

    # GBP falls to its trigger; EUR does not move.
    w = _two(j, "09:00", {"EUR/USD": 1.11919, "GBP/USD": plan.trigger})
    actions = w.run_once()
    assert len(actions) == 1 and "BUY GBP/USD" in actions[0]
    assert j.kv_get("irv:2026-10-07:GBPUSD:state") == "in"
    assert j.kv_get("irv:2026-10-07:EURUSD:state") is None, "EUR must still be waiting"
    assert "GBP/USD" in w.notifier.sent[0] and "EUR/USD" not in w.notifier.sent[0]
    j.close()


def test_only_the_pair_that_passed_honest_selection_is_watched():
    """EUR/USD alone, and the two exclusions are different failures.

    AUD/USD tested NEGATIVE at -1.3%/yr, with 0.0% of its London bars tight enough for any filter.
    GBP/USD was added on 2026-10-08 and removed the same evening: once the harness honoured the 16:00
    close that the live watcher enforces, GBP's OWN in-sample pick scored -2.0%/yr out of sample and
    only 10 of 30 grid settings were positive in both halves. The +10%/yr it showed on the shipped
    setting is a setting chosen on EUR/USD that loses in-sample on GBP, which is evidence of nothing.
    """
    from trader.config import load_settings as real

    cfg = real().intraday
    assert cfg.symbols == ["EUR/USD"]
    assert "AUD/USD" not in cfg.symbols and "GBP/USD" not in cfg.symbols
    # The setting shipped is the one EUR/USD's own in-sample half picks, with the close enforced.
    assert (cfg.drop_atr, cfg.stop_atr, cfg.target_frac) == (0.5, 0.3, 1.0)


def test_each_pair_carries_its_own_evidence_not_a_stale_footer():
    """The footer said "Shadow test, EUR/USD only" on every message, so the GBP/USD plan asserted a pair
    it was not about -- and, worse, implied GBP had EUR's result.

    They are not equally believed: EUR/USD tested +17.2%/yr out of sample and GBP/USD +3.0%, with spread
    taking 45% of the gross. A signal that hides that difference is misleading about its own strength.
    """
    from trader.engine.intraday_watch import EVIDENCE

    cfg = load_settings().intraday
    for symbol in cfg.symbols:
        assert symbol in EVIDENCE, f"{symbol} is being sent with no stated evidence"

    def footer(symbol):
        p = Plan(day=DAY, symbol=symbol, session_open=1.2, atr=0.006, trigger=1.197,
                 stop=1.1952, target=1.2, qty=50_000)
        return plan_text(p, cfg).splitlines()[-1]

    assert "EUR/USD only" not in footer("GBP/USD"), "the footer must not assert a pair it is not about"
    assert "+4.0%" in footer("EUR/USD"), "the corrected figure, not the +17.2% a broken deadline produced"
    assert "+17.2%" not in footer("EUR/USD")
    assert "thin" in footer("EUR/USD")
    assert "Not advice." in footer("EUR/USD")


def test_a_pair_with_no_measured_result_says_so_rather_than_borrowing_one():
    """Adding a symbol to the config must not silently inherit another pair's number."""
    from trader.engine.intraday_watch import plan_text as pt

    cfg = load_settings().intraday
    p = Plan(day=DAY, symbol="USD/JPY", session_open=150.0, atr=0.6, trigger=149.7,
             stop=149.52, target=150.0, qty=1000)
    assert "not yet measured on this pair" in pt(p, cfg).splitlines()[-1]


def test_what_the_watcher_did_reaches_the_journal():
    """It did not. The watcher runs in the coordinator's post-run block, AFTER each engine has logged
    its own "run finished" line, so every intraday action landed in a report nobody persisted.

    Asked why no currency messages were arriving, there was no record to check -- the kv state said a day
    had finished and nothing said what had been sent. These are the only messages that are meant to be
    acted on by hand, so they are the last thing that should go unlogged.
    """
    import inspect

    from trader.engine import live

    src = inspect.getsource(live.Coordinator._intraday)
    assert "self.journal.log(what" in src, "the watcher's actions must be journalled"
    assert "for what in did:" in src


# --- two processes watching at once -----------------------------------------------------------------

def test_running_the_watcher_twice_over_sends_nothing_twice(journal):
    """Idempotency: the second pass reads what the first wrote and stays quiet.

    This is the easy half, and the half that was already true. It is NOT a test of the race -- two
    sequential calls cannot interleave, which is exactly why the first version of this test passed with
    the compare-and-set removed. The race itself is tested below.
    """
    _watcher(journal, "07:05", 1.11919).run_once()
    plan = _watcher(journal, "07:05", 1.0)._stored_plan(DAY, "EUR/USD")

    a = _watcher(journal, "09:00", plan.trigger)
    b = _watcher(journal, "09:00", plan.trigger)
    did_a, did_b = a.run_once(), b.run_once()
    assert len(a.notifier.sent + b.notifier.sent) == 1
    assert len([x for x in (did_a, did_b) if x]) == 1
    assert journal.kv_get("irv:2026-10-07:EURUSD:state") == "in"


def test_two_processes_that_both_read_waiting_still_send_one_buy(journal):
    """The real race, written so it actually interleaves.

    The watcher now runs in two places -- its own minute-by-minute agent and the engine's fifteen-minute
    pass -- and they can read the day's state at the same instant. BOTH see "waiting", BOTH conclude the
    level was reached, and with a plain read-then-write both would send you the same buy and you would
    place the trade twice. The write is what has to be exclusive, so the write carries the condition.
    """
    _watcher(journal, "07:05", 1.11919).run_once()
    a = _watcher(journal, "09:00", 1.11000)
    b = _watcher(journal, "09:00", 1.11000)

    # Both read first: neither has written yet, so both see an unclaimed day.
    assert a._get(DAY, "state", "EUR/USD") is None
    assert b._get(DAY, "state", "EUR/USD") is None

    # Then both try to claim it. Exactly one may win.
    won = [a._claim(DAY, "EUR/USD", None, "in"), b._claim(DAY, "EUR/USD", None, "in")]
    assert won.count(True) == 1, f"both processes claimed the same transition: {won}"
    assert journal.kv_get("irv:2026-10-07:EURUSD:state") == "in"


def test_every_message_the_watcher_sends_is_behind_a_claim():
    """One exclusive claim per message, counted.

    A claim that nothing calls protects nothing, which is how three of today's bugs got in. The first
    version of this test scanned the six lines above each send for a claim -- and passed when the buy's
    claim was deleted, because six lines up it found the *gapped* branch's claim instead. Counting
    cannot be fooled that way: remove a guard and the two numbers stop matching.
    """
    import inspect

    from trader.engine.intraday_watch import IntradayWatcher

    for method in (IntradayWatcher._run_symbol, IntradayWatcher._close_symbol,
                   IntradayWatcher.sweep_unfinished):
        src = inspect.getsource(method)
        sends = src.count("self._announce(")
        guards = src.count("self._claim(") + src.count("self.journal.kv_set_if(")
        assert sends > 0, f"{method.__name__} sends nothing; this test is watching the wrong place"
        assert guards == sends, (
            f"{method.__name__} has {sends} message(s) and {guards} exclusive claim(s): "
            "every message must be behind one, or two processes can send the same alert")
        # And every message goes through `_announce`, never the notifier directly. A bare send is a
        # message with no delivery check behind it, which is how a claimed state could advance while
        # the message that was supposed to go with it vanished.
        assert "self.notifier.send(" not in src, (
            f"{method.__name__} calls the notifier directly; use _announce so a failed send is undone")


def test_the_quick_watcher_is_its_own_command_and_touches_no_broker():
    """The engine cannot just run every minute: it reconciles the broker, re-prices forty markets,
    rebuilds the dashboard and counts against the daily order budget. So the part that has to be quick
    runs alone -- and must stay that way, or this becomes a minute-by-minute engine."""
    import inspect

    from trader import cli

    src = inspect.getsource(cli.cmd_watch_fx)
    assert "IntradayWatcher" in src
    assert "kill_switch" in src.lower(), "a halted system must not keep texting trades"
    for forbidden in ("configured_brokers", "Engine(", "write_dashboard", "Coordinator"):
        assert forbidden not in src, f"the quick path must not do {forbidden}"
    assert "watch-fx" in inspect.getsource(cli.main)


# --- the message has to fit the ticket you are actually looking at ---------------------------------
#
# These are the only messages in the system that a human executes by hand, and they were written as a
# summary of a trade rather than as instructions for a broker ticket. The two are not the same thing:
# a ticket asks for Volume in LOTS, a Price, a Stop Loss and a Take Profit, and the message quoted raw
# units with no field names at all. Being handed "51,282 units" in front of a box that wants 0.51 is a
# conversion done in a hurry, at the one moment a wrong number costs money.

def _plan(qty: float = 41_152.0) -> Plan:
    return Plan(day=DAY, symbol="EUR/USD", session_open=1.16420, atr=0.0062,
                trigger=1.16110, stop=1.15924, target=1.16420, qty=qty)


def test_the_size_is_quoted_in_lots_because_that_is_what_the_volume_box_takes():
    from trader.engine.intraday_watch import buy_text

    p = _plan()
    for text in (plan_text(p, CFG), buy_text(p, p.trigger)):
        assert "Volume" in text, "the number needs the name of the box it goes in"
        assert f"{p.lots:.2f}" in text
    assert p.lots == pytest.approx(0.41)


def test_rounding_the_volume_can_only_ever_make_the_trade_smaller():
    """A ticket steps in 0.01 lots, so the sized quantity has to be rounded to reach the box.

    Rounding to nearest would round UP half the time, and then the message tells you to take more risk
    than it just quoted. Down is the only direction that cannot do that.
    """
    for qty in (41_152.0, 41_999.0, 150_001.0, 99_999.0):
        p = _plan(qty)
        assert p.sized_qty <= qty, f"{qty} was rounded up to {p.sized_qty}"
        assert qty - p.sized_qty < LOT * MIN_LOT, "rounded away more than one step"


def test_a_size_below_one_step_still_names_a_volume_a_ticket_would_accept():
    """0.00 in the volume box is not an order. Floor it and let the dollar figures follow."""
    p = _plan(400.0)
    assert p.lots == MIN_LOT
    assert "0.00" not in plan_text(p, CFG).split("Risks")[0]


def test_the_dollars_are_measured_on_the_volume_you_were_told_to_type():
    """Not on the unrounded quantity. The two differ by the rounding, and the message shows only one
    of them -- so the figure that is NOT shown must not be the one the dollars came from."""
    p = _plan(41_999.0)
    assert p.sized_qty == 41_000.0, "0.41 lots, not 0.42"
    assert p.risk_usd == pytest.approx((p.trigger - p.stop) * p.sized_qty)
    assert p.risk_usd != pytest.approx((p.trigger - p.stop) * p.qty)


def test_every_level_in_the_plan_is_labelled_with_its_box_on_the_ticket():
    text = plan_text(_plan(), CFG)
    for field in ("Volume", "Price", "Stop Loss", "Take Profit"):
        assert field in text, f"nothing tells you which box {field} belongs in"


def test_the_buy_message_names_the_two_levels_rather_than_just_the_prices():
    from trader.engine.intraday_watch import buy_text

    text = buy_text(_plan(), 1.16110)
    assert "Stop Loss" in text and "Take Profit" in text


# --- the exit message must not read as an order to place -------------------------------------------

def test_the_exit_never_tells_you_to_press_sell():
    """On a broker ticket SELL is not the opposite of BUY -- it is a NEW trade the other way.

    "SELL EUR/USD now" in front of the screen in the user's own screenshots reads as the big red Sell
    by Market button, which on a hedging account leaves two open positions instead of none, and the
    session-end exit the rule depends on never happens.
    """
    from trader.engine.intraday_watch import sell_text, stranded_text

    text = sell_text(_plan(), 1.16300, "session end", 1.16110)
    assert text.startswith("🔴 CLOSE"), "the verb is close, not sell"
    assert "positions list" in text
    assert "Do NOT use Sell by Market" in text
    assert "Do NOT use Sell by Market" not in text.replace("Do NOT use Sell by Market", "", 1), \
        "said once, not twice"
    assert "Sell order" in stranded_text(_plan(), 1.16110), "the stranded day has the same trap"


def test_an_exit_the_broker_already_made_says_there_is_nothing_to_do():
    """If the Stop Loss and Take Profit went on the ticket, the broker has closed the trade itself.

    Sending "CLOSE now" for a position that is already flat is how a trade gets closed twice, and the
    second close is a fresh position in the opposite direction.
    """
    from trader.engine.intraday_watch import sell_text

    for why, level in (("target", "Take Profit"), ("stop", "Stop Loss")):
        text = sell_text(_plan(), 1.16420, why, 1.16110)
        assert "Nothing to do" in text and level in text, why
        assert "positions list" not in text, "it is not asking you to do anything"


def test_a_day_that_never_triggered_tells_you_to_cancel_the_resting_order():
    """The plan message now has you leave a limit order sitting at the broker.

    Nothing cancelled it. Left there it is live tomorrow, against levels measured for today, which is a
    trade nobody planned and nothing in this system is watching.
    """
    from trader.engine.intraday_watch import no_setup_text

    text = no_setup_text(_plan(), CFG)
    assert "cancel it" in text.lower()
    assert "1.16110" in text, "say which order"


# --- when the broker's minimum is bigger than the risk budget ---------------------------------------
#
# The account these messages are typed into holds $175. One percent of that, over a median 24 pip stop,
# buys 723 units -- and the smallest trade a broker accepts is 1,000. So the sizing arithmetic stops
# choosing the position and the broker's floor chooses it instead, at whatever that costs on the day.
# Config still says 1%. The ticket does something else. That is this project's recurring bug -- two
# things that must agree, with nothing checking -- so the message has to say it out loud.

SMALL = CFG.model_copy(update={"capital_usd": 175.0})


def test_a_small_account_is_told_the_minimum_is_setting_the_size_not_the_rule():
    p = build_plan(SMALL, 1.16420, [0.00806] * 20, DAY)
    assert p.floored and p.lots == MIN_LOT
    text = plan_text(p, SMALL)
    assert "smallest trade your broker takes" in text
    assert "1.4% of your account" in text, "the real figure, not the 1% the config asks for"
    assert p.risk_pct_actual > SMALL.risk_pct, "the floor can only push risk UP"


def test_an_account_big_enough_is_not_warned_about_a_floor_that_is_not_binding():
    """Or the note is decoration, and the next reader learns to skip it."""
    p = build_plan(CFG, 1.16420, [0.00806] * 20, DAY)
    assert not p.floored
    assert "smallest trade your broker takes" not in plan_text(p, CFG)


def test_the_warning_survives_the_journal_into_the_buy_message(journal):
    """The BUY message is built from the STORED plan, not the fresh one.

    Capital is not in the stored row -- it is a setting, not something measured about that day -- so a
    plan read back out of the journal had no capital and silently dropped the warning. The one message
    sent at the moment money is about to move is exactly the one that must not lose it.
    """
    w = _watcher(journal, "07:05", 1.16420, cfg=SMALL)
    w.run_once()
    assert "smallest trade your broker takes" in w.notifier.sent[0], "the plan says it"

    stored = w._stored_plan(DAY, "EUR/USD")
    assert stored.capital_usd == SMALL.capital_usd, "capital came back from the config, not from zero"

    hit = _watcher(journal, "09:00", stored.trigger, cfg=SMALL)
    hit.run_once()
    assert "BUY EUR/USD now" in hit.notifier.sent[0]
    assert "smallest trade your broker takes" in hit.notifier.sent[0], "and so does the buy"


def test_a_plan_with_no_stated_capital_omits_the_percentage_rather_than_inventing_one():
    p = Plan(day=DAY, symbol="EUR/USD", session_open=1.16420, atr=0.00806,
             trigger=1.16017, stop=1.15775, target=1.16420, qty=723.0)
    assert p.floored and p.risk_pct_actual == 0.0
    text = plan_text(p, SMALL)
    assert "smallest trade your broker takes" in text
    assert "of your account" not in text


# --- the broker's minimum is a guess, so it has to be a setting -------------------------------------
#
# Asked to confirm it, the answer was "0.01 I think" -- and the one ticket we have a picture of showed
# 0.02 in the volume box. On a $175 account this number sets the position on every trade, so a wrong
# one is wrong every single time, always in the direction of more risk than intended. Hardcoded, that
# costs a code change to fix; as config it is one line, and it is written down as unconfirmed.

def test_the_broker_minimum_comes_from_config_not_from_a_constant():
    two = SMALL.model_copy(update={"min_lot": 0.02})
    p = build_plan(two, 1.16420, [0.00806] * 20, DAY)
    assert p.lots == 0.02 and p.sized_qty == 2_000
    assert "0.02 lots is the smallest trade" in plan_text(p, two), "the note quotes the real step"
    assert "0.01" not in plan_text(p, two).split("NOTE:")[1]


def test_a_bigger_minimum_doubles_the_risk_and_the_message_says_the_bigger_number():
    one = build_plan(SMALL, 1.16420, [0.00806] * 20, DAY)
    two = build_plan(SMALL.model_copy(update={"min_lot": 0.02}), 1.16420, [0.00806] * 20, DAY)
    assert two.risk_usd == pytest.approx(2 * one.risk_usd)
    assert two.risk_pct_actual == pytest.approx(2.8, abs=0.1)
    assert f"${two.risk_usd:,.2f}" in plan_text(two, SMALL)


def test_rounding_steps_down_to_the_brokers_own_step_not_always_to_a_hundredth():
    """A 0.1-lot broker must not be told to type 0.37. That volume would be rejected, or worse,
    silently rounded by the broker to something the message never quoted."""
    cfg = CFG.model_copy(update={"min_lot": 0.1, "capital_usd": 100_000.0})
    p = build_plan(cfg, 1.16420, [0.00806] * 20, DAY)
    assert p.lots == pytest.approx(round(p.lots / 0.1) * 0.1), "a whole number of steps"
    assert p.sized_qty <= p.qty, "and still never rounded up"


def test_the_brokers_step_survives_the_journal_into_the_buy_message(journal):
    """Same trap as capital: the BUY message is rebuilt from the stored row, which has no step in it."""
    two = SMALL.model_copy(update={"min_lot": 0.02})
    w = _watcher(journal, "07:05", 1.16420, cfg=two)
    w.run_once()
    stored = w._stored_plan(DAY, "EUR/USD")
    assert stored.min_lot == 0.02 and stored.lots == 0.02

    hit = _watcher(journal, "09:00", stored.trigger, cfg=two)
    hit.run_once()
    assert "Volume       0.02" in hit.notifier.sent[0], "the buy quotes this broker's step, not 0.01"


def test_the_floor_is_judged_against_the_brokers_step_not_a_hardcoded_one():
    """Whether the warning fires has to be asked about THIS broker's minimum.

    The doubling test above does not pin this down: at $175 the wanted size is under 0.01 lots, so a
    `floored` check left hardcoded at 0.01 still says True and the note still appears, by luck. This
    is the case that tells them apart -- an account that wants 0.03 lots, at a broker whose smallest
    trade is 0.05. Against 0.01 that reads as comfortably above the floor; against the real step the
    minimum is still choosing the size, and the reader still needs telling.
    """
    cfg = CFG.model_copy(update={"capital_usd": 726.0, "min_lot": 0.05})
    p = build_plan(cfg, 1.16420, [0.00806] * 20, DAY)
    assert 0.01 < p.qty / LOT < 0.05, "above a hundredth, below this broker's step"
    assert p.floored, "the step that matters is the broker's, not 0.01"
    assert p.lots == 0.05
    assert "smallest trade your broker takes" in plan_text(p, cfg)


# --- a message that did not arrive must not leave the state saying it did -----------------------------
#
# Every transition claims the day's state before it sends, and has to: the watcher runs in two places,
# so the write is what makes the decision exclusive. The cost was silent and bad. One timed-out request
# left the state at "in" with nobody told to buy, and the next message you would get was a SELL for a
# position that was never opened, quoting a profit on money that was never at risk.

def test_an_undelivered_buy_does_not_leave_the_state_saying_you_are_in(journal):
    """The one that mattered. A failed BUY rolls back to waiting, and the next pass tries again."""
    plan = _watcher(journal, "07:05", 1.16420)
    plan.run_once()
    trigger = plan._stored_plan(DAY, "EUR/USD").trigger

    lost = _watcher(journal, "09:00", trigger, delivers=False)
    out = lost.run_once()
    assert len(lost.notifier.sent) == 1, "it must have tried"
    assert "not delivered" in out[0]
    assert lost._get(DAY, "state", "EUR/USD") is None, "the claim must be undone"
    assert lost._entry(DAY, "EUR/USD") is None, "and the entry rate with it"

    again = _watcher(journal, "09:01", trigger)
    assert again.run_once() == [f"intraday BUY EUR/USD at {trigger:.5f}"]
    assert again._get(DAY, "state", "EUR/USD") == "in"
    assert "BUY" in again.notifier.sent[0]


def test_an_undelivered_sell_goes_back_to_in_and_never_re_sends_a_buy(journal):
    """The rollback direction is load-bearing, and the wrong one is worse than not rolling back.

    A SELL claims "in" -> "done". DELETING that key leaves no state, which reads as "waiting" -- and
    the next pass, seeing a rate under the trigger, would send a BUY for a trade that had already
    closed. So an overwritten value is restored rather than removed.
    """
    plan = _watcher(journal, "07:05", 1.16420)
    plan.run_once()
    stored = plan._stored_plan(DAY, "EUR/USD")
    _watcher(journal, "09:00", stored.trigger).run_once()
    assert plan._get(DAY, "state", "EUR/USD") == "in"

    lost = _watcher(journal, "12:00", stored.target, delivers=False)
    assert "not delivered" in lost.run_once()[0]
    assert lost._get(DAY, "state", "EUR/USD") == "in", "it must go back to in, not to nothing"

    # And the retry is a SELL, not a second BUY.
    again = _watcher(journal, "12:01", stored.target)
    assert again.run_once() == [f"intraday SELL EUR/USD at {stored.target:.5f} (target)"]
    assert "SELL" in again.notifier.sent[0] or "CLOSE" in again.notifier.sent[0]


def test_an_undelivered_plan_is_rebuilt_and_sent_again(journal):
    """The plan message is the ticket. Losing it leaves nothing to place by hand all day."""
    lost = _watcher(journal, "07:05", 1.16420, delivers=False)
    assert "could not be delivered" in lost.run_once()[0]
    assert lost._stored_plan(DAY, "EUR/USD") is None, "the stored plan must be undone too"

    again = _watcher(journal, "07:06", 1.16420)
    assert again.run_once() and "Market Execution to BUY LIMIT" in again.notifier.sent[0]


def test_a_failed_send_is_recorded_where_someone_can_read_it(journal):
    """There is nothing to send the bad news over, so it has to be written down."""
    lost = _watcher(journal, "07:05", 1.16420, delivers=False)
    lost.run_once()
    logs = [r["message"] for r in journal.conn.execute(
        "SELECT message FROM events WHERE level='error'").fetchall()]
    assert any("could not deliver" in m for m in logs), logs


def test_a_rollback_does_not_touch_a_claim_another_process_has_moved_on(journal):
    """`kv_del_if` and `kv_set_if` are conditional for a reason: the engine's fifteen-minute pass and
    this minute-by-minute one both run. If the other process has advanced the state in the meantime, a
    failed send here must leave it alone rather than reopening a settled day."""
    plan = _watcher(journal, "07:05", 1.16420)
    plan.run_once()
    w = _watcher(journal, "09:00", plan._stored_plan(DAY, "EUR/USD").trigger, delivers=False)

    key = w._key(DAY, "state", "EUR/USD")
    original = w.notifier.send

    def send_then_someone_else_moves_on(text):
        journal.kv_set(key, "done")          # the other pass finished the day while this send hung
        return original(text)

    w.notifier.send = send_then_someone_else_moves_on
    w.run_once()
    assert journal.kv_get(key) == "done", "a stale rollback must not reopen the day"


# --- silence must never be ambiguous -----------------------------------------------------------------
#
# Four days of downtime looked exactly like four quiet days. `launchctl` counts a run when it ATTEMPTS
# the job and a quiet pass prints nothing, so a stale log and a stale database were the only symptoms
# of six agents that had not executed since 2026-11-13 -- and both are also what a market with no
# setups looks like. `missed` could not help: it only fires from inside a session, so a day the Mac
# slept through the 07:00 UTC open leaves no row at all.

def test_every_pass_records_that_it_actually_ran(journal):
    """The key has no market, price or network dependency, which is the whole point: if it is not
    moving, the watcher is not running, and nothing else here can tell you that."""
    from trader.engine.intraday_watch import LAST_PASS_KEY

    w = _watcher(journal, "20:00", 1.16420)        # outside the session: a pass that does nothing
    assert w.run_once() == []
    w.report_downtime()
    assert journal.kv_get(LAST_PASS_KEY) == "2026-10-07T20:00:00+00:00"


def test_a_long_gap_is_reported_with_the_sessions_it_swallowed(journal):
    """What actually happened: 2026-11-13 to 2026-11-17, three weekday opens, nothing sent."""
    from trader.engine.intraday_watch import LAST_PASS_KEY

    journal.kv_set(LAST_PASS_KEY, "2026-11-13T06:00:00+00:00")
    s = load_settings().model_copy(update={"intraday": CFG})
    back = datetime.fromisoformat("2026-11-17T06:00:00+00:00")
    w = IntradayWatcher(s, journal, Spy(), clock=lambda: back, price=lambda _s: 1.16,
                        ranges=RANGES)

    out = w.report_downtime()
    assert out and "96h" in out[0]
    message = w.notifier.sent[0]
    assert "was not running for 4 days" in message
    # Mon 16, Fri 13 and Mon 16 are weekdays; Sat 14 and Sun 15 must not be counted.
    assert "Fri 13 Nov" in message and "Mon 16 Nov" in message
    assert "Sat 14" not in message and "Sun 15" not in message


def test_a_weekend_gap_is_not_reported_as_missed_sessions(journal):
    """The market is shut. Crying wolf every Monday is how a check gets ignored."""
    from trader.engine.intraday_watch import LAST_PASS_KEY

    journal.kv_set(LAST_PASS_KEY, "2026-11-14T06:00:00+00:00")       # Saturday
    s = load_settings().model_copy(update={"intraday": CFG})
    monday = datetime.fromisoformat("2026-11-16T06:30:00+00:00")     # before Monday's 07:00 open
    w = IntradayWatcher(s, journal, Spy(), clock=lambda: monday, price=lambda _s: 1.16, ranges=RANGES)

    w.report_downtime()
    assert "No London open fell inside the gap" in w.notifier.sent[0]


def test_a_session_that_did_send_a_plan_is_not_counted_as_missed(journal):
    """Otherwise a laptop asleep for one evening would report the day's own successful plan as lost."""
    from trader.engine.intraday_watch import LAST_PASS_KEY

    planned = _watcher(journal, "07:05", 1.16420)
    planned.run_once()                                   # Wed 7 Oct gets a real plan
    assert planned._stored_plan(DAY, "EUR/USD") is not None

    journal.kv_set(LAST_PASS_KEY, "2026-10-07T06:00:00+00:00")
    s = load_settings().model_copy(update={"intraday": CFG})
    later = datetime.fromisoformat("2026-10-07T18:00:00+00:00")
    w = IntradayWatcher(s, journal, Spy(), clock=lambda: later, price=lambda _s: 1.16, ranges=RANGES)

    w.report_downtime()
    assert "No London open fell inside the gap" in w.notifier.sent[0], w.notifier.sent[0]


def test_an_outage_is_reported_once_and_not_on_every_pass(journal):
    """It runs every minute. Reporting the same four-day gap sixty times an hour would make you mute
    the channel, which costs you the messages this whole project exists to send.

    This passes on the heartbeat alone: the first pass writes `last_pass`, so the second sees a
    one-minute gap. The race below is what the separate dedupe key is actually for.
    """
    from trader.engine.intraday_watch import LAST_PASS_KEY

    journal.kv_set(LAST_PASS_KEY, "2026-11-13T06:00:00+00:00")
    s = load_settings().model_copy(update={"intraday": CFG})
    spy = Spy()
    for minute in (0, 1, 2):
        at = datetime.fromisoformat(f"2026-11-17T06:0{minute}:00+00:00")
        IntradayWatcher(s, journal, spy, clock=lambda at=at: at, price=lambda _s: 1.16,
                        ranges=RANGES).report_downtime()
    assert len(spy.sent) == 1, f"announced {len(spy.sent)} times"


def test_two_processes_seeing_the_same_outage_announce_it_once(journal):
    """The reason `downtime_reported` exists, and the case the heartbeat cannot cover.

    The watcher runs in two places -- its own minute-by-minute agent and the engine's fifteen-minute
    pass -- so both can read the same stale `last_pass` before either writes a new one. The heartbeat
    then does not separate them and only the dedupe key does. Simulated by putting `last_pass` back to
    what the second process would still have been holding.

    Verified by deleting the dedupe branch: without it this announces twice.
    """
    from trader.engine.intraday_watch import LAST_PASS_KEY

    stale = "2026-11-13T06:00:00+00:00"
    journal.kv_set(LAST_PASS_KEY, stale)
    s = load_settings().model_copy(update={"intraday": CFG})
    spy = Spy()

    def pass_at(at: str):
        when = datetime.fromisoformat(at)
        IntradayWatcher(s, journal, spy, clock=lambda: when, price=lambda _s: 1.16,
                        ranges=RANGES).report_downtime()

    pass_at("2026-11-17T06:00:00+00:00")
    assert len(spy.sent) == 1
    journal.kv_set(LAST_PASS_KEY, stale)         # the other process had already read the old value
    pass_at("2026-11-17T06:07:00+00:00")
    assert len(spy.sent) == 1, "the same outage must not be announced by both processes"


def test_a_short_gap_is_not_an_outage(journal):
    """A pass can be a few minutes late without anything being wrong."""
    from trader.engine.intraday_watch import LAST_PASS_KEY

    journal.kv_set(LAST_PASS_KEY, "2026-10-07T07:00:00+00:00")
    w = _watcher(journal, "08:30", 1.16420)
    assert w.report_downtime() == [] and w.notifier.sent == []


def test_the_first_ever_pass_does_not_claim_an_outage(journal):
    w = _watcher(journal, "07:05", 1.16420)
    assert w.report_downtime() == [] and w.notifier.sent == []


# --- the ticket has to match the app it gets typed into ----------------------------------------------

def test_the_ticket_tells_you_to_change_the_order_type(journal):
    """MT4 opens the trade screen on Market Execution. A ticket filled in without changing that buys
    at the CURRENT rate instead of resting at the trigger -- a different trade from the tested one,
    taken at the worst possible moment, and invisible because everything else on the ticket is right.
    """
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    msg = w.notifier.sent[0]
    assert "Market Execution to BUY LIMIT" in msg
    assert "In MT4: Quotes → EURUSD → Trade" in msg, "name the path, not just the order type"
    assert "Fill Policy" not in msg, "that is a cTrader field; MT4 has no such box"


def test_the_expiry_is_given_in_local_time_not_only_utc(journal):
    """The close is 16:00 UTC, which is 3am in Sydney. Asking someone to cancel an order at 3am is
    asking them not to follow the rule; MT4 can expire it for them, so the message gives the time in
    the zone they live in and lets the broker do the work."""
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    msg = w.notifier.sent[0]
    assert "16:00 UTC" in msg, "keep UTC for the record"
    assert "Expiry" in msg
    local = next(line for line in msg.splitlines() if line.strip().startswith("Expiry"))
    assert "UTC)" in local and local.count(":") >= 2, f"needs a local time beside the UTC one: {local}"


def test_the_ticket_still_carries_what_the_evidence_actually_says(journal):
    """+4.0%/yr, not the +17.2% a broken backtest deadline produced. A ticket that hides how thin the
    edge is would be the most expensive line in this file."""
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    assert "+4.0%/yr out of sample over 34 trades" in w.notifier.sent[0]
    assert "Not advice" in w.notifier.sent[0]


# --- what you actually did, as reported over Telegram -------------------------------------------------

def test_no_report_is_distinct_from_skipped(journal):
    """Three states, not two: a signal nobody has answered yet must not read as one that was
    deliberately passed on."""
    w = _watcher(journal, "07:05", 1.16420)
    assert w.report(DAY, "EUR/USD") is None
    w.record_skipped(DAY, "EUR/USD")
    assert w.report(DAY, "EUR/USD") == ("skipped", None)


def test_a_taken_report_keeps_the_price(journal):
    w = _watcher(journal, "07:05", 1.16420)
    w.record_taken(DAY, "EUR/USD", 1.16377)
    assert w.report(DAY, "EUR/USD") == ("taken", 1.16377)


def test_a_later_report_overwrites_an_earlier_one(journal):
    """Someone correcting themselves -- `/skipped` then realising they did take it -- must win, not
    be silently ignored because a row already existed."""
    w = _watcher(journal, "07:05", 1.16420)
    w.record_skipped(DAY, "EUR/USD")
    w.record_taken(DAY, "EUR/USD", 1.16400)
    assert w.report(DAY, "EUR/USD") == ("taken", 1.16400)


def test_open_symbol_is_whichever_pair_got_a_plan_today(journal):
    w = _watcher(journal, "07:05", 1.16420)
    assert w.open_symbol(DAY) is None, "nothing built yet -- nothing to confirm"
    w.run_once()
    assert w.open_symbol(DAY) == "EUR/USD"


def test_open_symbol_ignores_other_days(journal):
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    assert w.open_symbol(date(2026, 10, 6)) is None


# --- the outcome behind "done" is not thrown away -----------------------------------------------------
#
# Every path into "done" wrote the identical string, discarding the one thing the Telegram message sent
# in the moment already knew: whether this was a target hit, a stop-out, a no-setup day, a session-end
# close or a stranded-overnight close. `outcome` is written alongside `state` at each of those sites.

def test_a_target_hit_records_its_own_outcome(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()            # in
    out = _watcher(journal, "11:00", 1.16430)
    out.run_once()
    assert out._outcome(DAY, "EUR/USD") == "target"


def test_a_stop_out_records_its_own_outcome(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()            # in
    out = _watcher(journal, "11:00", 1.15900)
    out.run_once()
    assert out._outcome(DAY, "EUR/USD") == "stop"


def test_a_session_end_close_records_its_own_outcome(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()            # in
    end = _watcher(journal, "16:00", 1.16250)
    end.close_out()
    assert end._outcome(DAY, "EUR/USD") == "session_end"


def test_a_no_setup_day_records_its_own_outcome(journal):
    _watcher(journal, "07:05", 1.11919).run_once()
    quiet = _watcher(journal, "16:05", 1.12100)               # drifted up all day, never near the trigger
    quiet.run_once()
    quiet.close_out()
    assert quiet._outcome(DAY, "EUR/USD") == "no_setup"


def test_a_stranded_overnight_close_records_its_own_outcome(journal):
    yesterday = _watcher(journal, "07:05", 1.11919)
    yesterday.run_once()
    entered = _watcher(journal, "09:00", 1.11500)
    entered.run_once()
    later = _watcher(journal, "07:05", 1.12500)
    later.clock = lambda: NEXT_DAY
    later.sweep_unfinished()
    assert later._outcome(DAY, "EUR/USD") == "stranded"


def test_a_gapped_day_has_no_outcome_because_it_never_reaches_done(journal):
    """`gapped` is its own terminal state, distinct from `done` -- there is nothing for `outcome` to
    say about it that `state` does not already say."""
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    plan = w._stored_plan(DAY, "EUR/USD")
    gap = _watcher(journal, "09:00", plan.stop - 0.0020)
    gap.run_once()
    assert gap._get(DAY, "state", "EUR/USD") == "gapped"
    assert gap._outcome(DAY, "EUR/USD") is None


# --- a gapped day still accepts /taken, exactly as reported, but says what the system itself called it

def test_is_gapped_is_true_only_for_a_gapped_day(journal):
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    plan = w._stored_plan(DAY, "EUR/USD")
    assert not w.is_gapped(DAY, "EUR/USD")

    gap = _watcher(journal, "09:00", plan.stop - 0.0020)
    gap.run_once()
    assert gap.is_gapped(DAY, "EUR/USD")


def test_the_gapped_taken_confirmation_names_the_price_and_the_systems_own_call():
    from trader.engine.intraday_watch import gapped_taken_confirmation

    text = gapped_taken_confirmation("EUR/USD", 1.15800)
    assert "1.158" in text
    assert "do NOT buy" in text
    assert "Recorded" in text
