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
from datetime import datetime, timedelta, timezone
from typing import Callable
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
    new_client_order_id,
    round_price,
)
from trader.notify.telegram import Notifier, NullNotifier
from trader.risk.kill_switch import KillSwitch
from trader.risk.sizing import plan_weights
from trader.strategies import LIVE_REGISTRY
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
HAND_EXECUTED = {"shadow_fx"}


@dataclass
class RunReport:
    started: datetime
    fills: list[Fill] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    mismatched: set[str] = field(default_factory=set)

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
        self.journal.kv_set("last_run", report.started.isoformat(timespec="seconds"))  # shown on dashboard/vault
        self.journal.kv_set(f"last_run:{self.broker.name}", report.started.isoformat(timespec="seconds"))
        steps: list[tuple[str, Callable[[RunReport], None]]] = [
            *([("telegram commands", self._handle_commands)] if self.handle_commands else []),
            ("sync fills", self._sync_fills),
            ("reconcile", self._reconcile),
            ("position state", self._refresh_states),
            ("protective stops", self._protect),
            ("decisions", self._decide),
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
        for msg in report.problems:
            self.notifier.send(f"⚠️ {self.broker.name}: {msg}")
        self.journal.kv_set("last_run_summary", report.summary())
        self.journal.log(f"run finished: {report.summary()}")
        return report

    # --- steps ------------------------------------------------------------------------------------

    def _handle_commands(self, report: RunReport) -> None:
        report.actions += handle_commands(self.notifier, self.journal, self.kill_switch, [self])

    def _should_notify(self) -> bool:
        """Real brokers always message you. Shadow sleeves stay quiet, except the hand-executed ones."""
        return not self.broker.name.startswith("shadow") or self.broker.name in HAND_EXECUTED

    def _sync_fills(self, report: RunReport) -> None:
        for fill in self.executor.sync_open_orders():
            report.fills.append(fill)
            if self._should_notify():
                self.notifier.send(_fill_text(fill, asset_class=self.settings.asset_class(fill.symbol)))

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
                if self.broker.name != "shadow":
                    self.notifier.send(f"✅ {state['sleeve_id']} closed its {state['symbol']} position.")

    def _protect(self, report: RunReport) -> None:
        market = self.broker.get_clock()
        broker_qty = {p.symbol: p.qty for p in self.broker.get_positions()}
        for state in self._my_states():
            sleeve_id, symbol, stop = state["sleeve_id"], state["symbol"], state["stop"]
            if stop is None or symbol in report.mismatched:
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
            if not resting:
                self._place_stop(sleeve_id, symbol, qty, stop, asset_class, report)  # type: ignore[arg-type]
            elif stop > (order_row["stop_price"] or 0) * (1 + STOP_MOVE_MIN):
                if asset_class is AssetClass.CRYPTO or market.is_open:
                    self._move_stop(order_row, stop, asset_class, report)  # type: ignore[arg-type]
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

    def _decide_sleeve(self, sleeve: SleeveConfig, now: datetime, market: MarketClock, report: RunReport) -> None:
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
            if last is not None and latest <= pd.Timestamp(last) and new_weights is None:
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
        for date in sorted(due):
            symbols = due[date]
            equity, _ = self.sleeve_value(sleeve)
            held = self.journal.sleeve_holdings(sleeve.id)
            holdings = self._holdings(sleeve.id, held)
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
                                                               self.settings.asset_class(symbol)))
            prices = live
            current = {s: q * prices[s] / equity for s, q in held.items() if equity > 0}
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

    def _enter(self, sleeve_id: str, symbol: str, dollars: float, price: float, stop: float | None, reason: str,
               report: RunReport) -> None:
        asset_class = self.settings.asset_class(symbol)
        step = self.broker.order_step(symbol, asset_class)  # type: ignore[arg-type]
        if step is not None:  # whole shares at Alpaca, whole units at OANDA
            qty = self._round_qty(symbol, dollars / price)
            if qty < step[1]:
                report.actions.append(f"{sleeve_id} skipped {symbol}: ${dollars:,.0f} is below the minimum order size")
                return
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.BUY, qty=qty, stop_price=stop,
                                 reason=reason)
        else:
            if dollars < MIN_ORDER_USD:
                return
            order = OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.BUY, notional=round(dollars, 2),
                                 stop_price=stop, reason=reason)
        self._submit(order, report)

    def _exit(self, sleeve_id: str, symbol: str, reason: str, report: RunReport) -> None:
        state = self.journal.position_state(sleeve_id, symbol)
        if state is not None:
            self._cancel_stop(state)
        held = self.journal.sleeve_holdings(sleeve_id).get(symbol, 0.0)
        at_broker = sum(p.qty for p in self.broker.get_positions() if p.symbol == symbol)
        qty = min(held, at_broker)
        if qty <= DUST_QTY:
            return
        self._submit(OrderRequest(sleeve_id=sleeve_id, symbol=symbol, side=Side.SELL, qty=qty, reason=reason), report)

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
                if self.broker.name != "shadow":
                    self.notifier.send(_fill_text(fill, order.reason, self.settings.asset_class(order.symbol)))
            if not result.fills and order.order_type is OrderType.MARKET and self.broker.name != "shadow":
                self.notifier.send(f"🕒 Queued {what}; fills when the market opens.")
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
        """Trailing stop moved up: replace the resting order (moving a stop up only reduces risk)."""
        new_id = new_client_order_id(order_row["sleeve_id"])
        limit = round(stop * (1 - CRYPTO_STOP_LIMIT_GAP), 2) if asset_class is AssetClass.CRYPTO else None
        bo = self.broker.replace_stop(order_row["broker_order_id"], new_id, stop, limit)
        order_type = OrderType(order_row["order_type"])
        replacement = OrderRequest(sleeve_id=order_row["sleeve_id"], symbol=order_row["symbol"], side=Side.SELL,
                                   qty=order_row["qty"], order_type=order_type,
                                   stop_price=round_price(stop, asset_class),
                                   limit_price=limit, reason="protective stop (trailed up)", client_order_id=new_id)
        self.journal.record_order(replacement, asset_class.value)
        self.journal.record_broker_order(self.broker.name, bo)
        # The old order stays tracked until the broker confirms the replacement (Alpaca reports it as
        # "replaced", which sync maps to canceled). If it somehow filled first, the fill is still recorded.
        self.journal.mark_order(order_row["client_order_id"], OrderStatus.SUBMITTED, "replace requested")
        self.journal.upsert_position_state(order_row["sleeve_id"], order_row["symbol"], stop_order_id=new_id)
        report.actions.append(f"{order_row['sleeve_id']} trailed {order_row['symbol']} stop up to "
                              f"{format_price(stop, asset_class)}")

    def _cancel_stop(self, state) -> None:
        if not state["stop_order_id"]:
            return
        row = self.journal.order(state["stop_order_id"])
        if row is not None and OrderStatus(row["status"]).is_open and row["broker_order_id"]:
            self.broker.cancel_order(row["broker_order_id"])
            self.journal.mark_order(row["client_order_id"], OrderStatus.CANCELED, "canceled by engine")
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


