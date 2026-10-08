"""Intraday session-reversion signals, sent to your phone for you to execute by hand.

This is a WATCHER, not a sleeve. It holds no capital, sends no orders and touches no broker. Once a day
it works out three levels from the session open, tells you what they are, and then tells you when the
rate reaches one. You place the trades.

The shape of the day:

    07:00 UTC   London opens. The session open and the recent daily range give a buy level, a stop and
                a target, and the plan goes out. Set a limit order and a stop and you are done.
    all day     Each engine run checks the live rate against those levels and messages you if one is
                reached: buy, then target or stop.
    16:00 UTC   Anything still open is closed, whatever the rate.

Two honest limits on this, both worth knowing before you follow it.

The engine runs every fifteen minutes, so a level touched and left inside that window is seen late or
not at all. That is why the plan goes out at the open with the levels in it: a resting limit order fills
at your price whether or not anything is watching. The "buy now" message is a prompt, not the mechanism.

And the live rate comes from a free feed quoting the MID, while you will trade at the ask. The backtest
charged the real spread from tick data; the alert cannot know the live one. On EUR/USD that gap is about
0.3 pips, which is small against a 30-pip target but not zero, and it always works against you.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from trader.config import IntradayConfig
from trader.models import AssetClass, format_price

PIP = 1e-4


def _parse(hhmm: str) -> time:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return time(hour, minute)


@dataclass
class Plan:
    day: date
    symbol: str
    session_open: float
    atr: float
    trigger: float
    stop: float
    target: float
    qty: float

    @property
    def risk_pips(self) -> float:
        return (self.trigger - self.stop) / PIP

    @property
    def reward_pips(self) -> float:
        return (self.target - self.trigger) / PIP


def build_plan(cfg: IntradayConfig, session_open: float, daily_ranges: list[float],
               day: date) -> Plan | None:
    """The day's three levels, or None when there is not enough history to size them.

    `daily_ranges` must be EARLIER days only. Letting today's own range set the scale would be choosing
    the entry already knowing how far the day was going to move.
    """
    recent = [r for r in daily_ranges if r > 0][-cfg.atr_days:]
    if not recent or session_open <= 0:
        return None
    atr = sum(recent) / len(recent)
    trigger = session_open - cfg.drop_atr * atr
    stop = trigger - cfg.stop_atr * atr
    target = session_open - (1 - cfg.target_frac) * (session_open - trigger)
    if not (stop < trigger < target) or stop <= 0:
        return None
    qty = (cfg.capital_usd * cfg.risk_pct / 100) / (trigger - stop)
    return Plan(day=day, symbol=cfg.symbol, session_open=session_open, atr=atr,
                trigger=trigger, stop=stop, target=target, qty=qty)


def plan_text(p: Plan, cfg: IntradayConfig) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (
        f"📋 {p.symbol} plan · {p.day:%a %d %b}\n"
        f"London opened at {f(p.session_open)} · typical day {p.atr / PIP:.0f} pips\n"
        f"\n"
        f"BUY if it falls to {f(p.trigger)}\n"
        f"   Stop   {f(p.stop)}  ({p.risk_pips:.0f} pips, ${cfg.capital_usd * cfg.risk_pct / 100:,.0f})\n"
        f"   Target {f(p.target)}  ({p.reward_pips:.0f} pips)\n"
        f"   Size   {p.qty:,.0f} units\n"
        f"\n"
        f"A resting limit buy at {f(p.trigger)} with a stop at {f(p.stop)} does this without you "
        f"watching. Flat by {cfg.close_utc} UTC either way.\n"
        f"Shadow test, EUR/USD only. Not advice."
    )


def missed_text(cfg: IntradayConfig, day: date, minutes_in: float) -> str:
    """Told, rather than left to wonder. See the call site for why this is worth a message."""
    return (f"😴 No {cfg.symbol} plan today ({day:%a %d %b})\n"
            f"The {cfg.open_utc} UTC open was missed by {minutes_in:.0f} min, so the levels cannot be "
            f"measured from it.\n"
            f"Taking the setup from a later rate would be trading something other than what was "
            f"tested, so today is skipped. Usually this means the Mac was asleep.")


def buy_text(p: Plan, rate: float) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"🟢 BUY {p.symbol} now — {f(rate)}\n"
            f"Stop {f(p.stop)} · Target {f(p.target)} · {p.qty:,.0f} units\n"
            f"You will pay the ask, a touch above this mid.")


def sell_text(p: Plan, rate: float, why: str) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    pips = (rate - p.trigger) / PIP
    return (f"🔴 SELL {p.symbol} now — {f(rate)} ({why})\n"
            f"Bought {f(p.trigger)} · {pips:+.0f} pips · about {pips * PIP * p.qty:+,.0f} USD")


class IntradayWatcher:
    """Holds the day's plan in the journal and messages you when the rate reaches a level."""

    def __init__(self, settings, journal, notifier, clock=None, price=None, ranges=None) -> None:
        self.cfg: IntradayConfig = settings.intraday
        self.settings, self.journal, self.notifier = settings, journal, notifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Both injectable so a test can pin the levels. Without this the daily ranges came from a live
        # Yahoo call, and a test asserting an exact stop price silently depended on the real market.
        self._price = price
        self._ranges = ranges

    # --- journal-backed state, so a restart mid-session does not re-alert ---------------------------
    def _key(self, day: date, name: str) -> str:
        return f"irv:{day:%Y-%m-%d}:{name}"

    def _get(self, day: date, name: str) -> str | None:
        return self.journal.kv_get(self._key(day, name))

    def _set(self, day: date, name: str, value: str) -> None:
        self.journal.kv_set(self._key(day, name), value)

    def _stored_plan(self, day: date) -> Plan | None:
        raw = self._get(day, "plan")
        if not raw:
            return None
        o, a, t, s, g, q = (float(x) for x in raw.split(","))
        return Plan(day=day, symbol=self.cfg.symbol, session_open=o, atr=a, trigger=t, stop=s,
                    target=g, qty=q)

    def _store_plan(self, p: Plan) -> None:
        self._set(p.day, "plan", f"{p.session_open},{p.atr},{p.trigger},{p.stop},{p.target},{p.qty}")

    def run_once(self, report=None) -> list[str]:
        """One pass. Returns what it did, for the run report."""
        if not self.cfg.enabled:
            return []
        now = self.clock()
        day = now.date()
        opens, closes = _parse(self.cfg.open_utc), _parse(self.cfg.close_utc)
        if now.weekday() >= 5 or not (opens <= now.time() < closes):
            return []                                   # outside the London session, nothing to watch

        plan = self._stored_plan(day)
        if plan is None:
            # The levels are measured FROM the session open, so the opening rate has to BE the opening
            # rate. If the first run of the day lands at 11:20 -- a restart, an outage, a sleeping
            # laptop -- then taking 11:20's rate as "the open" silently trades a different setup from
            # the one that was tested. Skip the day instead, and say so once.
            minutes_in = (now - datetime.combine(day, opens, tzinfo=timezone.utc)).total_seconds() / 60
            if minutes_in > self.cfg.plan_within_minutes:
                if self._get(day, "state") != "missed":
                    self._set(day, "state", "missed")
                    # Say so on the phone, not just in the run report. A feature whose entire purpose is
                    # to message you cannot fail by going quiet: a silent day is indistinguishable from
                    # a day the setup never appeared, and you would be left wondering which. The usual
                    # cause is this Mac asleep -- caffeinate -s only holds sleep off on AC power, so a
                    # morning on battery skips the open (observed 2026-10-07, 06:24 to 08:18, no runs).
                    self.notifier.send(missed_text(self.cfg, day, minutes_in))
                    return [f"intraday: missed the {self.cfg.open_utc} open by "
                            f"{minutes_in:.0f} min; no plan today"]
                return []
            rate = self._rate()
            if rate is None:
                return []
            plan = build_plan(self.cfg, rate, self._daily_ranges(), day)
            if plan is None:
                return []
            self._store_plan(plan)
            self.notifier.send(plan_text(plan, self.cfg))
            return [f"intraday plan for {self.cfg.symbol}: buy {plan.trigger:.5f}"]

        state = self._get(day, "state") or "waiting"
        if state in ("done", "missed"):
            return []
        rate = self._rate()
        if rate is None:
            return []

        if state == "waiting":
            if rate <= plan.trigger:
                self._set(day, "state", "in")
                self.notifier.send(buy_text(plan, rate))
                return [f"intraday BUY {self.cfg.symbol} at {rate:.5f}"]
            return []

        # state == "in"
        if rate >= plan.target:
            why = "target"
        elif rate <= plan.stop:
            why = "stop"
        else:
            return []
        self._set(day, "state", "done")
        self.notifier.send(sell_text(plan, rate, why))
        return [f"intraday SELL {self.cfg.symbol} at {rate:.5f} ({why})"]

    def close_out(self) -> list[str]:
        """Called after the session ends: anything still open gets closed at the rate, as tested."""
        if not self.cfg.enabled:
            return []
        now = self.clock()
        day = now.date()
        if now.time() < _parse(self.cfg.close_utc):
            return []
        if (self._get(day, "state") or "waiting") != "in":
            return []
        plan = self._stored_plan(day)
        rate = self._rate()
        if plan is None or rate is None:
            return []
        self._set(day, "state", "done")
        self.notifier.send(sell_text(plan, rate, "session end"))
        return [f"intraday SELL {self.cfg.symbol} at {rate:.5f} (session end)"]

    # --- data ---------------------------------------------------------------------------------------
    def _rate(self) -> float | None:
        if self._price is not None:
            return self._price(self.cfg.symbol)
        try:
            from trader.data.fx_quotes import YahooFXFeed

            return YahooFXFeed().get_quote(self.cfg.symbol, AssetClass.FOREX).price
        except Exception:
            return None

    def _daily_ranges(self) -> list[float]:
        if self._ranges is not None:
            return list(self._ranges)
        """Daily high-low for earlier days, used only as a scale.

        Yahoo's daily bars, not the tick-derived ones the backtest used. They differ a little at the
        edges of the day; it moves the levels by a fraction of a pip and keeps this independent of a
        download that only runs by hand.
        """
        try:
            from trader.data.history import load_history

            frame = load_history(self.cfg.symbol).tail(self.cfg.atr_days + 5)
            today = self.clock().date()
            frame = frame[frame.index.date < today]
            return [float(h - low) for h, low in zip(frame["high"], frame["low"])]
        except Exception:
            return []
