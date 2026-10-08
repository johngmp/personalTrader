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
from trader.models import AssetClass, format_price, round_price

PIP = 1e-4

# Floors and caps that only ever bite on bad data, chosen by measuring 5,896 rolling windows of 23
# years of real EUR/USD history rather than by taste:
#
#   lowest 20-day ATR ever observed   15.8 pips   ->  4.7 pip stop,  ~211,000 units
#   median                            80.6 pips   -> 24.2 pip stop,  ~ 41,000 units
#
# So an ATR under 10 pips has not happened once in that history. Treating it as a quiet market and
# sizing off it produced a message telling you to trade 3,333,333 units -- $3.7m of EUR/USD on a
# $10,000 account, behind a 0.3 pip stop the spread alone would take out. It is a data fault, and the
# only safe response is to build no plan at all.
MIN_ATR = 10 * PIP
# Risk-based sizing is correct -- 1% of capital divided by the stop distance -- but it says nothing
# about whether the resulting position can be held. 30:1 is the ASIC retail cap for FX majors, so this
# is the most a position could be. At the median ATR the size is 4.6x capital and at the lowest ATR ever
# seen it is 23.7x, so this cap never binds on a real market either.
MAX_LEVERAGE = 30.0
# Hourly bars needed before a UTC day counts as a whole day. Forex runs ~24h on a weekday, so a day with
# fewer than this is a weekend, a holiday or a partial feed -- all of which understate the range.
HOURS_FOR_A_FULL_DAY = 20


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

    @property
    def risk_usd(self) -> float:
        """What the stop actually costs at this size, which is not always 1% of capital.

        The leverage cap can cut the size below what risk-based sizing asked for, and then quoting the
        nominal 1% would overstate the loss. Derived from the two numbers in the message so it cannot
        disagree with them."""
        return (self.trigger - self.stop) * self.qty


def build_plan(cfg: IntradayConfig, session_open: float, daily_ranges: list[float],
               day: date) -> Plan | None:
    """The day's three levels, or None when there is not enough history to size them.

    `daily_ranges` must be EARLIER days only. Letting today's own range set the scale would be choosing
    the entry already knowing how far the day was going to move.
    """
    recent = [r for r in daily_ranges if r > 0][-cfg.atr_days:]
    # The full window, not whatever happened to arrive. A mean of three days is not the statistic the
    # levels were chosen against, and one freak 300-pip day on its own would set a 90-pip stop.
    if len(recent) < cfg.atr_days or session_open <= 0:
        return None
    atr = sum(recent) / len(recent)
    if atr < MIN_ATR:
        return None                                     # see MIN_ATR: bad data, not a quiet market
    # Rounded to the precision the messages are written in, and compared at that precision for the rest
    # of the day. Unrounded, the level you are told is not the level being watched: a target of
    # 1.118943691 prints as "Target 1.11894", and a rate of exactly 1.11894 then fails `rate >= target`
    # by four ten-millionths, so the day you watch it touch your number is the day nothing is sent.
    fx = AssetClass.FOREX
    session_open = round_price(session_open, fx)
    trigger = round_price(session_open - cfg.drop_atr * atr, fx)
    stop = round_price(trigger - cfg.stop_atr * atr, fx)
    target = round_price(session_open - (1 - cfg.target_frac) * (session_open - trigger), fx)
    if not (stop < trigger < target) or stop <= 0:
        return None
    # Sized off the rounded levels, so the dollar risk quoted in the message is the risk those levels
    # actually carry rather than the one an unrounded distance would have had.
    qty = (cfg.capital_usd * cfg.risk_pct / 100) / (trigger - stop)
    qty = min(qty, cfg.capital_usd * MAX_LEVERAGE / trigger)    # see MAX_LEVERAGE
    return Plan(day=day, symbol=cfg.symbol, session_open=session_open, atr=atr,
                trigger=trigger, stop=stop, target=target, qty=qty)


def plan_text(p: Plan, cfg: IntradayConfig) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (
        f"📋 {p.symbol} plan · {p.day:%a %d %b}\n"
        f"London opened at {f(p.session_open)} · typical day {p.atr / PIP:.0f} pips\n"
        f"\n"
        f"BUY if it falls to {f(p.trigger)}\n"
        f"   Stop   {f(p.stop)}  ({p.risk_pips:.0f} pips, ${p.risk_usd:,.0f})\n"
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
            f"tested, so today is skipped.\n"
            f"Usually the Mac was asleep. It can also mean the price history needed to size the levels "
            f"could not be fetched, in which case no plan is the right answer too.")


def buy_text(p: Plan, rate: float) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"🟢 BUY {p.symbol} now — {f(rate)}\n"
            f"Stop {f(p.stop)} · Target {f(p.target)} · {p.qty:,.0f} units\n"
            f"You will pay the ask, a touch above this mid.")


