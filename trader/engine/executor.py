"""Order pipeline: journal the proposal -> risk gate -> broker -> journal the result and fills."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from trader.brokers.base import Broker, LiveTradingRefused
from trader.config import CostModel, Settings
from trader.journal.db import Journal, trading_day_start
from trader.models import AssetClass, BrokerOrder, Fill, MarketClock, OrderRequest, OrderStatus, Quote, RiskDecision
from trader.risk.gate import RiskContext, RiskGate
from trader.risk.kill_switch import KillSwitch


@dataclass
class ExecutionResult:
    order: OrderRequest
    decision: RiskDecision
    broker_order: BrokerOrder | None = None
    fills: list[Fill] | None = None
    error: str = ""

    @property
    def status(self) -> OrderStatus:
        if self.error or not self.decision.approved:
            return OrderStatus.REJECTED
        return self.broker_order.status if self.broker_order else OrderStatus.PROPOSED


class Executor:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        journal: Journal,
        kill_switch: KillSwitch | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.risk.mode != "paper" or not broker.is_paper:
            raise LiveTradingRefused("Live trading is not part of this plan. Set mode: paper in config/risk.yaml.")
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.kill_switch = kill_switch or KillSwitch(settings.kill_switch_path)
        self.gate = RiskGate(settings)
        self.clock = clock
        self.sleep = sleep

    def build_context(self, order: OrderRequest) -> RiskContext:
        now = self.clock()
        asset_class = self.settings.asset_class(order.symbol)
        account = self.account()
        self.journal.snapshot_equity(self.broker.name, account.equity, account.cash)
        quote = self.broker.get_quote(order.symbol, asset_class) if asset_class else None
        guards = self.settings.risk.guards
        return RiskContext(
            now=now,
            broker_is_paper=self.broker.is_paper,
            account=account,
            positions=self.broker.get_positions(),
            # Unknown symbols are rejected by the gate; a zero-price placeholder keeps the context complete.
            quote=quote or Quote(symbol=order.symbol, price=0.0, ts=now),
            asset_class=asset_class,
            sleeve_holdings=self.journal.sleeve_holdings(order.sleeve_id),
            peak_equity=self.journal.peak_equity(self.broker.name),
            orders_today=self.journal.orders_submitted_since(trading_day_start(now)),
            recent_orders=self.journal.recent_orders(timedelta(seconds=guards.duplicate_order_window_s), now),
            kill_switch_active=self.kill_switch.active,
            kill_switch_reason=self.kill_switch.reason,
            market=self._market(asset_class, now),
        )

    def _market(self, asset_class: AssetClass | None, now: datetime) -> MarketClock | None:
        """A clock, so the gate can tell "the market is shut" from "the feed has died".

        It only ever got one for US equities, which left the FOREX arm of the gate's freshness check
        unreachable -- and that arm exists precisely to judge a closed-market quote against the last
        close. So an FX order over a weekend was refused as "price is 171982s old", blaming the feed for
        a market that was simply shut. Two of the nine refusals in the journal say exactly that.

        Crypto stays None deliberately: it never closes, so a stale crypto price really is a fault.
        """
        if asset_class is AssetClass.US_EQUITY:
            return self.broker.get_clock()
        if asset_class is AssetClass.FOREX:
            from trader.brokers.oanda import fx_clock

            return fx_clock(now)
        return None

    def submit(self, order: OrderRequest, wait_for_fill_s: float = 10.0) -> ExecutionResult:
        asset_class = self.settings.asset_class(order.symbol)
        self.journal.record_order(order, asset_class.value if asset_class else None)

        decision = self.gate.check(order, self.build_context(order))
        self.journal.record_decision(order.client_order_id, decision)
        if decision.halt_account and not self.kill_switch.active:
            self.kill_switch.engage("account drawdown limit hit; review, then run `trader resume`")
            self.journal.log("kill switch engaged by drawdown limit", level="critical")
        result = ExecutionResult(order=order, decision=decision)
        if not decision.approved:
            return result

        try:
            result.broker_order = self.broker.submit_order(order, asset_class)  # type: ignore[arg-type]
        except Exception as exc:  # broker rejected or network failure: record it, never retry blindly
            result.error = str(exc)
            self.journal.record_broker_error(order.client_order_id, result.error)
            self.journal.log(f"broker error for {order.client_order_id}: {exc}", level="error")
            return result
        self.journal.record_broker_order(self.broker.name, result.broker_order)
        result.broker_order, result.fills = self._track_fills(result.broker_order, order.sleeve_id, wait_for_fill_s)
        return result

    def account(self):
        """The broker account, with the day's starting equity filled in from the journal if the broker
        doesn't report it (OANDA), so the daily loss limit works everywhere."""
        account = self.broker.get_account()
        if account.last_equity is None:
            start = self.journal.first_equity_since(self.broker.name, trading_day_start(self.clock()))
            account = account.model_copy(update={"last_equity": start or account.equity})
        return account

    def sync_open_orders(self) -> list[Fill]:
        """Pick up fills for this broker's open orders (e.g. stock orders placed while the market was shut)."""
        fills: list[Fill] = []
        for row in self.journal.open_orders(self.broker.name):
            bo = self.broker.get_order(row["broker_order_id"])
            fill = self.journal.record_fill_progress(bo, row["sleeve_id"], self._cost_model(bo.symbol))
            if fill:
                fills.append(fill)
        return fills

    # --- internals ----------------------------------------------------------------------------

    def _track_fills(self, bo: BrokerOrder, sleeve_id: str, wait_s: float) -> tuple[BrokerOrder, list[Fill]]:
        """Poll the broker until the order is done or `wait_s` passes; journal each new fill."""
        fills: list[Fill] = []
        deadline = time.monotonic() + wait_s
        while True:
            fill = self.journal.record_fill_progress(bo, sleeve_id, self._cost_model(bo.symbol))
            if fill:
                fills.append(fill)
            if not bo.status.is_open or time.monotonic() >= deadline:
                break
            self.sleep(1.0)
            try:
                bo = self.broker.get_order(bo.broker_order_id)
            except Exception as exc:  # the order is placed; `trader sync` will pick up its fills later
                self.journal.log(f"could not poll {bo.client_order_id}: {exc}", level="warning")
                break
        return bo, fills

    def _cost_model(self, symbol: str) -> CostModel:
        return self.settings.costs[self.settings.asset_class(symbol)]  # type: ignore[index]
