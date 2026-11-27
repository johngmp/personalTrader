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

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from trader.config import IntradayConfig
from trader.models import AssetClass, format_price, round_price

PIP = 1e-4

# Liveness keys. `last_pass` moves on every executed pass and nothing else; `downtime_reported` holds
# the `last_pass` value an outage was already announced for, so a long gap is reported once rather than
# on every pass for the rest of the day.
LAST_PASS_KEY = "irv:last_pass"
DOWNTIME_KEY = "irv:downtime_reported"
# The watcher runs every minute, so an hour of silence is already far outside normal. Three hours is
# chosen to sit comfortably above a laptop lid closed over lunch while still catching a missed session:
# the London open to close is nine hours, so no whole session can hide under this.
QUIET_GAP_HOURS = 3.0

# What the out-of-sample test actually said about each pair, carried into the message because the two
# are not equally believed and a signal that hides that is misleading. Same parameters for both, chosen
# once on EUR/USD's first 60% of history and applied unchanged.
EVIDENCE = {
    # +4.0%/yr, NOT the +17.2% quoted until 2026-10-08. That figure came from a harness that ignored the
    # 16:00 close this watcher enforces and scanned to the end of the UTC day, so 44% of trades exited up
    # to eight hours later than the live rule allows and carried 61% of the profit. This is the number
    # for the rule actually being run.
    "EUR/USD": "tested +4.0%/yr out of sample over 34 trades — thin, and the only pair that passed",
}

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
# A broker ticket asks for VOLUME IN LOTS, not units of currency, so the message has to speak lots or
# you are left converting in your head at the moment you can least afford to. One standard lot is
# 100,000 units of the base currency, and retail tickets step in hundredths of one.
LOT = 100_000.0
# The usual retail minimum, and only a DEFAULT -- brokers differ, and on a small account this number
# decides every trade, because 1% of the account buys less than one step. Asked to confirm it, the
# answer was "0.01 I think", and the one ticket we have a picture of showed 0.02 in the volume box. So
# it is config, where a wrong guess is one line to fix and is written down as a guess.
MIN_LOT = 0.01
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
    # What the size was worked out from. Carried so the plan can say when the broker's minimum trade
    # is bigger than the risk budget -- see `floored`. Zero means "not stated", and the percentage is
    # then left out rather than invented.
    capital_usd: float = 0.0
    # The smallest volume this broker accepts. See MIN_LOT -- on this account it, not the rule, is
    # what sets the position, so it travels with the plan rather than being assumed at the far end.
    min_lot: float = MIN_LOT

    @property
    def risk_pips(self) -> float:
        return (self.trigger - self.stop) / PIP

    @property
    def reward_pips(self) -> float:
        return (self.target - self.trigger) / PIP

    @property
    def lots(self) -> float:
        """`qty` as the volume box on a broker ticket wants it.

        Rounded DOWN to the 0.01 step a retail ticket accepts, so the number you are told to type is
        never larger than the number that was sized, and floored at the smallest volume a ticket will
        take. Everything quoted in dollars is then measured on THIS, not on `qty`, because this is the
        trade you would actually place.
        """
        step = self.min_lot
        return max(step, math.floor(self.qty / LOT / step) * step)

    @property
    def floored(self) -> bool:
        """True when the broker's smallest trade is LARGER than 1% of capital bought.

        Below roughly $240 this is the normal case on EUR/USD, and it quietly inverts the sizing: the
        rule stops choosing the position and the broker's 0.01 minimum chooses it instead. Risk per
        trade is then whatever that minimum happens to cost at today's stop distance, which on a wide
        day is several times the budget. Config says 1%; the ticket does something else. Nothing
        checked that before, so the message says it now.
        """
        return self.qty / LOT < self.min_lot

    @property
    def risk_pct_actual(self) -> float:
        """What the stop really costs as a share of capital, or 0.0 when capital was not stated."""
        return 100 * self.risk_usd / self.capital_usd if self.capital_usd > 0 else 0.0

    @property
    def sized_qty(self) -> float:
        """The units `lots` really buys. The gap to `qty` is the rounding, and it is never silent."""
        return self.lots * LOT

    @property
    def risk_usd(self) -> float:
        """What the stop actually costs at this size, which is not always 1% of capital.

        The leverage cap can cut the size below what risk-based sizing asked for, and then quoting the
        nominal 1% would overstate the loss. Derived from the lot size the message tells you to type,
        so the dollars and the volume box cannot disagree."""
        return (self.trigger - self.stop) * self.sized_qty


