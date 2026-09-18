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
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from trader.brokers.base import Broker
from trader.config import Settings, SleeveConfig
from trader.data.live_bars import BarSource
from trader.engine.executor import ExecutionResult, Executor
from trader.journal.db import DUST_QTY, Journal
from trader.models import AssetClass, Fill, MarketClock, OrderRequest, OrderStatus, OrderType, Side, new_client_order_id
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
                 executor: Executor | None = None, fill_wait_s: float = 15.0) -> None:
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.bars = bars
        self.notifier = notifier or NullNotifier()
        self.clock = clock
        self.kill_switch = KillSwitch(settings.kill_switch_path)
        self.executor = executor or Executor(settings, broker, journal, kill_switch=self.kill_switch, clock=clock)
        self.fill_wait_s = fill_wait_s

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
        steps: list[tuple[str, Callable[[RunReport], None]]] = [
            ("telegram commands", self._handle_commands),
            ("sync fills", self._sync_fills),
            ("reconcile", self._reconcile),
            ("position state", self._refresh_states),
            ("protective stops", self._protect),
            ("decisions", self._decide),
            ("position state", self._refresh_states),  # register positions filled just now...
            ("protective stops", self._protect),       # ...so they get their stop in this same run
            ("sleeve values", self._record_equity),
            ("vault", self._write_vault),
            ("dashboard", self._write_dashboard),
            ("daily digest", self._digest),
        ]
        for name, step in steps:
            try:
                step(report)
            except Exception as exc:  # keep going: one failing step must not stop stops/fills handling
                msg = f"{name} failed: {type(exc).__name__}: {exc}"
                report.problems.append(msg)
                self.journal.log(msg, level="error")
        for msg in report.problems:
            self.notifier.send(f"⚠️ {msg}")
        self.journal.kv_set("last_run_summary", report.summary())
        self.journal.log(f"run finished: {report.summary()}")
        return report

    # --- steps ------------------------------------------------------------------------------------

    def _handle_commands(self, report: RunReport) -> None:
        for command in self.notifier.poll_commands(self.journal):
            if command == "/halt":
                self.kill_switch.engage("halted from Telegram")
                self.notifier.send("🛑 Kill switch ON. No new orders. Existing stops stay at Alpaca. /resume to undo.")
            elif command == "/resume":
                self.kill_switch.release()
                self.notifier.send("✅ Kill switch OFF. Trading resumes on the next run.")
            elif command == "/status":
                self.notifier.send(self.status_text())
            else:
                self.notifier.send("Commands: /status · /halt · /resume")
            report.actions.append(f"telegram {command}")

    def _sync_fills(self, report: RunReport) -> None:
        for fill in self.executor.sync_open_orders():
            report.fills.append(fill)
            self.notifier.send(_fill_text(fill))

    def _reconcile(self, report: RunReport) -> None:
        """Journal holdings (all sleeves together) must equal Alpaca's position in each symbol."""
        ours: dict[str, float] = {}
        for (_, symbol), qty in self.journal.all_holdings().items():
            ours[symbol] = ours.get(symbol, 0.0) + qty
        theirs = {p.symbol: p.qty for p in self.broker.get_positions()}
        for symbol in sorted(set(ours) | set(theirs)):
            a, b = ours.get(symbol, 0.0), theirs.get(symbol, 0.0)
            if abs(a - b) > max(DUST_QTY, 1e-6 * max(abs(a), abs(b))):
                report.mismatched.add(symbol)
                msg = f"{symbol}: journal holds {a:g} but Alpaca holds {b:g}; not trading it until this is fixed"
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
        for state in self.journal.position_states():
            if holdings.get((state["sleeve_id"], state["symbol"]), 0.0) <= DUST_QTY:
                self._cancel_stop(state)
                self.journal.delete_position_state(state["sleeve_id"], state["symbol"])
                report.actions.append(f"{state['sleeve_id']} {state['symbol']} position closed")
                self.notifier.send(f"✅ {state['sleeve_id']} closed its {state['symbol']} position.")

    def _protect(self, report: RunReport) -> None:
        market = self.broker.get_clock()
        broker_qty = {p.symbol: p.qty for p in self.broker.get_positions()}
        for state in self.journal.position_states():
            sleeve_id, symbol, stop = state["sleeve_id"], state["symbol"], state["stop"]
            if stop is None or symbol in report.mismatched:
                continue
            asset_class = self.settings.asset_class(symbol)
            qty = min(self.journal.sleeve_holdings(sleeve_id).get(symbol, 0.0), broker_qty.get(symbol, 0.0))
            if asset_class is AssetClass.US_EQUITY:
                qty = math.floor(qty + 1e-9)
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
                self._move_stop(order_row, stop, asset_class, report)  # type: ignore[arg-type]

    def _decide(self, report: RunReport) -> None:
        if self.kill_switch.active:
            report.actions.append("decisions skipped: kill switch is on")
            return
        now = self.clock()
        market = self.broker.get_clock()
        for sleeve in self._live_sleeves():
            self._decide_sleeve(sleeve, now, market, report)

    def _record_equity(self, report: RunReport) -> None:
        for sleeve in self._live_sleeves():
            equity, invested = self.sleeve_value(sleeve)
            adjusted = equity - self.journal.sleeve_haircut(sleeve.id)
            self.journal.record_sleeve_equity(sleeve.id, equity, adjusted, invested)

    def _write_vault(self, report: RunReport) -> None:
        from trader.journal.vault import write_vault

        write_vault(self.settings, self.journal, self)

    def _write_dashboard(self, report: RunReport) -> None:
        from trader.engine.dashboard import write_dashboard

        write_dashboard(self.settings, self.journal, self)

    def _digest(self, report: RunReport) -> None:
        local = self.clock().astimezone(SYDNEY)
        if local.hour < DIGEST_HOUR or self.journal.kv_get("last_digest") == str(local.date()):
            return
        self.journal.kv_set("last_digest", str(local.date()))
        self.notifier.send("☀️ Daily summary\n" + self.status_text())

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

        due: dict[pd.Timestamp, list[str]] = {}
        for symbol in sleeve.symbols:
            if symbol in report.mismatched:
                continue
            latest = bars[symbol].index[-1]
            last = self.journal.last_decided_bar(sleeve.id, symbol)
            if last is not None and latest <= pd.Timestamp(last):
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
                        self.journal.upsert_position_state(sleeve.id, symbol, stop=new_stop)
            prices = live
            current = {s: q * prices[s] / equity for s, q in held.items() if equity > 0}
            for symbol, weight in plan_weights(targets, current, prices, limits).items():
                reason = targets[symbol].reason
                if weight <= 0:
                    self._exit(sleeve.id, symbol, reason, report)
                elif symbol not in held:
                    dollars = weight * equity * SIZING_MARGIN
                    self._enter(sleeve.id, symbol, dollars, prices[symbol], targets[symbol].stop, reason, report)
                else:
                    self._resize(sleeve.id, symbol, weight * equity / prices[symbol] - held[symbol], prices[symbol],
                                 reason, report)
            for symbol in symbols:
                self.journal.set_decided_bar(sleeve.id, symbol, str(date.date()))
            report.actions.append(f"{sleeve.id} decided on {date.date()} bar for {', '.join(symbols)}")

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
        if asset_class is AssetClass.US_EQUITY:
            qty = math.floor(dollars / price)  # whole shares, so the protective stop can stay at Alpaca (GTC)
            if qty < 1:
                report.actions.append(f"{sleeve_id} skipped {symbol}: ${dollars:,.0f} buys less than one share")
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
        if self.settings.asset_class(symbol) is AssetClass.US_EQUITY:
            qty_delta = math.floor(qty_delta) if qty_delta > 0 else -math.floor(-qty_delta)
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
                self.notifier.send(_fill_text(fill, order.reason))
            if not result.fills and order.order_type is OrderType.MARKET:
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
                                 order_type=OrderType.STOP, stop_price=round(stop, 2), reason="protective stop")
        result = self.executor.submit(order, wait_for_fill_s=0.0)
        if result.decision.approved and not result.error:
            self.journal.upsert_position_state(sleeve_id, symbol, stop_order_id=order.client_order_id)
            report.actions.append(f"{sleeve_id} stop for {symbol} at {stop:,.2f}")
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
                                   qty=order_row["qty"], order_type=order_type, stop_price=round(stop, 2),
                                   limit_price=limit, reason="protective stop (trailed up)", client_order_id=new_id)
        self.journal.record_order(replacement, asset_class.value)
        self.journal.record_broker_order(self.broker.name, bo)
        self.journal.mark_order(order_row["client_order_id"], OrderStatus.CANCELED, "replaced")
        self.journal.upsert_position_state(order_row["sleeve_id"], order_row["symbol"], stop_order_id=new_id)
        report.actions.append(f"{order_row['sleeve_id']} trailed {order_row['symbol']} stop up to {stop:,.2f}")

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
        account = self.broker.get_account()
        lines = [f"Paper account ${account.equity:,.0f} (today {account.equity - account.last_equity:+,.0f})",
                 f"Kill switch: {'ON' if self.kill_switch.active else 'off'}"]
        for sleeve in self._live_sleeves():
            equity, invested = self.sleeve_value(sleeve)
            ret = equity / sleeve.capital_usd - 1
            lines.append(f"{sleeve.id} {sleeve.name}: ${equity:,.0f} ({ret:+.2%}), {invested / equity:.0%} invested")
        states = self.journal.position_states()
        for s in states:
            lines.append(f"  {s['sleeve_id']} {s['symbol']}: stop {s['stop']:,.2f}" if s["stop"] else
                         f"  {s['sleeve_id']} {s['symbol']}: no stop (benchmark)")
        return "\n".join(lines)


def _fill_text(fill: Fill, reason: str = "") -> str:
    verb = "Bought" if fill.side is Side.BUY else "Sold"
    extra = f" ({reason})" if reason else ""
    return f"{'🟢' if fill.side is Side.BUY else '🔴'} {fill.sleeve_id} {verb} {fill.qty:g} {fill.symbol} @ ${fill.price:,.2f}{extra}"