def gapped_text(p: Plan, rate: float) -> str:
    """Both levels crossed before anything looked. See the call site for why this is not a buy."""
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"⏭️ {p.symbol} skipped today — it went straight through\n"
            f"Now {f(rate)}, past both the buy at {f(p.trigger)} and the stop at {f(p.stop)}.\n"
            f"\n"
            f"If you left a resting limit buy at {f(p.trigger)} with its stop, that trade has already "
            f"happened and your stop closed it. Nothing to do.\n"
            f"If you were waiting on this message, do NOT buy now: down here the stop is above the "
            f"entry, which is a different trade from the one that was tested.")


def no_setup_text(p: Plan, cfg: IntradayConfig) -> str:
    """Said at the close on a day that never triggered, so silence never has to be interpreted.

    The setup fires on about a third of days -- measured at 32% over 250 days of tick data, a median of
    4 days apart, with a 35-day quiet spell in the record. So most days there is genuinely nothing, and
    without this message "nothing happened" and "something is broken" look identical from the outside.
    """
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"😐 No {p.symbol} trade today ({p.day:%a %d %b})\n"
            f"It needed to fall to {f(p.trigger)} and never got there, so there was nothing to take.\n"
            f"\n"
            f"This is the normal case: the setup appears on roughly one day in three. Nothing is wrong, "
            f"and no news is the same as this message.")


def stranded_text(p: Plan, entry: float | None = None) -> str:
    """For a day that ended with you holding something and nothing awake to say so."""
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"⚠️ {p.symbol} from {p.day:%a %d %b} was never closed\n"
            f"You were told to buy at {f(p.trigger if entry is None else entry)} and the session ended "
            f"without a sell message, "
            f"because nothing was running at 16:00 UTC — usually this Mac asleep.\n"
            f"\n"
            f"If you are still holding it, close it now. The tested rule is flat at the session end, so "
            f"carrying it overnight is not the strategy any more.\n"
            f"No exit price is quoted here on purpose: the rate at a close this missed is not something "
            f"to invent. Check your broker for what you actually got.")