def build_plan(cfg: IntradayConfig, session_open: float, daily_ranges: list[float],
               day: date, symbol: str = "EUR/USD") -> Plan | None:
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
    return Plan(day=day, symbol=symbol, session_open=session_open, atr=atr, trigger=trigger,
                stop=stop, target=target, qty=qty, capital_usd=cfg.capital_usd,
                min_lot=cfg.min_lot)


def _floor_note(p: Plan) -> str:
    """One line when the broker's minimum trade, not the rule, is setting the risk. See `Plan.floored`."""
    if not p.floored:
        return ""
    pct = f", which is {p.risk_pct_actual:.1f}% of your account" if p.capital_usd > 0 else ""
    return (f"NOTE: {p.min_lot:.2f} lots is the smallest trade your broker takes, and it is BIGGER "
            f"than this "
            f"account can risk at 1%. So the minimum is setting the size{pct} -- not the rule. "
            f"A wider day makes that worse, not better.\n")


def _expiry(p: Plan, cfg: IntradayConfig) -> tuple[str, str]:
    """(local time, UTC time) the order must stop being valid, as strings for the ticket.

    The tested rule holds nothing past the close, so an order still resting after it is a trade the
    rule never took. Saying "cancel it at 16:00 UTC" asked the wrong thing of someone in Australia --
    that is 3am, and nobody is cancelling anything at 3am. MT4 can expire the order itself, which is
    the difference between a rule you have to stay up for and one you can sleep through, so the
    message gives the time in the local zone AND in UTC rather than making it an arithmetic problem
    on a phone.
    """
    hh, mm = (int(x) for x in cfg.close_utc.split(":"))
    close = datetime.combine(p.day, time(hh, mm), tzinfo=timezone.utc)
    local = close.astimezone()
    same_day = local.date() == close.date()
    return (f"{local:%H:%M} {local:%Z}" + ("" if same_day else f" {local:%a %d %b}"),
            f"{close:%H:%M} UTC")


def plan_text(p: Plan, cfg: IntradayConfig) -> str:
    """The ticket, written for MetaTrader 4 on an iPhone, which is what it gets typed into.

    Field names and the order of the steps are MT4's, not a generic description of a limit order. The
    third line is the one that matters most and used to be missing: MT4 opens the trade screen on
    "Market Execution", so a ticket filled in without changing that buys at the CURRENT rate instead
    of resting at the trigger -- a different trade from the tested one, entered at the worst moment,
    and the mistake is invisible because the ticket looks right apart from one dropdown.

    Not cTrader: there is no Fill Policy field in MT4, and a message naming boxes that are not on the
    screen teaches you to skim it.
    """
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    local_close, utc_close = _expiry(p, cfg)
    return (
        f"📋 {p.symbol} plan · {p.day:%a %d %b}\n"
        f"London opened at {f(p.session_open)} · typical day {p.atr / PIP:.0f} pips\n"
        f"\n"
        f"In MT4: Quotes → {p.symbol.replace('/', '')} → Trade\n"
        f"Change the type from Market Execution to BUY LIMIT, then:\n"
        f"   Volume       {p.lots:.2f}        ({p.sized_qty:,.0f} units)\n"
        f"   Price        {f(p.trigger)}\n"
        f"   Stop Loss    {f(p.stop)}\n"
        f"   Take Profit  {f(p.target)}\n"
        f"   Expiry       {local_close}   ({utc_close})\n"
        f"then tap PLACE.\n"
        f"\n"
        f"Risks ${p.risk_usd:,.2f} ({p.risk_pips:.0f} pips) to make {p.reward_pips:.0f} pips, if it "
        f"fills at all -- most days it will not.\n"
        f"{_floor_note(p)}"
        f"Set all four and you can put the phone down: the Stop Loss and Take Profit attach to the "
        f"position the moment the order fills, your broker closes it at whichever comes first, and the "
        f"Expiry cancels anything still resting at the close -- the tested rule holds nothing "
        f"overnight. If your MT4 has no Expiry box, cancel it yourself by {local_close}.\n"
        f"Shadow test — {EVIDENCE.get(p.symbol, 'not yet measured on this pair')}. Not advice."
    )