def _fill_text(fill: Fill, reason: str = "", asset_class: AssetClass | None = None) -> str:
    verb = "Bought" if fill.side is Side.BUY else "Sold"
    extra = f" ({reason})" if reason else ""
    ac = asset_class or AssetClass.US_EQUITY
    money = "" if ac is AssetClass.FOREX else "$"   # 1.14890 is a rate, not a dollar amount
    return (f"{'🟢' if fill.side is Side.BUY else '🔴'} {fill.sleeve_id} {verb} {fill.qty:g} {fill.symbol} "
            f"@ {money}{format_price(fill.price, ac)}{extra}")


def _stop_move_text(sleeve_id: str, symbol: str, old: float | None, new: float,
                    asset_class: AssetClass) -> str:
    """Manual execution lives or dies on this one: a trailing stop that moves and isn't copied across
    leaves the real position exposed exactly where the tested rules say it should have exited."""
    was = format_price(old, asset_class) if old else "none"
    return (f"📈 {sleeve_id} · {symbol} stop moves {was} → {format_price(new, asset_class)}\n"
            f"Move your stop if you are following this by hand.")


# --- shared across brokers --------------------------------------------------------------------------


def status_text(engines: list[Engine]) -> str:
    lines = [f"Kill switch: {'ON' if engines[0].kill_switch.active else 'off'}"]
    for engine in engines:
        account = engine.executor.account()
        day = account.equity - (account.last_equity or account.equity)
        lines.append(f"{engine.broker.name} paper account ${account.equity:,.0f} (today {day:+,.0f})")
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
    notifier.send("☀️ Daily summary\n" + status_text(engines))


class Coordinator:
    """Runs one Engine per broker under a single lock, then writes the shared outputs once."""

    def __init__(self, settings: Settings, journal: Journal, engines: list[Engine], notifier: Notifier,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.settings, self.journal, self.engines, self.notifier, self.clock = settings, journal, engines, notifier, clock
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
            for name, fn in (("vault", lambda: write_vault(self.settings, self.journal, self.engines)),
                             ("dashboard", lambda: write_dashboard(self.settings, self.journal, self.engines)),
                             ("daily digest", lambda: send_digest(self.notifier, self.journal, self.engines, self.clock()))):
                try:
                    fn()
                except Exception as exc:
                    reports[0].problems.append(f"{name} failed: {type(exc).__name__}: {exc}")
                    self.journal.log(f"{name} failed: {exc}", level="error")
                    self.notifier.send(f"⚠️ {name} failed: {exc}")
            return reports