def sell_text(p: Plan, rate: float, why: str, entry: float | None = None) -> str:
    """`entry` is the rate the BUY message actually quoted, which is at or below the trigger.

    Measuring the result from the trigger instead was wrong by the difference between the two: the buy
    fires on the first observation at or under the trigger, so a 15-minute window that steps past it
    reports a gain smaller than the one you had, and a loss larger. Wrong in your favour is still wrong,
    and it would read as a mistake the first time you compared it with your broker.
    """
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    entry = p.trigger if entry is None else entry
    pips = (rate - entry) / PIP
    return (f"🔴 SELL {p.symbol} now — {f(rate)} ({why})\n"
            f"Bought {f(entry)} · {pips:+.0f} pips · about {pips * PIP * p.qty:+,.0f} USD\n"
            f"At the mid, before the spread you actually paid on the way in.")


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

    def _entry(self, day: date) -> float | None:
        raw = self._get(day, "entry")
        return float(raw) if raw else None

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
        if state in ("done", "missed", "gapped"):
            return []
        rate = self._rate()
        if rate is None:
            return []

        if state == "waiting":
            if rate <= plan.stop:
                # Straight through both levels inside one fifteen-minute window, as a news spike does.
                # Saying "BUY now" here would quote a stop ABOVE the entry and have you sell at a loss
                # on the next run. The tested rule enters with a RESTING LIMIT at the trigger, so it is
                # either already filled and already stopped, or it never happened -- and a fresh entry
                # down here is simply a different trade from the one that was measured.
                self._set(day, "state", "gapped")
                self.notifier.send(gapped_text(plan, rate))
                return [f"intraday: {self.cfg.symbol} gapped past the stop at {rate:.5f}; day skipped"]
            if rate <= plan.trigger:
                self._set(day, "state", "in")
                self._set(day, "entry", f"{rate}")       # what the message quoted, for the sell to measure from
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
        self.notifier.send(sell_text(plan, rate, why, self._entry(day)))
        return [f"intraday SELL {self.cfg.symbol} at {rate:.5f} ({why})"]

    def sweep_unfinished(self) -> list[str]:
        """Days that ended while you were still holding, and were never told to sell.

        `close_out` only works if a run happens after 16:00 UTC, which is 3am in Sydney and exactly when
        this Mac is most likely to be asleep. Slept through, the day keeps state "in" forever: you got a
        buy message and no sell, and the next morning a fresh plan is built straight over the top of it.

        Of everything that can go wrong here that is the worst, because you are the one holding the
        position -- an alert that opens a trade and then goes quiet is worse than one that never fired.
        So it is swept on EVERY run, inside the session or not, weekend or not, and said once.
        """
        if not self.cfg.enabled:
            return []
        today = self.clock().date()
        out: list[str] = []
        for key in self.journal.kv_keys("irv:%:state"):
            if self.journal.kv_get(key) != "in":
                continue
            try:
                day = date.fromisoformat(key.split(":")[1])
            except ValueError:
                continue
            if day >= today:
                continue                                # today is close_out's business, not this
            plan = self._stored_plan(day)
            if plan is None:
                continue
            self._set(day, "state", "done")
            self.notifier.send(stranded_text(plan, self._entry(day)))
            out.append(f"intraday: {self.cfg.symbol} from {day} was left open; told you to check it")
        return out

    def close_out(self) -> list[str]:
        """Called after the session ends: anything still open gets closed at the rate, as tested."""
        if not self.cfg.enabled:
            return []
        now = self.clock()
        day = now.date()
        if now.time() < _parse(self.cfg.close_utc):
            return []
        state = self._get(day, "state") or "waiting"
        if state not in ("in", "waiting"):
            return []
        plan = self._stored_plan(day)
        if plan is None:
            return []

        if state == "waiting":
            # The day is over and the level was never reached. Said once, because you asked why no
            # messages were arriving and the honest answer was "the setup did not happen" -- which the
            # system knew and never told you. Silence should not need interpreting.
            self._set(day, "state", "done")
            self.notifier.send(no_setup_text(plan, self.cfg))
            return [f"intraday: no {self.cfg.symbol} setup today"]

        rate = self._rate()
        if rate is None:
            return []
        self._set(day, "state", "done")
        self.notifier.send(sell_text(plan, rate, "session end", self._entry(day)))
        return [f"intraday SELL {self.cfg.symbol} at {rate:.5f} (session end)"]

    # --- data ---------------------------------------------------------------------------------------
    def _rate(self) -> float | None:
        """The live mid, or None when it is too old to act on.

        The feed stamps each quote with the BAR's own timestamp on purpose, so that "a stale feed must
        look stale to the risk gate" -- and this watcher is the one consumer with no risk gate in front
        of it, because it sends messages instead of orders. It was reading `.price` and discarding `.ts`,
        so a feed that froze or fell back to a daily close could have put a buy on your phone at a rate
        that no longer existed. Same limit the gate uses for forex, from the same config.
        """
        if self._price is not None:
            return self._price(self.cfg.symbol)
        try:
            from trader.data.fx_quotes import YahooFXFeed

            quote = YahooFXFeed().get_quote(self.cfg.symbol, AssetClass.FOREX)
        except Exception:
            return None
        if quote.price <= 0:
            return None
        age = (self.clock() - quote.ts).total_seconds()
        if age > self.settings.risk.guards.stale_forex_seconds:
            self.journal.log(f"intraday: ignoring a {age / 60:.0f} min old {self.cfg.symbol} quote "
                             f"from {quote.ts:%Y-%m-%d %H:%M} UTC", level="warning")
            return None
        return quote.price

    def _daily_ranges(self) -> list[float]:
        """Daily high-low for earlier days, used only as a scale for "how far is far".

        Built from HOURLY bars grouped into whole UTC days, because that is the window the backtest
        measured and the two are not interchangeable. Measured against the Dukascopy 5-minute bars the
        research actually used, over 268 overlapping days:

            hourly, grouped by UTC day     20-day ATR off by a median of 1.2 pips (worst 2.0)
            Yahoo's own daily bars         off by a median of 6.9 pips, mean 8.3, worst 21.0

        Yahoo's FX daily bar understates the range -- its day boundary is not midnight UTC and it misses
        extremes in thin hours -- and it understates it in one direction, so the error does not wash out.
        Eight pips of ATR moves the buy level about 3.5 pips and tightens the stop by about 9%: live would
        have entered earlier and been stopped out more often than the run that produced +17.2%/yr. A
        strategy sized off a different number from the one it was tested on is a different strategy.

        No fallback to the daily bars on failure. Returning them would quietly resume trading the biased
        version; returning nothing means no plan, and the missed-open message then says so.
        """
        if self._ranges is not None:
            return list(self._ranges)
        try:
            import yfinance as yf

            from trader.data.history import YAHOO_TICKERS

            ticker = YAHOO_TICKERS.get(self.cfg.symbol)
            if ticker is None:
                return []
            frame = yf.Ticker(ticker).history(period="2mo", interval="1h")
            if frame.empty:
                return []
            index = frame.index
            index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
            frame = frame.set_index(index.tz_localize(None))
            today = self.clock().date()
            out: list[float] = []
            for day, bars in frame.groupby(frame.index.normalize()):
                # A part-day understates the range, so it is not a day. Weekends come back short and
                # drop out here rather than averaging a quiet Sunday into the scale.
                if day.date() >= today or len(bars) < HOURS_FOR_A_FULL_DAY:
                    continue
                span = float(bars["High"].max() - bars["Low"].min())
                if span > 0:
                    out.append(span)
            return out
        except Exception:
            return []