def downtime_text(previous: datetime, now: datetime, gap_h: float, skipped: list[date],
                  cfg: IntradayConfig) -> str:
    """What you missed, said the moment anything runs again."""
    when = f"{gap_h:.0f} hours" if gap_h < 48 else f"{gap_h / 24:.0f} days"
    head = (f"🔌 The forex watcher was not running for {when}\n"
            f"Last pass {previous:%a %d %b %H:%M} UTC, back now at {now:%a %d %b %H:%M} UTC.\n")
    if not skipped:
        return head + ("No London open fell inside the gap, so no plan was missed. Nothing to do.")
    days = ", ".join(f"{d:%a %d %b}" for d in skipped[:6]) + (" …" if len(skipped) > 6 else "")
    return head + (
        f"\n{len(skipped)} London open(s) went by with no plan sent: {days}\n"
        f"Those are gone -- the levels are measured from the {cfg.open_utc} UTC open and cannot be "
        f"reconstructed later, so there is nothing to place for them now.\n"
        f"\nIf you have an MT4 order still resting from before the gap, cancel it: its levels were "
        f"for a day that has ended.")


def missed_text(cfg: IntradayConfig, day: date, minutes_in: float, symbol: str) -> str:
    """Told, rather than left to wonder. See the call site for why this is worth a message."""
    return (f"😴 No {symbol} plan today ({day:%a %d %b})\n"
            f"The {cfg.open_utc} UTC open was missed by {minutes_in:.0f} min, so the levels cannot be "
            f"measured from it.\n"
            f"Taking the setup from a later rate would be trading something other than what was "
            f"tested, so today is skipped.\n"
            f"Usually the Mac was asleep. It can also mean the price history needed to size the levels "
            f"could not be fetched, in which case no plan is the right answer too.")


