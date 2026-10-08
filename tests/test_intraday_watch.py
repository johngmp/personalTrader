"""The intraday watcher: the thing that actually texts you a trade.

It sends no orders, so the risk gate never sees it. That makes these tests the only thing standing
between a bug and a message telling you to buy at the wrong price.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from trader.config import IntradayConfig, load_settings
from trader.engine.intraday_watch import IntradayWatcher, build_plan, plan_text
from trader.journal.db import Journal

CFG = IntradayConfig(enabled=True, symbol="EUR/USD", drop_atr=0.5, stop_atr=0.3, target_frac=1.0,
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
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "j.db")
    yield j
    j.close()


def _watcher(journal, at: str, rate: float, cfg=CFG, ranges=None):
    now = datetime.fromisoformat(f"2026-10-07T{at}+00:00")
    s = load_settings().model_copy(update={"intraday": cfg})
    return IntradayWatcher(s, journal, Spy(), clock=lambda: now, price=lambda _s: rate,
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
    # And the watcher only ever hands it earlier days: _daily_ranges filters on `index.date < today`.
    import inspect

    from trader.engine import intraday_watch

    assert "index.date < today" in inspect.getsource(intraday_watch.IntradayWatcher._daily_ranges)


def test_a_plan_goes_out_once_at_the_open_and_is_not_repeated(journal):
    w = _watcher(journal, "07:05", 1.16420)
    assert w.run_once() and len(w.notifier.sent) == 1
    assert "BUY if it falls to" in w.notifier.sent[0]
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
    assert "SELL" in out.notifier.sent[0] and "target" in out.notifier.sent[0]


def test_it_says_sell_at_the_stop(journal):
    _watcher(journal, "07:05", 1.16420).run_once()
    _watcher(journal, "08:15", 1.16100).run_once()
    out = _watcher(journal, "11:00", 1.15900)
    out.run_once()
    assert "SELL" in out.notifier.sent[0] and "stop" in out.notifier.sent[0]


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
    assert "SELL" in end.notifier.sent[0] and "session end" in end.notifier.sent[0]


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
    assert "BUY if it falls to" not in late.notifier.sent[0]

    # And it stays skipped for the rest of that day, however many times the engine runs. The notice is
    # sent once: one message explaining the day, not one every fifteen minutes until midnight.
    for at in ("11:35", "12:00", "14:00"):
        w = _watcher(journal, at, 1.11000)
        assert w.run_once() == [] and w.notifier.sent == []


def test_a_plan_built_inside_the_window_is_still_allowed(journal):
    w = _watcher(journal, "07:25", 1.16420)
    assert w.run_once() and "BUY if it falls to" in w.notifier.sent[0]


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
    plan = w._stored_plan(DAY)
    # Exactly the figures a message prints, not the floats behind them.
    trigger, target = float(f"{plan.trigger:.5f}"), float(f"{plan.target:.5f}")
    _watcher(journal, "09:00", trigger, ranges=ODD_RANGES).run_once()          # in
    out = _watcher(journal, "12:00", target, ranges=ODD_RANGES)
    assert out.run_once(), "the rate reached the stated target and nothing was sent"
    assert "SELL" in out.notifier.sent[0] and "target" in out.notifier.sent[0]


def test_a_setup_that_gapped_past_its_own_stop_is_not_a_buy(journal):
    """Both levels crossed inside one 15-minute window, as a news spike does.

    This used to send "BUY now" quoting a stop ABOVE the entry, so the next run told you to sell at a
    loss. The tested rule enters with a resting limit at the trigger: either that filled and the stop
    closed it, or it never happened. A fresh entry below the stop is a different trade.
    """
    w = _watcher(journal, "07:05", 1.16420)
    w.run_once()
    plan = w._stored_plan(DAY)

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
    plan = w._stored_plan(DAY)
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
    assert p.risk_usd == pytest.approx((p.trigger - p.stop) * p.qty)
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

    assert watcher(limit + 60)._rate() is None, "a quote past the forex staleness limit must not be used"
    assert watcher(60)._rate() == pytest.approx(1.11000), "a fresh quote must still be used"
