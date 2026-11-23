"""The live paper-trading engine. `trader run` calls Engine.run_once() every 15 minutes (via launchd).

Each run:
  1. Telegram commands (/status, /halt, /resume)
  2. pick up fills for open orders
  3. reconcile: journal holdings must match Alpaca's positions, symbol by symbol
  4. keep strategy state (stop, highest close, days held) for every open position
  5. protective stops: every position has a resting stop order at Alpaca; trailing stops move up
  6. decisions: when a new daily bar has finished, run the strategy exactly as in the backtest
  7. record sleeve values, refresh the Obsidian vault and dashboard, send the daily digest

Every order still goes through the risk gate. No AI is involved in any of this.
"""

from __future__ import annotations

import fcntl
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from trader.brokers.base import Broker
from trader.config import Settings, SleeveConfig
from trader.data.live_bars import BarSource
from trader.engine.executor import ExecutionResult, Executor
from trader.journal.db import DUST_QTY, Journal
from trader.models import (
    AssetClass,
    Fill,
    MarketClock,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    format_price,
    round_price,
)
from trader.notify.telegram import Notifier, NullNotifier
from trader.risk.kill_switch import KillSwitch
from trader.risk.sizing import plan_weights
from trader.strategies import LIVE_REGISTRY, Target
from trader.strategies.base import Holding, Strategy

SYDNEY = ZoneInfo("Australia/Sydney")
DIGEST_HOUR = 7                 # daily summary after 7am Sydney time (US markets have closed)
CRYPTO_STOP_LIMIT_GAP = 0.03    # crypto stop-limit floor 3% below the stop; the engine backstops below that
STOP_MOVE_MIN = 0.002           # only move a resting stop when it changes by >0.2%
MIN_ORDER_USD = 1.0
SIZING_MARGIN = 0.98            # size entries 2% under the limits, so small price moves can't breach them
AI_VERDICT_WAIT = timedelta(hours=8)  # analyst-team entries skip if the AI hasn't answered by then
# Shadow sleeves are research and stay off your phone. These are the exception: their whole purpose is to
# reach it, because you execute them by hand and nothing else will move your stop for you.
# Shadow brokers whose signals are yours to place by hand, and so are worth a message.
#
# EMPTY on purpose since 2026-10-08. shadow_fx (S9) was the only member, and S9 runs the FX rules that
# failed Gate 1 at an out-of-sample Sharpe of -0.184: it lost to leaving the money in T-bills. Sending a
# buy, a stop and a dollar figure to a phone for a strategy this system has already concluded does not
# work is a standing invitation to lose money, however carefully the message is labelled -- and labelling
# it was the previous fix, which was the wrong fix. S9 keeps running and keeps accumulating evidence; it
# appears on the dashboard and in the daily summary, marked as simulated. It just no longer interrupts
# you. The forex messages you DO want come from the intraday watcher, which passed a clean test.
#
# Anything added here goes straight to the phone, so add a broker only once its rules have earned it.
HAND_EXECUTED: set[str] = set()

ALERTS_PER_RUN = 5              # one bad run can raise many problems; it may not send many messages


def _alert_shape(msg: str) -> str:
    """The stable shape of a problem, so one problem recurring is one alert and not ninety-six.

    Keeps the first two colon-separated parts. For every message this engine produces that is
    "<what failed>: <exception type>" -- which still tells S8's decisions apart from S9's, and keeps
    "could not place S8 stop for XLK" separate per symbol, while dropping the volatile tail (a host,
    a port, a URL) that made ninety-six identical connection timeouts look like ninety-six problems.

    Measured on this journal: 337 `protective stops failed: ConnectTimeout` events collapse to one,
    and so do 133 `vault failed` and 131 `dashboard failed`.
    """
    return ": ".join(msg.split(": ")[:2])


def worth_sending(journal: Journal, scope: str, day: date, problems: Iterable[str],
                  cap: int = ALERTS_PER_RUN) -> list[str]:
    """Which of this run's problems to actually put on the phone, and what to say instead.

    Two limits, for two different failure shapes. REPETITION: a problem already raised today is
    counted and not re-sent -- a flapping network connection that fails every fifteen minutes is one
    thing you need to know, not ninety-six. The running count is kept in the journal so the dashboard
    and the digest can say how many times, which a stream of identical messages never told you anyway.
    VOLUME: at most `cap` distinct new problems per run, then a single line saying how many more there
    were, so one catastrophic run cannot bury the phone.

    Both counts live in the journal rather than in memory: the engine is a fresh process every fifteen
    minutes, so anything held in memory resets before it can suppress a thing.
    """
    sent: list[str] = []
    held = 0
    for msg in problems:
        key = f"alerted:{scope}:{day.isoformat()}:{_alert_shape(msg)}"
        seen = int(journal.kv_get(key, "0") or 0)
        journal.kv_set(key, str(seen + 1))
        if seen:
            continue                     # said once today; the count is on the dashboard
        if len(sent) >= cap:
            held += 1
            continue
        sent.append(msg)
    if held:
        sent.append(f"and {held} more new problem(s) this run -- see the dashboard")
    return sent


def alert_counts(journal: Journal, days: Iterable[date]) -> list[tuple[str, int]]:
    """Every distinct problem alerted on those days and how many times it happened, worst first.

    This is the other half of rationing the messages. Sending one alert instead of ninety-six is only
    honest if the ninety-six is still readable somewhere -- otherwise the fix for too much silence is
    more silence. Summed across scopes, so a failure hitting both brokers reads as one problem twice.
    """
    prefix = "alerted:"
    wanted = {d.isoformat() for d in days}
    totals: dict[str, int] = {}
    for key in journal.kv_keys(prefix + "%"):
        scope_day_shape = key[len(prefix):].split(":", 2)
        if len(scope_day_shape) == 3 and scope_day_shape[1] in wanted:
            shape = scope_day_shape[2]
            totals[shape] = totals.get(shape, 0) + int(journal.kv_get(key, "0") or 0)
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass
class RunReport:
    started: datetime
    fills: list[Fill] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    mismatched: set[str] = field(default_factory=set)
    # Markets a sleeve wanted but could not afford a single share of. Collected across the run and
    # raised as ONE problem at the end: twenty separate alerts for a broad rally would be noise, and
    # noise is how this gets ignored.
    unaffordable: list[str] = field(default_factory=list)
    # Markets a sleeve wanted but another sleeve's resting stop makes untradeable at this broker.
    wash_blocked: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{len(self.fills)} fill(s)", f"{len(self.actions)} action(s)"]
        if self.problems:
            parts.append(f"{len(self.problems)} problem(s)")
        return ", ".join(parts)


class EngineBusy(RuntimeError):
    pass