def buy_text(p: Plan, rate: float) -> str:
    f = lambda v: format_price(v, AssetClass.FOREX)      # noqa: E731
    return (f"🟢 BUY {p.symbol} now — {f(rate)}\n"
            f"In MT4: Quotes → {p.symbol.replace('/', '')} → Trade → Buy by Market, then tap the open "
            f"position and set both levels on it:\n"
            f"   Volume       {p.lots:.2f}        ({p.sized_qty:,.0f} units)\n"
            f"   Stop Loss    {f(p.stop)}\n"
            f"   Take Profit  {f(p.target)}\n"
            f"\n"
            f"Risks ${p.risk_usd:,.2f} ({p.risk_pips:.0f} pips) to make {p.reward_pips:.0f} pips.\n"
            f"{_floor_note(p)}"
            f"Set both before you walk away: this message needed a phone to reach you, and those two "
            f"levels sit at the broker whether the next one arrives or not.\n"
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


def gapped_taken_confirmation(symbol: str, price: float) -> str:
    """Said back by `/taken` instead of the ordinary confirmation, when the day it lands on is one
    `gapped_text` already told you not to trade.

    `/taken` still records exactly what you report -- that never changes, whatever the day's own state
    is. What was missing is that the dashboard and this reply then showed a gapped day's `/taken` in the
    identical words used for a trade the system itself endorsed, so a report typed in confusion, or
    typed because the resting order filled anyway, read as if the watcher had agreed with it.
    """
    return (f"✅ Recorded: you took {symbol} at {price}.\n"
            f"⚠️ But the system's own call that day was do NOT buy — it gapped past both levels in one "
            f"window, which is a different trade from the one that was tested. This records what you "
            f"did; it is not the watcher agreeing with it.")


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
            f"If you left a BUY LIMIT resting at {f(p.trigger)}, cancel it now. It did not fill today "
            f"and tomorrow's levels will be different ones, so an order left there is a trade nobody "
            f"planned.\n"
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
            f"If you are still holding it, close it now, from your positions list rather than with a "
            f"Sell order -- a Sell opens a second trade the other way instead of ending this one. The "
            f"tested rule is flat at the session end, so carrying it overnight is not the strategy any "
            f"more.\n"
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
    level = {"target": "Take Profit", "stop": "Stop Loss"}.get(why)
    if level:
        # The two exits the ticket already carries. Telling you to go and do by hand something the
        # broker has done for you is how a position gets closed twice -- and on a ticket, the second
        # close is a new trade in the opposite direction.
        instruction = (f"Nothing to do if you set the {level}: your broker closed this at that price "
                       f"without you. This message is the record, not an instruction.\n")
    else:
        instruction = ("Close the open position from your positions list -- the X, or Close, on the "
                       "trade itself.\n"
                       "Do NOT use Sell by Market: on a ticket that opens a SECOND trade the other "
                       "way instead of ending this one, and leaves you holding both.\n")
    return (f"🔴 CLOSE {p.symbol} now — {f(rate)} ({why})\n"
            f"{instruction}"
            f"\n"
            f"Bought {f(entry)} · {pips:+.0f} pips · about {pips * PIP * p.sized_qty:+,.0f} USD\n"
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
    def _key(self, day: date, name: str, symbol: str) -> str:
        """One key per day PER PAIR. It used to be one per day, because there was only ever one pair.

        The slash is dropped so the key stays readable: irv:2026-10-08:EURUSD:state.
        """
        return f"irv:{day:%Y-%m-%d}:{symbol.replace('/', '')}:{name}"

    def _claim(self, day: date, symbol: str, expect: str | None, becomes: str) -> bool:
        """Take ownership of the day's next step, or find that someone else already did.

        The watcher runs in two places now: its own minute-by-minute agent and the engine's
        fifteen-minute pass. Both read the state and both decide whether to message you, so a plain
        read-then-write would let two processes see "waiting", both conclude the level was reached, and
        both send the same buy. Every transition that sends a message goes through here, and only the
        process that wins the write does the sending.
        """
        return self.journal.kv_set_if(self._key(day, "state", symbol), expect, becomes)

    def _announce(self, text: str, key: str, wrote: str, was: str | None = None) -> bool:
        """Send a message that a state change has already been claimed for, and undo the claim if it
        does not get through.

        Every transition here writes the state BEFORE sending, and has to: the watcher runs in two
        places, so the write is what makes the decision exclusive. The cost was a silent one. A send
        that failed -- one timed-out request was enough -- left the state saying you were in a trade
        nobody had told you to open, and the next thing you would hear was a SELL for a position you
        did not hold, quoting a profit on money you never put in.

        So a failed send is rolled back, conditionally, and the next pass a minute later tries again
        from the state the market is actually in. Conditionally because the other process may have moved
        on in the meantime: `kv_del_if` only removes the value this caller wrote, and `kv_set_if` only
        restores over it.

        `was` is what the key held before the claim, and getting it wrong is worse than not rolling back
        at all. A SELL claims "in" -> "done"; DELETING that leaves no state, which reads as "waiting",
        and the next pass would see a rate under the trigger and send a BUY for a trade that had already
        closed. So an overwrite is restored and only a fresh claim is deleted.

        What this does NOT fix, stated because it reads like it should: if you have the resting limit
        order the plan recommends, the broker may fill it whether or not any message arrives, and the
        retried message a minute later quotes the rate then rather than the fill. The entry prices this
        watcher records are what the message said, not what your broker did -- it has no broker
        connection at all. Reconciling the two is still yours to do.
        """
        if self.notifier.send(text):
            return True
        undone = (self.journal.kv_del_if(key, wrote) if was is None
                  else self.journal.kv_set_if(key, wrote, was))
        self.journal.log(f"intraday: could not deliver message for {key}; "
                         f"{f'state put back to {was or None}, will retry' if undone else 'state had already moved on'}",
                         level="error")
        return False

    def _get(self, day: date, name: str, symbol: str) -> str | None:
        return self.journal.kv_get(self._key(day, name, symbol))

    def _set(self, day: date, name: str, value: str, symbol: str) -> None:
        self.journal.kv_set(self._key(day, name, symbol), value)

    def _stored_plan(self, day: date, symbol: str) -> Plan | None:
        raw = self._get(day, "plan", symbol)
        if not raw:
            return None
        o, a, t, s, g, q = (float(x) for x in raw.split(","))
        # Capital comes from the config rather than the stored row: it is a setting, not something
        # measured about that day, so reading it back is how it stays current. Without this the BUY
        # message -- which is built from the stored plan, not the fresh one -- loses the warning that
        # the plan it came from carried.
        return Plan(day=day, symbol=symbol, session_open=o, atr=a, trigger=t, stop=s,
                    target=g, qty=q, capital_usd=self.cfg.capital_usd,
                    min_lot=self.cfg.min_lot)

    def _entry(self, day: date, symbol: str) -> float | None:
        raw = self._get(day, "entry", symbol)
        return float(raw) if raw else None

    def _outcome(self, day: date, symbol: str) -> str | None:
        """Which of the five ways a day ends in "done", for a reader who only sees `state`.

        `state` alone collapses a target hit, a stop-out, a session-end close, a no-setup day and a
        stranded-overnight close into the same string, "done" -- even though the Telegram message sent
        in the moment already knew which one it was. Written alongside `state` at every transition into
        "done", additively, so an old row from before this existed simply has none.
        """
        return self._get(day, "outcome", symbol)

    # --- what you actually did, as reported over Telegram -------------------------------------------
    #
    # Everything above this point is the system's own side of the trade: the rate it quoted when it
    # said BUY or SELL. It has no broker connection and never will -- this pair is hand-traded in MT4
    # -- so without this it never learns what you actually got, or whether you traded at all. `/taken
    # <price>` and `/skipped` in the forex chat record that, against the one open signal for the day.
    # Same kv-keyed pattern as `plan`, `state` and `entry`: one more column, not a new mechanism.

    def report(self, day: date, symbol: str) -> tuple[str, float | None] | None:
        """(\"taken\", price) or (\"skipped\", None), or None when nothing has been reported yet.

        Three states, not two -- "no response" has to be distinguishable from "skipped", or a signal
        nobody has answered yet would read on the dashboard as one that was deliberately passed on.
        """
        raw = self._get(day, "report", symbol)
        if raw is None:
            return None
        if raw == "skipped":
            return ("skipped", None)
        _, _, price = raw.partition(":")
        return ("taken", float(price))

    def record_taken(self, day: date, symbol: str, price: float) -> None:
        self._set(day, "report", f"taken:{price}", symbol)

    def record_skipped(self, day: date, symbol: str) -> None:
        self._set(day, "report", "skipped", symbol)

    def open_symbol(self, day: date) -> str | None:
        """The symbol with a signal open today, or None when there is nothing to confirm.

        "Open" means a plan was built -- `/taken` and `/skipped` are answers to "what did you do about
        today's ticket", and a ticket went out the moment the plan did, whatever its state has moved
        on to since.
        """
        for symbol in self.cfg.symbols:
            if self._get(day, "plan", symbol):
                return symbol
        return None

    def is_gapped(self, day: date, symbol: str) -> bool:
        """True when the day's own verdict was `gapped` -- both levels crossed in one window.

        `open_symbol` still answers `/taken` on a day like this exactly as on any other: what you
        report is yours to report, unchanged. This is for the caller that wants to say, alongside the
        recording, that the system's own call that day was not to trade at all. See `gapped_text`.
        """
        return self._get(day, "state", symbol) == "gapped"

    def _store_plan(self, p: Plan) -> None:
        self._set(p.day, "plan", f"{p.session_open},{p.atr},{p.trigger},{p.stop},{p.target},{p.qty}",
                  p.symbol)

    def run_once(self, report=None) -> list[str]:
        """One pass over every pair. Returns what it did, for the run report."""
        if not self.cfg.enabled:
            return []
        now = self.clock()
        opens, closes = _parse(self.cfg.open_utc), _parse(self.cfg.close_utc)
        if now.weekday() >= 5 or not (opens <= now.time() < closes):
            return []                                   # outside the London session, nothing to watch
        out: list[str] = []
        for symbol in self.cfg.symbols:
            out += self._run_symbol(symbol, now)
        return out

    def _run_symbol(self, symbol: str, now: datetime) -> list[str]:
        day = now.date()
        opens = _parse(self.cfg.open_utc)

        plan = self._stored_plan(day, symbol)
        if plan is None:
            # The levels are measured FROM the session open, so the opening rate has to BE the opening
            # rate. If the first run of the day lands at 11:20 -- a restart, an outage, a sleeping
            # laptop -- then taking 11:20's rate as "the open" silently trades a different setup from
            # the one that was tested. Skip the day instead, and say so once.
            minutes_in = (now - datetime.combine(day, opens, tzinfo=timezone.utc)).total_seconds() / 60
            if minutes_in > self.cfg.plan_within_minutes:
                if self._claim(day, symbol, None, "missed"):
                    # Say so on the phone, not just in the run report. A feature whose entire purpose is
                    # to message you cannot fail by going quiet: a silent day is indistinguishable from
                    # a day the setup never appeared, and you would be left wondering which. The usual
                    # cause is this Mac asleep -- caffeinate -s only holds sleep off on AC power, so a
                    # morning on battery skips the open (observed 2026-10-07, 06:24 to 08:18, no runs).
                    if not self._announce(missed_text(self.cfg, day, minutes_in, symbol),
                                          self._key(day, "state", symbol), "missed"):
                        return [f"intraday: missed the open, message not delivered; will retry"]
                    return [f"intraday: missed the {self.cfg.open_utc} open by "
                            f"{minutes_in:.0f} min; no plan today"]
                return []
            rate = self._rate(symbol)
            if rate is None:
                return []
            plan = build_plan(self.cfg, rate, self._daily_ranges(symbol), day, symbol)
            if plan is None:
                return []
            stored = (f"{plan.session_open},{plan.atr},{plan.trigger},"
                      f"{plan.stop},{plan.target},{plan.qty}")
            if not self.journal.kv_set_if(self._key(day, "plan", symbol), None, stored):
                return []                               # another process got there first
            if not self._announce(plan_text(plan, self.cfg), self._key(day, "plan", symbol), stored):
                return [f"intraday plan for {symbol} could not be delivered; will retry"]
            return [f"intraday plan for {symbol}: buy {plan.trigger:.5f}"]

        state = self._get(day, "state", symbol) or "waiting"
        if state in ("done", "missed", "gapped"):
            return []
        rate = self._rate(symbol)
        if rate is None:
            return []

        if state == "waiting":
            if rate <= plan.stop:
                # Straight through both levels inside one fifteen-minute window, as a news spike does.
                # Saying "BUY now" here would quote a stop ABOVE the entry and have you sell at a loss
                # on the next run. The tested rule enters with a RESTING LIMIT at the trigger, so it is
                # either already filled and already stopped, or it never happened -- and a fresh entry
                # down here is simply a different trade from the one that was measured.
                if not self._claim(day, symbol, None, "gapped"):
                    return []
                if not self._announce(gapped_text(plan, rate), self._key(day, "state", symbol), "gapped"):
                    return [f"intraday: {symbol} gapped, message not delivered; will retry"]
                return [f"intraday: {symbol} gapped past the stop at {rate:.5f}; day skipped"]
            if rate <= plan.trigger:
                if not self._claim(day, symbol, None, "in"):
                    return []
                self._set(day, "entry", f"{rate}", symbol)       # what the message quoted, for the sell to measure from
                if not self._announce(buy_text(plan, rate), self._key(day, "state", symbol), "in"):
                    # The entry goes with the claim. Leaving it behind would have the retried BUY --
                    # or a later SELL -- measure profit from a rate that was never sent to anyone.
                    self.journal.kv_del_if(self._key(day, "entry", symbol), f"{rate}")
                    return [f"intraday BUY {symbol} not delivered; will retry"]
                return [f"intraday BUY {symbol} at {rate:.5f}"]
            return []

        # state == "in"
        if rate >= plan.target:
            why = "target"
        elif rate <= plan.stop:
            why = "stop"
        else:
            return []
        if not self._claim(day, symbol, "in", "done"):
            return []
        self._set(day, "outcome", why, symbol)             # see _outcome: "target" or "stop"
        if not self._announce(sell_text(plan, rate, why, self._entry(day, symbol)),
                              self._key(day, "state", symbol), "done", was="in"):
            return [f"intraday SELL {symbol} not delivered; will retry"]
        return [f"intraday SELL {symbol} at {rate:.5f} ({why})"]

    # --- liveness ------------------------------------------------------------------------------------

    def note_pass(self) -> None:
        """Record that a pass actually executed. Written on EVERY pass, before anything can fail.

        The one fact nothing recorded. `launchctl` counts a run when it *attempts* the job, and a quiet
        pass prints nothing, so a stale log and a stale database were the only symptoms of six agents
        that had not executed for four days -- and both look exactly like a market with no setups. This
        key is the difference: it has no network, market or price dependency, so if it is not moving,
        the watcher is not running. Nothing else here can make that distinction.
        """
        self.journal.kv_set(LAST_PASS_KEY, self.clock().isoformat(timespec="seconds"))

    def report_downtime(self) -> list[str]:
        """Say how many London sessions went by while nothing was running.

        `missed` only ever fires from inside a session, so a day when the Mac was asleep at 07:00 UTC
        leaves NO row at all -- and a weekday with no row is invisible. Three quiet days and three days
        of downtime were the same thing seen from the phone.

        This cannot make a dead Mac speak; nothing on the Mac can. What it does is make the silence
        unambiguous the moment anything runs again: you are told what you missed instead of having to
        work out that you missed it. A dead-man's switch off the machine is the only way to be told
        while it is still down, and that is a separate decision because it sends a ping off this
        computer.
        """
        if not self.cfg.enabled:
            return []
        now = self.clock()
        raw = self.journal.kv_get(LAST_PASS_KEY)
        self.note_pass()
        if not raw:
            return []                          # first ever pass: nothing to compare against
        try:
            previous = datetime.fromisoformat(raw)
        except ValueError:
            return []
        gap_h = (now - previous).total_seconds() / 3600
        if gap_h < QUIET_GAP_HOURS:
            return []
        skipped = self._sessions_without_a_plan(previous, now)
        if self.journal.kv_get(DOWNTIME_KEY) == raw:
            return []                          # already reported this outage; do not repeat it hourly
        self.journal.kv_set(DOWNTIME_KEY, raw)
        self.notifier.send(downtime_text(previous, now, gap_h, skipped, self.cfg))
        return [f"intraday: nothing ran for {gap_h:.0f}h; {len(skipped)} session(s) with no plan"]

    def _sessions_without_a_plan(self, previous: datetime, now: datetime) -> list[date]:
        """Weekdays between two passes whose London session opened and left no row behind.

        A row of any kind counts -- plan, missed, gapped -- because the question is whether the system
        was there, not whether it traded. Weekends are excluded because the market is shut, and the
        current day only counts once its open is behind us.
        """
        hh, mm = (int(x) for x in self.cfg.open_utc.split(":"))
        out: list[date] = []
        day = previous.date()
        while day <= now.date():
            opened = datetime.combine(day, time(hh, mm), tzinfo=timezone.utc)
            if day.weekday() < 5 and previous <= opened <= now:
                if not any(self._get(day, name, s) for s in self.cfg.symbols
                           for name in ("plan", "state")):
                    out.append(day)
            day += timedelta(days=1)
        return out

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
        flat = {s.replace("/", ""): s for s in self.cfg.symbols}
        for key in self.journal.kv_keys("irv:%:state"):
            if self.journal.kv_get(key) != "in":
                continue
            parts = key.split(":")
            if len(parts) != 4:
                continue                                # a pre-multi-pair key; nothing left holding it
            try:
                day = date.fromisoformat(parts[1])
            except ValueError:
                continue
            symbol = flat.get(parts[2])
            if symbol is None or day >= today:
                continue                                # today is close_out's business, not this
            plan = self._stored_plan(day, symbol)
            if plan is None:
                continue
            if not self._claim(day, symbol, "in", "done"):
                continue
            self._set(day, "outcome", "stranded", symbol)
            if not self._announce(stranded_text(plan, self._entry(day, symbol)),
                                  self._key(day, "state", symbol), "done", was="in"):
                out.append(f"intraday: {symbol} from {day} left open, message not delivered; will retry")
                continue
            out.append(f"intraday: {symbol} from {day} was left open; told you to check it")
        return out

    def close_out(self) -> list[str]:
        """Called after the session ends: anything still open gets closed at the rate, as tested."""
        if not self.cfg.enabled:
            return []
        now = self.clock()
        if now.time() < _parse(self.cfg.close_utc):
            return []
        out: list[str] = []
        for symbol in self.cfg.symbols:
            out += self._close_symbol(symbol, now)
        return out

    def _close_symbol(self, symbol: str, now: datetime) -> list[str]:
        day = now.date()
        state = self._get(day, "state", symbol) or "waiting"
        if state not in ("in", "waiting"):
            return []
        plan = self._stored_plan(day, symbol)
        if plan is None:
            return []

        if state == "waiting":
            # The day is over and the level was never reached. Said once, because you asked why no
            # messages were arriving and the honest answer was "the setup did not happen" -- which the
            # system knew and never told you. Silence should not need interpreting.
            if not self._claim(day, symbol, None, "done"):
                return []
            self._set(day, "outcome", "no_setup", symbol)
            if not self._announce(no_setup_text(plan, self.cfg), self._key(day, "state", symbol), "done"):
                return [f"intraday: no {symbol} setup message not delivered; will retry"]
            return [f"intraday: no {symbol} setup today"]

        rate = self._rate(symbol)
        if rate is None:
            return []
        if not self._claim(day, symbol, "in", "done"):
            return []
        self._set(day, "outcome", "session_end", symbol)
        if not self._announce(sell_text(plan, rate, "session end", self._entry(day, symbol)),
                              self._key(day, "state", symbol), "done", was="in"):
            return [f"intraday SELL {symbol} (session end) not delivered; will retry"]
        return [f"intraday SELL {symbol} at {rate:.5f} (session end)"]

    # --- data ---------------------------------------------------------------------------------------
    def _rate(self, symbol: str) -> float | None:
        """The live mid, or None when it is too old to act on.

        The feed stamps each quote with the BAR's own timestamp on purpose, so that "a stale feed must
        look stale to the risk gate" -- and this watcher is the one consumer with no risk gate in front
        of it, because it sends messages instead of orders. It was reading `.price` and discarding `.ts`,
        so a feed that froze or fell back to a daily close could have put a buy on your phone at a rate
        that no longer existed. Same limit the gate uses for forex, from the same config.
        """
        if self._price is not None:
            return self._price(symbol)
        try:
            from trader.data.fx_quotes import YahooFXFeed

            quote = YahooFXFeed().get_quote(symbol, AssetClass.FOREX)
        except Exception:
            return None
        if quote.price <= 0:
            return None
        age = (self.clock() - quote.ts).total_seconds()
        if age > self.settings.risk.guards.stale_forex_seconds:
            self.journal.log(f"intraday: ignoring a {age / 60:.0f} min old {symbol} quote "
                             f"from {quote.ts:%Y-%m-%d %H:%M} UTC", level="warning")
            return None
        return quote.price

    def _daily_ranges(self, symbol: str) -> list[float]:
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

            ticker = YAHOO_TICKERS.get(symbol)
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