class Engine:
    def __init__(self, settings: Settings, broker: Broker, journal: Journal, bars: BarSource,
                 notifier: Notifier | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 executor: Executor | None = None, fill_wait_s: float = 15.0, handle_commands: bool = True,
                 shared_outputs: bool = True) -> None:
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.bars = bars
        self.notifier = notifier or NullNotifier()
        self.clock = clock
        self.kill_switch = KillSwitch(settings.kill_switch_path)
        self.executor = executor or Executor(settings, broker, journal, kill_switch=self.kill_switch, clock=clock)
        self.fill_wait_s = fill_wait_s
        # With several brokers (Alpaca, OANDA), one Coordinator handles Telegram commands and writes the
        # vault/dashboard/digest once; each Engine only ever touches its own broker's sleeves.
        self.handle_commands = handle_commands
        self.shared_outputs = shared_outputs
        self.my_sleeves = {s.id for s in settings.sleeves.values() if s.broker == broker.name}

    # --- entry point ------------------------------------------------------------------------------

    def run_once(self) -> RunReport:
        lock_path = self.settings.root / "data" / "engine.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise EngineBusy("another engine run is still in progress") from exc
            return self._run()

    def _run(self) -> RunReport:
        report = RunReport(started=self.clock())
        # Via the constant, not the literal: the dashboard reads the same name from the same place.
        self.journal.kv_set(Journal.ENGINE_HEARTBEAT,
                            report.started.isoformat(timespec="seconds"))  # shown on dashboard/vault
        self.journal.kv_set(f"last_run:{self.broker.name}", report.started.isoformat(timespec="seconds"))
        steps: list[tuple[str, Callable[[RunReport], None]]] = [
            *([("telegram commands", self._handle_commands)] if self.handle_commands else []),
            ("sync fills", self._sync_fills),
            ("reconcile", self._reconcile),
            ("position state", self._refresh_states),
            ("broker orders", self._reconcile_orders),
            ("protective stops", self._protect),
            ("decisions", self._decide),
            ("unaffordable markets", self._report_unaffordable),
            ("wash-blocked markets", self._report_wash_blocked),
            ("position state", self._refresh_states),  # register positions filled just now...
            ("protective stops", self._protect),       # ...so they get their stop in this same run
            ("sleeve values", self._record_equity),
            *([("AUD rate", self._record_fx_rate), ("vault", self._write_vault),
               ("dashboard", self._write_dashboard), ("daily digest", self._digest)]
              if self.shared_outputs else []),
        ]
        for name, step in steps:
            try:
                step(report)
            except Exception as exc:  # keep going: one failing step must not stop stops/fills handling
                msg = f"{name} failed: {type(exc).__name__}: {exc}"
                report.problems.append(msg)
                self.journal.log(msg, level="error")
        for msg in worth_sending(self.journal, self.broker.name, self.clock().date(), report.problems):
            self.notifier.send(f"⚠️ {self.broker.name}: {msg}")
        self.journal.kv_set("last_run_summary", report.summary())
        self.journal.log(f"run finished: {report.summary()}")
        return report

    # --- steps ------------------------------------------------------------------------------------

    def _report_wash_blocked(self, report: RunReport) -> None:
        """One alert per day naming the markets another sleeve's stop is keeping this one out of.

        Worth saying out loud rather than logging: it means the sleeve is not holding the basket that
        passed Gate 1, and unlike the affordability problem no amount of capital fixes it. It is a
        property of running several strategies through one brokerage account.
        """
        if not report.wash_blocked:
            return
        key = f"washblocked:{self.broker.name}:{self.clock().date()}"
        if self.journal.kv_get(key) is not None:
            self.journal.log(f"still wash-blocked: {', '.join(report.wash_blocked)}", level="warning")
            return
        self.journal.kv_set(key, "; ".join(report.wash_blocked))
        report.problems.append(
            f"{len(report.wash_blocked)} market(s) another sleeve already holds with a stop, so this "
            f"broker refuses the buy as a wash trade: {', '.join(report.wash_blocked)}. "
            f"More capital will not fix this one.")

    def _report_unaffordable(self, report: RunReport) -> None:
        """One alert per run naming every market the sleeve wanted and could not afford a share of.

        Raised once per day per sleeve, not once per run: the engine runs every fifteen minutes and the
        same three symbols would be unaffordable all day, which would train you to ignore it.
        """
        if not report.unaffordable:
            return
        key = f"unaffordable:{self.broker.name}:{self.clock().date()}"
        if self.journal.kv_get(key) is not None:
            self.journal.log(f"still unaffordable: {', '.join(report.unaffordable)}", level="warning")
            return
        self.journal.kv_set(key, "; ".join(report.unaffordable))
        report.problems.append(
            f"{len(report.unaffordable)} market(s) wanted but not affordable at this sleeve's capital: "
            f"{', '.join(report.unaffordable)}. The sleeve is not holding the basket that passed Gate 1.")

    def _handle_commands(self, report: RunReport) -> None:
        report.actions += handle_commands(self.notifier, self.journal, self.kill_switch, [self])

    def _should_notify(self) -> bool:
        """Real brokers always message you. Shadow sleeves stay quiet, except the hand-executed ones.

        Three other places asked this question as `name != "shadow"`, which matches the name EXACTLY and
        so said yes to every shadow broker except the one literally called "shadow". It agreed with this
        method only by coincidence, because shadow_fx happens to be hand-executed; a second shadow
        broker would have been messaged to your phone while this method said to stay quiet.
        """
        return not self.broker.name.startswith("shadow") or self.broker.name in HAND_EXECUTED

    def _is_shadow(self) -> bool:
        return self.broker.name.startswith("shadow")

    def _sync_fills(self, report: RunReport) -> None:
        for fill in self.executor.sync_open_orders():
            report.fills.append(fill)
            if self._should_notify():
                self.notifier.send(_fill_text(fill, asset_class=self.settings.asset_class(fill.symbol),
                                              shadow=self._is_shadow()))

    def _reconcile(self, report: RunReport) -> None:
        """Journal holdings (this broker's sleeves together) must equal the broker's position in each symbol."""
        ours: dict[str, float] = {}
        for (sleeve_id, symbol), qty in self.journal.all_holdings().items():
            if sleeve_id in self.my_sleeves:
                ours[symbol] = ours.get(symbol, 0.0) + qty
        theirs = {p.symbol: p.qty for p in self.broker.get_positions()}
        for symbol in sorted(set(ours) | set(theirs)):
            a, b = ours.get(symbol, 0.0), theirs.get(symbol, 0.0)
            if abs(a - b) > max(DUST_QTY, 1e-6 * max(abs(a), abs(b))):
                report.mismatched.add(symbol)
                msg = f"{symbol}: journal holds {a:g} but {self.broker.name} holds {b:g}; not trading it until fixed"
                key = f"mismatch:{symbol}:{self.clock().date()}"
                if self.journal.kv_get(key) is None:  # alert once per symbol per day
                    self.journal.kv_set(key, "1")
                    report.problems.append(msg)
                else:
                    self.journal.log(msg, level="warning")

    def _broker_stop_holding(self, symbol: str, sleeve_id: str) -> bool:
        """Is one of OUR stops already resting on this symbol at the broker?

        Deliberately not limited to this sleeve: the shares are held per account, not per sleeve, so
        another sleeve's resting stop refuses this one's placement just the same. That is the wash
        trade `_blocking_stop` already guards on the entry side.
        """
        return bool(getattr(self, "_broker_stops", {}).get(symbol))

    def _reconcile_orders(self, report: RunReport) -> None:
        """What orders does the BROKER actually have open? Nothing ever asked until now.

        `_reconcile` compares positions. `broker.get_open_orders()` is implemented on all six adapters
        and was called only from tests, so the engine's belief about its own resting stops came purely
        from `positions_state.stop_order_id` and that row's journal status -- and when those drifted
        from reality nothing noticed.

        They drift because `_cancel_stop` clears the pointer while deliberately NOT marking the order
        cancelled (Alpaca may refuse a cancel, or the stop may fill first). If the cancel is refused,
        the order keeps holding the shares and the engine has forgotten it exists. `_protect` then
        tries to place a replacement, Alpaca refuses it -- "insufficient qty available", with
        `held_for_orders == existing_qty` proving the shares are already reserved -- and the engine
        reports "could not place stop", which the code calls its most alarming message, while the
        position is in fact protected.

        That had fired 3,334 times, 222 of them on 2026-11-11 alone, each one a Telegram message. The
        loudest alarm in the system was a permanent false positive, which is worse than no alarm:
        a genuinely unprotected position would have been indistinguishable from the noise.
        """
        self._broker_stops = {}
        live = self.broker.get_open_orders()
        known = {o.client_order_id: o for o in live}
        for order in live:
            if order.side is Side.SELL and order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                self._broker_stops.setdefault(order.symbol, []).append(order)

        adopted, orphans = [], []
        for state in self._my_states():
            sleeve_id, symbol = state["sleeve_id"], state["symbol"]
            pointer = state["stop_order_id"]
            if pointer and pointer in known:
                # The broker agrees this order is live. Heal a journal row that says otherwise, which
                # is the `pending_cancel` case: we asked to cancel, the broker has not done it.
                row = self.journal.order(pointer)
                if row is not None and not OrderStatus(row["status"]).is_open:
                    self.journal.mark_order(pointer, OrderStatus.SUBMITTED, "still open at the broker")
                continue
            for order in self._broker_stops.get(symbol, []):
                row = self.journal.order(order.client_order_id)
                if row is None or row["sleeve_id"] != sleeve_id:
                    continue
                # A stop this sleeve placed, still resting, that we had lost the pointer to.
                self.journal.record_broker_order(self.broker.name, order)
                self.journal.upsert_position_state(sleeve_id, symbol, stop_order_id=order.client_order_id)
                adopted.append(f"{sleeve_id} {symbol}")
                break

        for order in live:
            if self.journal.order(order.client_order_id) is None:
                orphans.append(f"{order.symbol} ({order.order_type.value}, {order.broker_order_id[:8]})")

        if adopted:
            report.actions.append(f"re-adopted {len(adopted)} resting stop(s) the journal had lost: "
                                  f"{', '.join(sorted(adopted))}")
        if orphans:
            # Once a day: a true orphan is a standing condition, not news every fifteen minutes.
            key = f"orphan-orders:{self.broker.name}:{self.clock().date()}"
            msg = (f"{len(orphans)} order(s) resting at {self.broker.name} that this system has no "
                   f"record of: {', '.join(sorted(orphans)[:8])}. They hold shares, so a replacement "
                   f"stop will be refused until they are cancelled or adopted.")
            if self.journal.kv_get(key) is None:
                self.journal.kv_set(key, str(len(orphans)))
                report.problems.append(msg)
            else:
                self.journal.log(msg, level="warning")

    def _refresh_states(self, report: RunReport) -> None:
        holdings = self.journal.all_holdings()
        live_sleeves = {s.id for s in self._live_sleeves()}
        for (sleeve_id, symbol), qty in holdings.items():
            if sleeve_id in live_sleeves and self.journal.position_state(sleeve_id, symbol) is None and qty > 0:
                self._create_state(sleeve_id, symbol)
        for state in self._my_states():
            if holdings.get((state["sleeve_id"], state["symbol"]), 0.0) <= DUST_QTY:
                self._cancel_stop(state)
                self.journal.delete_position_state(state["sleeve_id"], state["symbol"])
                report.actions.append(f"{state['sleeve_id']} {state['symbol']} position closed")
                if self._should_notify():
                    self.notifier.send(
                        f"👻 {state['sleeve_id']} would have closed its {state['symbol']} position "
                        f"(shadow test — nothing was held)." if self._is_shadow() else
                        f"✅ {state['sleeve_id']} closed its {state['symbol']} position.")

    def _protect(self, report: RunReport) -> None:
        market = self.broker.get_clock()
        broker_qty = {p.symbol: p.qty for p in self.broker.get_positions()}
        for state in self._my_states():
            sleeve_id, symbol, stop = state["sleeve_id"], state["symbol"], state["stop"]
            if stop is None or symbol in report.mismatched:
                continue
            if self.settings.sleeves[sleeve_id].retiring:
                # It is on its way out and a market sell is already resting. Trying to add a stop here
                # fails the duplicate-order guard -- two sells of the same symbol inside a minute -- and
                # reports "could not place stop", which is this system's most alarming message, fired
                # when nothing is wrong. The exit order IS the protection now.
                continue
            asset_class = self.settings.asset_class(symbol)
            qty = self._round_qty(symbol, min(self.journal.sleeve_holdings(sleeve_id).get(symbol, 0.0),
                                              broker_qty.get(symbol, 0.0)))
            if qty <= 0:
                continue
            quote = self.broker.get_quote(symbol, asset_class)  # type: ignore[arg-type]
            can_trade_now = asset_class is AssetClass.CRYPTO or market.is_open
            if quote.price <= stop and can_trade_now:
                # Price is already through the stop (e.g. a crypto gap below the stop-limit floor): sell now.
                self._exit(sleeve_id, symbol, "stop hit (engine backup)", report)
                continue
            order_row = self.journal.order(state["stop_order_id"]) if state["stop_order_id"] else None
            resting = order_row is not None and OrderStatus(order_row["status"]).is_open
            if resting and abs((order_row["qty"] or 0) - qty) > DUST_QTY:
                self._cancel_stop(state)
                resting = False
            if not resting and self._broker_stop_holding(symbol, sleeve_id):
                # Something of ours is already resting on this symbol at the broker and holding the
                # shares. Placing another would be refused -- "insufficient qty available" -- and
                # reported as a failure to protect a position that IS protected. See
                # _reconcile_orders for how the pointer to it gets lost.
                continue
            if not resting:
                self._place_stop(sleeve_id, symbol, qty, stop, asset_class, report)  # type: ignore[arg-type]
            elif stop > (order_row["stop_price"] or 0) * (1 + STOP_MOVE_MIN):
                if asset_class is AssetClass.CRYPTO or market.is_open:
                    try:  # one symbol's stop-move problem must not stop protecting the others
                        self._move_stop(order_row, stop, asset_class, report)  # type: ignore[arg-type]
                    except Exception as exc:
                        msg = f"{sleeve_id} {symbol} stop move failed: {type(exc).__name__}: {exc}"
                        report.problems.append(msg)
                        self.journal.log(msg, level="error")
                # else: the market is closed; the broker would only queue the change until the open,
                # so move it on the first run after the open instead (same effect, no half-replaced orders).

    def _decide(self, report: RunReport) -> None:
        if self.kill_switch.active:
            report.actions.append("decisions skipped: kill switch is on")
            return
        now = self.clock()
        market = self.broker.get_clock()
        for sleeve in self._live_sleeves():
            try:  # one sleeve's problem (e.g. missing data) must not stop the others deciding
                self._decide_sleeve(sleeve, now, market, report)
            except Exception as exc:
                msg = f"{sleeve.id} decisions failed: {type(exc).__name__}: {exc}"
                report.problems.append(msg)
                self.journal.log(msg, level="error")

    def _record_equity(self, report: RunReport) -> None:
        account = self.broker.get_account()  # account history for the dashboard and the drawdown limit
        self.journal.snapshot_equity(self.broker.name, account.equity, account.cash)
        for sleeve in self._live_sleeves():
            equity, invested = self.sleeve_value(sleeve)
            adjusted = equity - self.journal.sleeve_haircut(sleeve.id)
            self.journal.record_sleeve_equity(sleeve.id, equity, adjusted, invested)

    def _record_fx_rate(self, report: RunReport) -> None:
        """Store AUD per USD so the dashboard and menu bar never make a network call to render.

        Swallows its own failures deliberately. Every other step reports through `report.problems`, which
        also sends a Telegram warning -- the right severity for "could not place a protective stop" and
        badly wrong for "could not fetch a display currency". The AUD column simply does not appear when
        the rate is missing or stale, which is already handled, so a failure here costs nothing. Letting
        it escalate would also make a quiet network blip look like a trading fault.
        """
        from trader.money import fetch_rate, store_rate

        try:
            store_rate(self.journal, fetch_rate(self.clock()))
        except Exception as exc:
            self.journal.log(f"AUD rate unavailable: {type(exc).__name__}: {exc}", level="info")

    def _write_vault(self, report: RunReport) -> None:
        from trader.journal.vault import write_vault

        write_vault(self.settings, self.journal, [self])

    def _write_dashboard(self, report: RunReport) -> None:
        from trader.engine.dashboard import write_dashboard

        write_dashboard(self.settings, self.journal, [self])

    def _digest(self, report: RunReport) -> None:
        send_digest(self.notifier, self.journal, [self], self.clock())

    # --- decisions ----------------------------------------------------------------------------------

    def _live_sleeves(self) -> list[SleeveConfig]:
        return [s for s in self.settings.sleeves.values()
                if s.enabled and s.broker == self.broker.name and (s.strategy or s.id) in LIVE_REGISTRY]

    def _strategy(self, sleeve: SleeveConfig) -> Strategy:
        cls = LIVE_REGISTRY[sleeve.strategy or sleeve.id]
        return cls(params={**cls.default_params, **sleeve.params}, symbols=list(sleeve.symbols))

    def _retirement_done(self, sleeve: SleeveConfig, report: RunReport) -> bool:
        """True once a retiring sleeve is flat with nothing left in flight.

        From here it is finished: it holds nothing and, because `retiring` makes it want nothing, it
        cannot enter again. Processing it further would only re-ask the same question every fifteen
        minutes. The config flag is left to you -- an engine that rewrites its own configuration
        unattended is a surprise nobody wants at 3am -- but the behaviour is already that of a disabled
        sleeve, so there is no hurry and no exposure while it waits.
        """
        if not sleeve.retiring:
            return False
        if self.journal.sleeve_holdings(sleeve.id):
            return False
        if any(r["sleeve_id"] == sleeve.id for r in self.journal.open_orders(self.broker.name)):
            return False                                  # a sell still queued for the open
        key = f"retired:{sleeve.id}"
        if self.journal.kv_get(key) is None:
            self.journal.kv_set(key, self.clock().isoformat(timespec="seconds"))
            report.actions.append(f"{sleeve.id} retirement complete: flat, nothing in flight")
            if self._should_notify():
                sold = "would have sold" if self._is_shadow() else "sold"
                self.notifier.send(
                    f"🏁 {sleeve.id} is retired. It {sold} the last of its positions and will not trade "
                    f"again.\nIt behaves as disabled from now on; set enabled: false in sleeves.yaml "
                    f"when convenient.")
        return True

    def _decide_sleeve(self, sleeve: SleeveConfig, now: datetime, market: MarketClock, report: RunReport) -> None:
        if self._retirement_done(sleeve, report):
            return
        strategy = self._strategy(sleeve)
        bars = {s: self.bars.completed_bars(s, self.settings.asset_class(s), now, market)  # type: ignore[arg-type]
                for s in sleeve.symbols}
        if any(df.empty for df in bars.values()):
            missing = [s for s, df in bars.items() if df.empty]
            report.problems.append(f"{sleeve.id}: no price history for {missing}")
            return
        strategy.setup(bars)
        from trader.signals import store as signals

        new_weights = None
        if sleeve.ai == "investor-panel":
            new_weights = signals.latest_weights(self.journal, sleeve.id, now)
            if new_weights is None:
                return  # nothing to do until the weekly investor panel submits new weights
            strategy.weights = {w.symbol: w.weight for w in new_weights[1].weights}  # type: ignore[attr-defined]

        due: dict[pd.Timestamp, list[str]] = {}
        for symbol in sleeve.symbols:
            if symbol in report.mismatched:
                continue
            latest = bars[symbol].index[-1]
            last = self.journal.last_decided_bar(sleeve.id, symbol)
            already_decided = last is not None and latest <= pd.Timestamp(last)
            # A retiring sleeve ignores that. Deciding once per daily bar is right for a strategy, and
            # wrong for a wind-down: having already decided today would otherwise leave the positions
            # open until tomorrow's bar, when the whole point is to be out of them.
            if already_decided and new_weights is None and not sleeve.retiring:
                continue
            self._advance_state(sleeve.id, symbol, bars[symbol], last)
            due.setdefault(latest, []).append(symbol)
        if not due:
            return

        limits = self.settings.sleeve_limits(sleeve.id)
        # Decide on the finished daily bar (as in the backtest), but SIZE at the live price, the same price
        # the risk gate checks a moment later. Sizing on yesterday's close could break the 1% risk rule
        # after an overnight move.
        live = {s: self.broker.get_quote(s, self.settings.asset_class(s)).price for s in sleeve.symbols}  # type: ignore[arg-type]
        self._price_what_it_still_holds(sleeve, self.journal.sleeve_holdings(sleeve.id), live, report)
        for date in sorted(due):
            symbols = due[date]
            equity, _ = self.sleeve_value(sleeve)
            held = self.journal.sleeve_holdings(sleeve.id)
            holdings = self._holdings(sleeve.id, held)
            if sleeve.retiring:
                # Wind-down: want nothing. Going through the ordinary exit path means stops are
                # cancelled, fills are journalled and the P&L lands in the record like any other close.
                targets = {s: Target(weight=0.0, reason="sleeve retiring") for s in held if s in symbols}
            else:
                targets = {s: t for s, t in strategy.on_bar(date, holdings).items() if s in symbols}
            for symbol, target in targets.items():  # trailing stops only ever move up
                state = self.journal.position_state(sleeve.id, symbol)
                if state is not None and target.weight > 0 and target.stop is not None:
                    new_stop = max(state["stop"] or 0.0, target.stop)
                    if new_stop != state["stop"]:
                        old_stop = state["stop"]
                        self.journal.upsert_position_state(sleeve.id, symbol, stop=new_stop)
                        # Only sleeves you execute by hand need telling: everywhere else the stop is a
                        # real resting order at the broker and moves without you.
                        if self.broker.name in HAND_EXECUTED and (not old_stop
                                                                  or new_stop > old_stop * (1 + STOP_MOVE_MIN)):
                            self.notifier.send(_stop_move_text(sleeve.id, symbol, old_stop, new_stop,
                                                               self.settings.asset_class(symbol),
                                                               shadow=self._is_shadow()))
            prices = live
            # `s in prices` because a symbol can be held and unpriceable at once -- see
            # _price_what_it_still_holds, which reports those rather than letting them raise here.
            current = {s: q * prices[s] / equity for s, q in held.items() if equity > 0 and s in prices}
            deferred: set[str] = set()
            for symbol, weight in plan_weights(targets, current, prices, limits).items():
                reason = targets[symbol].reason
                if weight <= 0:
                    self._exit(sleeve.id, symbol, reason, report)
                elif symbol not in held:
                    if sleeve.ai == "analyst-team":
                        reason = self._ai_gate(sleeve.id, symbol, date, targets[symbol], strategy, bars, now,
                                               report, deferred)
                        if reason is None:
                            continue
                    dollars = weight * equity * SIZING_MARGIN
                    self._enter(sleeve.id, symbol, dollars, prices[symbol], targets[symbol].stop, reason, report)
                else:
                    self._resize(sleeve.id, symbol, weight * equity / prices[symbol] - held[symbol], prices[symbol],
                                 reason, report)
            for symbol in symbols:
                if symbol not in deferred:  # candidates waiting for an AI verdict are re-checked next run
                    self.journal.set_decided_bar(sleeve.id, symbol, str(date.date()))
            report.actions.append(f"{sleeve.id} decided on {date.date()} bar for {', '.join(symbols)}"
                                  + (f" (waiting for AI on {', '.join(sorted(deferred))})" if deferred else ""))
        if new_weights is not None:
            signals.mark_applied(self.journal, new_weights[0])
            report.actions.append(f"{sleeve.id} applied investor-panel weights {new_weights[0]}")

    def _ai_gate(self, sleeve_id: str, symbol: str, date: pd.Timestamp, target, strategy: Strategy,
                 bars: dict[str, pd.DataFrame], now: datetime, report: RunReport, deferred: set[str]) -> str | None:
        """Entries in analyst-team sleeves need an AI 'approve'. Returns the reason to trade with, or None."""
        from trader.signals import store as signals

        bar = str(date.date())
        verdict = signals.verdict_for(self.journal, sleeve_id, symbol, bar)
        if verdict is None:
            age = signals.request_age(self.journal, sleeve_id, symbol, bar, now)
            if age is None:
                row = strategy.row(symbol, date) or {}
                closes = bars[symbol]["close"]
                context = {"rule_reason": target.reason, "close": float(closes.iloc[-1]), "stop": target.stop,
                           "indicators": {k: (None if pd.isna(v) else round(float(v), 6)) for k, v in row.items()},
                           "return_20d": float(closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) > 21 else None,
                           "return_60d": float(closes.iloc[-1] / closes.iloc[-61] - 1) if len(closes) > 61 else None}
                signals.request_verdict(self.journal, sleeve_id, symbol, bar, context, now)
                report.actions.append(f"{sleeve_id} asked the AI analyst team about {symbol}")
                deferred.add(symbol)
            elif age > AI_VERDICT_WAIT:
                signals.expire_request(self.journal, sleeve_id, symbol, bar)
                report.actions.append(f"{sleeve_id} skipped {symbol}: no AI verdict within {AI_VERDICT_WAIT}")
            else:
                deferred.add(symbol)
            return None
        if verdict.verdict == "veto":
            report.actions.append(f"{sleeve_id} AI vetoed {symbol} ({verdict.confidence:.0%}): {verdict.reasoning[:120]}")
            self.journal.log(f"{sleeve_id} AI veto {symbol} {bar}: {verdict.reasoning[:300]}")
            return None
        return f"{target.reason}; AI approved ({verdict.confidence:.0%})"

    def _advance_state(self, sleeve_id: str, symbol: str, bars: pd.DataFrame, last_decided: str | None) -> None:
        """Count the new bars since the last decision for an open position (days held, highest close)."""
        state = self.journal.position_state(sleeve_id, symbol)
        if state is None:
            return
        since = pd.Timestamp(last_decided) if last_decided else pd.Timestamp(state["entry_date"][:10])
        new = bars["close"][bars.index > since]
        if new.empty:
            return
        self.journal.upsert_position_state(sleeve_id, symbol, bars_held=state["bars_held"] + len(new),
                                           high_since_entry=max(state["high_since_entry"], float(new.max())))

    def _holdings(self, sleeve_id: str, held: dict[str, float]) -> dict[str, Holding]:
        out = {}
        for symbol, qty in held.items():
            state = self.journal.position_state(sleeve_id, symbol)
            if state is None:
                continue
            out[symbol] = Holding(qty=qty, entry_price=state["entry_price"],
                                  entry_date=pd.Timestamp(state["entry_date"][:10]), stop=state["stop"],
                                  high_since_entry=state["high_since_entry"], bars_held=state["bars_held"])
        return out

    # --- orders -------------------------------------------------------------------------------------

    def _blocking_stop(self, sleeve_id: str, symbol: str) -> str | None:
        """The sleeve whose resting sell-stop would make a buy here a wash trade, if there is one.

        The broker sees ONE account; sleeves are our abstraction, not its. Alpaca refuses a buy while any
        opposite-side order rests on the same symbol: "potential wash trade detected". Measured on
        2026-10-08, it refuses a plain limit order too, so the error's advice to "use complex orders"
        means brackets and OCO, not a different order type we can slip in.

        So the conflict cannot be traded through, only avoided -- and cancelling the other sleeve's stop
        to make room is not avoiding it, it is leaving a real position unprotected to place a new one.
        This reports instead. The cost is honest and visible; the alternative was an order the broker was
        always going to reject, logged as a broker error among real ones.
        """
        for state in self.journal.position_states():
            if state["sleeve_id"] == sleeve_id or state["symbol"] != symbol:
                continue
            if state["sleeve_id"] not in self.my_sleeves:
                continue                                    # a different broker, a different account
            order = self.journal.order(state["stop_order_id"]) if state["stop_order_id"] else None
            if order is not None and OrderStatus(order["status"]).is_open:
                return str(state["sleeve_id"])
        return None

    def _enter(self, sleeve_id: str, symbol: str, dollars: float, price: float, stop: float | None, reason: str,
               report: RunReport) -> None:
        blocker = self._blocking_stop(sleeve_id, symbol)
        if blocker is not None:
            report.actions.append(f"{sleeve_id} skipped {symbol}: {blocker} has a resting stop on it")
            report.wash_blocked.append(f"{symbol} (held by {blocker})")
            return
        dollars = self._fit_to_risk(sleeve_id, symbol, dollars, price, stop, report)
        if dollars <= 0:
            return
        asset_class = self.settings.asset_class(symbol)
        step = self.broker.order_step(symbol, asset_class)  # type: ignore[arg-type]
        if step is not None:  # whole shares at Alpaca, whole units at OANDA
            qty = self._round_qty(symbol, dollars / price)
            if qty < step[1]:
                # A problem, not just an action. This is how a sleeve quietly stops being the strategy
                # that was backtested: on 2026-10-08 S8 wanted SPY, QQQ and MDY, each was allocated
                # $544 against share prices of $777/$758/$664, all three floored to zero shares, and
                # nothing was ordered or reported. The backtest held them, because it sized
                # fractionally. Silence there is the worst outcome -- the sleeve looks like it is
                # running the tested basket while missing its core holdings.
                report.actions.append(f"{sleeve_id} skipped {symbol}: ${dollars:,.0f} is below the minimum order size")
                report.unaffordable.append(f"{symbol} (${dollars:,.0f} < 1 share at ${price:,.2f})")
                return
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.BUY, qty=qty, stop_price=stop,
                                 reason=reason)
        else:
            if dollars < MIN_ORDER_USD:
                return
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.BUY, notional=round(dollars, 2),
                                 stop_price=stop, reason=reason)
        self._submit(order, report)

    def _price_what_it_still_holds(self, sleeve: SleeveConfig, held: dict[str, float],
                                   prices: dict[str, float], report: RunReport) -> None:
        """Add a price for anything the sleeve HOLDS but no longer trades.

        Quotes were fetched for `sleeve.symbols` only, while the weights below look up a price for every
        symbol the sleeve HOLDS. Take a symbol out of a sleeve's list in sleeves.yaml while it still
        holds one and the two disagree, which raised KeyError -- and because a sleeve's decisions are
        wrapped so that one sleeve cannot stop the others, that killed the WHOLE sleeve, every run.
        Not just the removed symbol: nothing else in the sleeve got decided either, so a trend that
        ended never sold. Reproduced on 2026-10-08 by removing SPY from S1 while it held 2 shares:
        "S1 decisions failed: KeyError: 'SPY'", and S1 then sat frozen holding all four of its markets.

        The broker's own valuation is used, since an orphan is by definition not in the sleeve's quote
        set and may not even be in the universe any more. Counting it matters: its weight is part of the
        sleeve's exposure, and leaving it out would let the next entry be sized as though the room were
        free.

        The position is NOT liquidated here. Removing a symbol from a list is not the same instruction as
        "sell this now", `retiring` already exists for a wind-down, and the position keeps the protective
        stop it already has. It is reported on every run instead, because a holding no strategy is
        steering should not be quiet.
        """
        orphans = [s for s in held if s not in prices]
        if not orphans:
            return
        for position in self.broker.get_positions():
            if position.symbol in orphans and position.qty:
                prices[position.symbol] = abs(position.market_value / position.qty)
        report.problems.append(
            f"{sleeve.id} still holds {', '.join(sorted(orphans))}, which is no longer in its symbol "
            f"list. The position keeps its stop but no strategy is steering it; set `retiring: true` to "
            f"wind the sleeve down, or put the symbol back.")

    def _fit_to_risk(self, sleeve_id: str, symbol: str, dollars: float, price: float,
                     stop: float | None, report: RunReport) -> float:
        """Shrink an entry until the stop costs no more than the per-trade budget.

        The allocation comes from the strategy's target weight and the gate then judges it by how far the
        stop is, with nothing connecting the two. When a wide stop meets a large weight the gate refused
        the order OUTRIGHT, so the sleeve held nothing at all -- the same way a $544 allocation floored to
        zero shares left S8 without SPY, QQQ and MDY. On 2026-10-08 S1 wanted $1,211 of ETH/USD behind a
        stop 8.3% below, risking $100.67 against a $100 budget, and was refused over 67 cents.

        The backtest applies no per-trade risk cap -- it sizes on weight alone -- so this check is a
        live-only rule and it makes live differ from the test whichever way it goes. Holding 97% of the
        tested position is a far smaller difference than holding none of it, which is the whole argument
        for sizing down rather than refusing.
        """
        if stop is None or not (0 < stop < price):
            return dollars                              # no stop to size against (the benchmark sleeve)
        limits = self.settings.sleeve_limits(sleeve_id)
        if not limits.require_stop:
            return dollars
        budget = self.settings.sleeves[sleeve_id].capital_usd * limits.risk_per_trade_pct / 100
        # SIZING_MARGIN again because the gate re-prices on its own fresh quote a moment later, and a
        # cap that only just fits here would fail there on any small move.
        cap = (budget / (1 - stop / price)) * SIZING_MARGIN
        if cap >= dollars:
            return dollars
        report.actions.append(
            f"{sleeve_id} sized {symbol} down to ${cap:,.0f} from ${dollars:,.0f} to keep the stop "
            f"inside {limits.risk_per_trade_pct}% of sleeve capital")
        return cap

    def _exit(self, sleeve_id: str, symbol: str, reason: str, report: RunReport) -> None:
        state = self.journal.position_state(sleeve_id, symbol)
        if state is not None:
            self._cancel_stop(state)
        held = self.journal.sleeve_holdings(sleeve_id).get(symbol, 0.0)
        at_broker = sum(p.qty for p in self.broker.get_positions() if p.symbol == symbol)
        # Less whatever is already on its way out. The broker reserves the shares behind a resting sell,
        # so sending the exit a second time cannot do anything except be rejected.
        qty = min(held, at_broker) - self._sell_in_flight(sleeve_id, symbol)
        if qty <= DUST_QTY:
            return
        self._submit(OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.SELL, qty=qty, reason=reason), report)

    def _sell_in_flight(self, sleeve_id: str, symbol: str) -> float:
        """How much of `symbol` this sleeve is already selling at the broker, protective stops aside.

        A retiring sleeve re-decides on every run, by design: deciding once per daily bar would leave
        its positions open until tomorrow, when the point is to be out of them. The cost is that it
        asks to exit the same symbol every cycle, and the first ask is still queued while the market is
        shut. Alpaca holds the shares behind that queued order and refuses each repeat -- "insufficient
        qty available for order (requested: 3, available: 1)", logged every 16 minutes through the night
        of 2026-10-08 as S1 wound SPY and QQQ down into S8.

        Protective stops are deliberately NOT counted. `_exit` has just asked the broker to cancel the
        stop, and that request is not journalled until the broker confirms it, so the stop still reads
        as open here. Counting it would subtract the whole position and suppress the very exit being
        placed -- which is exactly what a broader first version of this guard did, and why a position
        that should have closed would instead have sat there unsold.
        """
        plain = (OrderType.MARKET.value, OrderType.LIMIT.value)
        return sum(max(0.0, (row["qty"] or 0.0) - (row["filled_qty"] or 0.0))
                   for row in self.journal.open_orders(self.broker.name)
                   if row["sleeve_id"] == sleeve_id and row["symbol"] == symbol
                   and row["side"] == Side.SELL.value and row["order_type"] in plain)

    def _resize(self, sleeve_id: str, symbol: str, qty_delta: float, price: float, reason: str,
                report: RunReport) -> None:
        if abs(qty_delta) * price < MIN_ORDER_USD:
            return
        if self.broker.order_step(symbol, self.settings.asset_class(symbol)) is not None:  # type: ignore[arg-type]
            qty_delta = math.copysign(self._round_qty(symbol, abs(qty_delta)), qty_delta)
            if qty_delta == 0:
                return
        side = Side.BUY if qty_delta > 0 else Side.SELL
        self._submit(OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=side, qty=abs(qty_delta), reason=reason),
                     report)

    def _submit(self, order: OrderRequest, report: RunReport) -> ExecutionResult:
        crypto = self.settings.asset_class(order.symbol) is AssetClass.CRYPTO
        result = self.executor.submit(order, wait_for_fill_s=self.fill_wait_s if crypto else 0.0)
        what = f"{order.sleeve_id} {order.side.value} {order.symbol} ({order.reason})"
        if not result.decision.approved:
            report.problems.append(f"blocked by risk gate: {what}: {result.decision.summary()}")
        elif result.error:
            report.problems.append(f"Alpaca rejected {what}: {result.error}")
        else:
            report.actions.append(f"sent {what}")
            for fill in result.fills or []:
                report.fills.append(fill)
                if self._should_notify():
                    self.notifier.send(_fill_text(fill, order.reason, self.settings.asset_class(order.symbol),
                                                  stop=order.stop_price, shadow=self._is_shadow()))
            if not result.fills and order.order_type is OrderType.MARKET and self._should_notify():
                self.notifier.send(
                    f"👻 {what} would be queued for the next open (shadow test — no order was sent)."
                    if self._is_shadow() else f"🕒 Queued {what}; fills when the market opens.")
        return result

    def _place_stop(self, sleeve_id: str, symbol: str, qty: float, stop: float, asset_class: AssetClass,
                    report: RunReport) -> None:
        if asset_class is AssetClass.CRYPTO:
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.SELL, qty=qty,
                                 order_type=OrderType.STOP_LIMIT, stop_price=round(stop, 2),
                                 limit_price=round(stop * (1 - CRYPTO_STOP_LIMIT_GAP), 2), reason="protective stop")
        else:
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.SELL, qty=qty,
                                 order_type=OrderType.STOP, stop_price=round_price(stop, asset_class),
                                 reason="protective stop")
        result = self.executor.submit(order, wait_for_fill_s=0.0)
        if result.decision.approved and not result.error:
            self.journal.upsert_position_state(sleeve_id, symbol, stop_order_id=order.client_order_id)
            report.actions.append(f"{sleeve_id} stop for {symbol} at {format_price(stop, asset_class)}")
        else:
            report.problems.append(f"could not place {sleeve_id} stop for {symbol}: "
                                   f"{result.error or result.decision.summary()}")

    def _move_stop(self, order_row, stop: float, asset_class: AssetClass, report: RunReport) -> None:
        """Trailing stop moved up: replace the resting order (moving a stop up only reduces risk).

        Goes through the executor/risk gate exactly like `_place_stop`, instead of calling the broker
        directly -- a trailed stop is still an order the engine derives, so the kill switch and every
        other gate check must apply to it the same way they apply to placing one.
        """
        sleeve_id, symbol = order_row["sleeve_id"], order_row["symbol"]
        limit = round(stop * (1 - CRYPTO_STOP_LIMIT_GAP), 2) if asset_class is AssetClass.CRYPTO else None
        order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.SELL, qty=order_row["qty"],
                             order_type=OrderType(order_row["order_type"]), stop_price=round_price(stop, asset_class),
                             limit_price=limit, reason="protective stop (trailed up)")
        result = self.executor.submit_replace(order, order_row["broker_order_id"])
        if result.decision.approved and not result.error:
            # The old order stays tracked until the broker confirms the replacement (Alpaca reports it as
            # "replaced", which sync maps to canceled). If it somehow filled first, the fill is still recorded.
            self.journal.mark_order(order_row["client_order_id"], OrderStatus.SUBMITTED, "replace requested")
            self.journal.upsert_position_state(sleeve_id, symbol, stop_order_id=order.client_order_id)
            report.actions.append(f"{sleeve_id} trailed {symbol} stop up to {format_price(stop, asset_class)}")
        else:
            report.problems.append(f"could not move {sleeve_id} stop for {symbol}: "
                                   f"{result.error or result.decision.summary()}")

    def _cancel_stop(self, state) -> None:
        if not state["stop_order_id"]:
            return
        row = self.journal.order(state["stop_order_id"])
        if row is not None and OrderStatus(row["status"]).is_open and row["broker_order_id"]:
            self.broker.cancel_order(row["broker_order_id"])
            # Deliberately NOT marked canceled here. Asking to cancel is not the same as it being
            # cancelled: an equity stop cancelled while the market is shut sits at "pending_cancel"
            # until the open, and the adapter rightly maps that to an OPEN status. Writing CANCELED
            # ourselves would take it out of open_orders(), so sync_open_orders would stop checking it,
            # and if the cancel were refused -- or the stop filled first -- we would never find out.
            # Leaving it open lets the next sync read the broker's own answer, whatever it turns out
            # to be. Observed on 2026-10-08 retiring S1: our journal said "canceled" while Alpaca still
            # said "pending_cancel" on both SPY and QQQ.
            self.journal.log(f"cancel requested for {state['symbol']} stop {row['client_order_id']}",
                             level="info")
        # The pointer IS cleared: the sleeve stops counting on this stop, which is what the caller
        # asked for. Keeping it was tried and reverted -- it contradicts
        # test_a_cancelled_stop_is_still_tracked_until_the_broker_confirms, and it is not needed,
        # because _reconcile_orders recovers a still-resting order from the broker on the next run
        # and _protect refuses to place a duplicate while one is holding the shares. One mechanism
        # (ask the broker) beats two (ask the broker AND hang on to a pointer that may be wrong).
        self.journal.upsert_position_state(state["sleeve_id"], state["symbol"], stop_order_id=None)

    def _create_state(self, sleeve_id: str, symbol: str) -> None:
        """New position: record entry price/date and the protective stop the entry order carried."""
        buys = [f for f in self.journal.fills(sleeve_id) if f["symbol"] == symbol and f["side"] == "buy"]
        if not buys:
            return
        last_entry = buys[-1]
        qty = sum(f["qty"] for f in buys if f["client_order_id"] == last_entry["client_order_id"])
        cost = sum(f["qty"] * f["price"] for f in buys if f["client_order_id"] == last_entry["client_order_id"])
        entry_price = cost / qty if qty else last_entry["price"]
        stop = last_entry["order_stop"] if last_entry["order_type"] == OrderType.MARKET.value else None
        self.journal.upsert_position_state(sleeve_id, symbol, entry_price=entry_price, entry_date=last_entry["ts"],
                                           stop=stop, initial_stop=stop, high_since_entry=entry_price, bars_held=0)

    def _my_states(self) -> list:
        return [s for s in self.journal.position_states() if s["sleeve_id"] in self.my_sleeves]

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round a quantity DOWN to what the broker accepts (whole shares, whole FX units, crypto as is)."""
        step = self.broker.order_step(symbol, self.settings.asset_class(symbol))  # type: ignore[arg-type]
        if step is None:
            return qty
        return round(math.floor(qty / step[0] + 1e-9) * step[0], 9)

    # --- reporting ----------------------------------------------------------------------------------

    def sleeve_value(self, sleeve: SleeveConfig, bars: dict[str, pd.DataFrame] | None = None) -> tuple[float, float]:
        """(sleeve equity, $ invested) = capital + trading cash flow + holdings at current prices."""
        invested = 0.0
        for symbol, qty in self.journal.sleeve_holdings(sleeve.id).items():
            if bars and symbol in bars:
                price = float(bars[symbol]["close"].iloc[-1])
            else:
                price = self.broker.get_quote(symbol, self.settings.asset_class(symbol)).price  # type: ignore[arg-type]
            invested += qty * price
        return sleeve.capital_usd + self.journal.sleeve_cash_flow(sleeve.id) + invested, invested

    def status_text(self) -> str:
        return status_text([self])


def _fill_text(fill: Fill, reason: str = "", asset_class: AssetClass | None = None,
               stop: float | None = None, shadow: bool = False) -> str:
    """One message that is a complete instruction.

    The stop is on here because of how this reads on a phone when you are the one placing the trade. The
    buy used to arrive on its own and the stop followed in a separate message on the NEXT engine run --
    up to fifteen minutes later. In between you either sat out of a trade the rules had taken, or you
    were in it with nothing protecting you. Both are worse than waiting, and neither is what the backtest
    measured, because there the stop exists from the moment of entry.

    `shadow` matters more than it looks. A shadow sleeve places nothing: its fills are simulated, and the
    only enabled one runs the FX rules that FAILED Gate 1 at an out-of-sample Sharpe of -0.184, meaning
    they lost to leaving the money in T-bills. That message read "S9 Bought 60000 EUR/USD @ 1.11894" with
    a stop and a dollar figure beside it -- identical in shape to a real fill, and the most
    actionable-looking thing this system sends. The dashboard says the verdict plainly; the phone did not
    say it at all, so the one alert that should carry the loudest caveat carried none.
    """
    verb = "Bought" if fill.side is Side.BUY else "Sold"
    if shadow:
        verb = "would have bought" if fill.side is Side.BUY else "would have sold"
    extra = f" ({reason})" if reason else ""
    ac = asset_class or AssetClass.US_EQUITY
    money = "" if ac is AssetClass.FOREX else "$"   # 1.14890 is a rate, not a dollar amount
    text = (f"{'👻' if shadow else ('🟢' if fill.side is Side.BUY else '🔴')} {fill.sleeve_id} {verb} "
            f"{fill.qty:g} {fill.symbol} @ {money}{format_price(fill.price, ac)}{extra}")
    if stop and fill.side is Side.BUY:
        risk = (fill.price - stop) * fill.qty
        text += (f"\n🛑 Stop {money}{format_price(stop, ac)}"
                 + (f" · {'would risk' if shadow else 'risks'} ${risk:,.0f} if it is hit" if risk > 0 else ""))
    if shadow:
        text += ("\n\n⚠️ Shadow test — nothing was traded and this is NOT a recommendation. These rules "
                 "failed Gate 1 out of sample, so they are being recorded to see whether that holds, not "
                 "followed. Do not place this trade.")
    return text


def _stop_move_text(sleeve_id: str, symbol: str, old: float | None, new: float,
                    asset_class: AssetClass, shadow: bool = False) -> str:
    """Manual execution lives or dies on this one: a trailing stop that moves and isn't copied across
    leaves the real position exposed exactly where the tested rules say it should have exited.

    Which is why the `shadow` caveat matters more here than on a fill. This is reached only for a
    hand-executed broker, and the only one of those today is the FX shadow running rules that failed
    Gate 1 -- so every message this has ever sent was for that strategy, and it ended with a flat
    instruction to go and move a stop. Telling someone to adjust a real position on behalf of rules that
    lost to T-bills is the most direct way this system could cost money.
    """
    was = format_price(old, asset_class) if old else "none"
    text = (f"{'👻' if shadow else '📈'} {sleeve_id} · {symbol} stop "
            f"{'would move' if shadow else 'moves'} {was} → {format_price(new, asset_class)}")
    if shadow:
        return text + ("\n\n⚠️ Shadow test — there is no position behind this. These rules failed Gate 1 "
                       "out of sample, so do not move a real stop for them.")
    return text + "\nMove your stop if you are following this by hand."


# --- shared across brokers --------------------------------------------------------------------------


def status_text(engines: list[Engine]) -> str:
    """The daily summary, which is the message most likely to be read and least likely to be questioned.

    A shadow engine was listed as "<name> paper account $X" directly beneath the real Alpaca one, and its
    sleeve's return beside the real sleeves'. A shadow holds no account and placed none of those trades,
    and the only enabled one runs rules that failed Gate 1, so a glance down this list read a simulation
    of a losing strategy as money that had been made.
    """
    lines = [f"Kill switch: {'ON' if engines[0].kill_switch.active else 'off'}"]
    for engine in engines:
        account = engine.executor.account()
        day = account.equity - (account.last_equity or account.equity)
        lines.append(f"{engine.broker.name} simulated, NOT an account ${account.equity:,.0f} "
                     f"(today {day:+,.0f})" if engine._is_shadow() else
                     f"{engine.broker.name} paper account ${account.equity:,.0f} (today {day:+,.0f})")
        for sleeve in engine._live_sleeves():
            equity, invested = engine.sleeve_value(sleeve)
            lines.append(f"  {sleeve.id} {sleeve.name}: ${equity:,.0f} ({equity / sleeve.capital_usd - 1:+.2%}), "
                         f"{invested / equity:.0%} invested")
        for s in engine._my_states():
            lines.append(f"    {s['sleeve_id']} {s['symbol']}: stop {s['stop']:,.4g}" if s["stop"] else
                         f"    {s['sleeve_id']} {s['symbol']}: no stop (benchmark)")
    return "\n".join(lines)


def handle_commands(notifier: Notifier, journal: Journal, kill_switch: KillSwitch, engines: list[Engine]) -> list[str]:
    done = []
    for command in notifier.poll_commands(journal):
        if command == "/halt":
            kill_switch.engage("halted from Telegram")
            notifier.send("🛑 Kill switch ON. No new orders. Existing stops stay at the brokers. /resume to undo.")
        elif command == "/resume":
            kill_switch.release()
            notifier.send("✅ Kill switch OFF. Trading resumes on the next run.")
        elif command == "/status":
            notifier.send(status_text(engines))
        else:
            notifier.send("Commands: /status · /halt · /resume")
        done.append(f"telegram {command}")
    return done


def send_digest(notifier: Notifier, journal: Journal, engines: list[Engine], now: datetime) -> None:
    local = now.astimezone(SYDNEY)
    if local.hour < DIGEST_HOUR or journal.kv_get("last_digest") == str(local.date()):
        return
    journal.kv_set("last_digest", str(local.date()))
    # The problems that were raised once and then counted quietly. Without this the rationing in
    # `worth_sending` would be the old silence wearing a different hat: you would see the first
    # connection timeout and never learn it then happened another three hundred and thirty-six times.
    since = [(now - timedelta(days=d)).date() for d in (1, 0)]
    repeats = [(shape, n) for shape, n in alert_counts(journal, since) if n > 1]
    tail = ""
    if repeats:
        lines = "\n".join(f"  {n}x {shape}" for shape, n in repeats[:8])
        more = f"\n  and {len(repeats) - 8} more" if len(repeats) > 8 else ""
        tail = f"\n\nRepeated problems, told you once:\n{lines}{more}"
    notifier.send("☀️ Daily summary\n" + status_text(engines) + tail)


class Coordinator:
    """Runs one Engine per broker under a single lock, then writes the shared outputs once."""

    def __init__(self, settings: Settings, journal: Journal, engines: list[Engine], notifier: Notifier,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 fx_notifier: Notifier | None = None) -> None:
        self.settings, self.journal, self.engines, self.notifier, self.clock = settings, journal, engines, notifier, clock
        # Forex messages have their own destination when one is configured. Defaults to the main
        # notifier so every existing caller and test behaves exactly as before; `cmd_run` passes the
        # real one. Without this the split would only cover `trader watch-fx` and the engine's own
        # fifteen-minute pass would keep posting tickets into the busy chat -- the same message arriving
        # in two different places depending on which schedule happened to catch the level.
        self.fx_notifier = fx_notifier or notifier
        for engine in engines:
            engine.handle_commands = False
            engine.shared_outputs = False

    def run_once(self) -> list[RunReport]:
        from trader.engine.dashboard import write_dashboard
        from trader.journal.vault import write_vault

        lock_path = self.settings.root / "data" / "engine.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise EngineBusy("another engine run is still in progress") from exc
            try:
                handle_commands(self.notifier, self.journal, self.engines[0].kill_switch, self.engines)
            except Exception as exc:
                self.journal.log(f"telegram commands failed: {exc}", level="error")
            reports = [engine._run() for engine in self.engines]  # noqa: SLF001
            # Once per cycle, not once per broker: it is a watcher over one symbol, and running it per
            # engine would message you twice for the same signal.
            self._check_alerts_got_through(reports[0])
            failed: list[str] = []
            for name, fn in (("intraday watch", lambda: self._intraday(reports[0])),
                             ("vault", lambda: write_vault(self.settings, self.journal, self.engines)),
                             ("dashboard", lambda: write_dashboard(self.settings, self.journal, self.engines)),
                             ("daily digest", lambda: send_digest(self.notifier, self.journal, self.engines, self.clock()))):
                try:
                    fn()
                except Exception as exc:
                    failed.append(f"{name} failed: {type(exc).__name__}: {exc}")
                    self.journal.log(f"{name} failed: {exc}", level="error")
            reports[0].problems.extend(failed)
            # Through the same budget as the engine's own problems: `vault failed` had been sent 133
            # times and `dashboard failed` 131, every one of them the same flapping Alpaca connection.
            for msg in worth_sending(self.journal, "outputs", self.clock().date(), failed):
                self.notifier.send(f"⚠️ {msg}")
            return reports

    def _check_alerts_got_through(self, report: RunReport) -> None:
        """Did the messages this run sent actually reach the phone?

        `Telegram.send` deliberately swallows failures so an alert cannot stop the engine trading, and it
        used to swallow them into a Python logger nobody reads. A wrong chat id, a revoked token or a
        blocked bot would then make every message disappear while the run reported itself clean -- and
        for a system whose entire job is to tell you things, that is indistinguishable from a quiet
        market. You would sit waiting for a signal that had already been sent into nothing.
        """
        discarded = getattr(self.notifier, "sent", None)
        if discarded is not None and getattr(self.notifier, "misconfigured", False):
            # A NullNotifier that is not a dry run: the run has just "sent" every one of these into a
            # list and is about to drop it. This is what had been happening for weeks -- see
            # telegram.from_env -- and the only reason nobody could tell is that nothing looked.
            reason = getattr(self.notifier, "reason", "Telegram is not set up")
            self.journal.kv_set("alert_channel", f"DISCARDING {reason}")
            self.journal.log(f"alerts going nowhere: {len(discarded)} message(s) discarded, {reason}",
                             level="error")
            report.problems.append(f"{len(discarded)} alert(s) went nowhere: {reason}")
            return
        failures = getattr(self.notifier, "failures", 0)
        if not failures:
            # Record the healthy case too, so "the alerts are working" is something you can check
            # rather than assume. The whole A2 failure was invisible because nothing wrote this down:
            # a clean run and a run whose every message vanished produced identical journals.
            self.journal.kv_set("alert_channel", f"{type(self.notifier).__name__} ok "
                                                 f"{self.clock().isoformat(timespec='seconds')}")
            return
        detail = getattr(self.notifier, "last_error", None) or "unknown error"
        self.journal.kv_set("alert_channel", f"FAILING {failures} consecutive: {detail}")
        report.problems.append(f"{failures} alert(s) did not reach Telegram: {detail}")
        self.journal.log(f"telegram delivery failing: {failures} consecutive, last error {detail}",
                         level="error")

    def _intraday(self, report: RunReport) -> None:
        """Session-reversion signals. Sends messages; never places an order.

        Kill-switch aware on purpose. It places nothing, so it cannot breach a limit -- but a halted
        system that keeps texting you trades to place by hand has not really halted.
        """
        from trader.engine.intraday_watch import IntradayWatcher

        if not self.settings.intraday.enabled:
            return
        if self.engines[0].kill_switch.active:
            return
        watcher = IntradayWatcher(self.settings, self.journal, self.fx_notifier, clock=self.clock)
        # Swept first and unconditionally: a trade stranded by a missed 16:00 close has to be raised
        # whatever today is, including a weekend, and before a new day's plan is built over the top.
        did = watcher.sweep_unfinished() + watcher.run_once(report) + watcher.close_out()
        report.actions += did
        # Journalled explicitly, because this runs AFTER each engine has already logged its own
        # "run finished" line -- so everything the forex watcher did was absent from the journal
        # entirely. Asked why no currency messages were arriving, there was no record to check: the kv
        # state said a day had finished and nothing said what had been sent. These are the only messages
        # you are expected to act on by hand, so they are the last thing that should be unlogged.
        for what in did:
            self.journal.log(what, level="info")
