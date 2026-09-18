"""Wraps a real broker for `trader run --dry-run`: reads are real, nothing is ever sent.

Orders come back as "accepted" and never fill, so the engine shows what it would do on real data
without touching the account.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from trader.brokers.base import Broker
from trader.models import Account, AssetClass, BrokerOrder, MarketClock, OrderRequest, OrderStatus, Position, Quote


class DryRunBroker(Broker):
    def __init__(self, real: Broker) -> None:
        self.real = real
        self.name = real.name
        self.would_send: list[OrderRequest] = []
        self._orders: dict[str, BrokerOrder] = {}

    @property
    def is_paper(self) -> bool:
        return self.real.is_paper

    def get_account(self) -> Account:
        return self.real.get_account()

    def get_positions(self) -> list[Position]:
        return self.real.get_positions()

    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        return self.real.get_quote(symbol, asset_class)

    def get_clock(self) -> MarketClock:
        return self.real.get_clock()

    def get_open_orders(self) -> list[BrokerOrder]:
        return self.real.get_open_orders()

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        self.would_send.append(order)
        bo = BrokerOrder(broker_order_id=f"dry-{uuid.uuid4().hex[:12]}", client_order_id=order.client_order_id,
                         symbol=order.symbol, side=order.side, status=OrderStatus.SUBMITTED, qty=order.qty,
                         notional=order.notional, order_type=order.order_type, stop_price=order.stop_price,
                         limit_price=order.limit_price, submitted_at=datetime.now(timezone.utc), raw_status="dry-run")
        self._orders[bo.broker_order_id] = bo
        return bo

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        if broker_order_id in self._orders:
            return self._orders[broker_order_id]
        return self.real.get_order(broker_order_id)

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        bo = self.get_order(broker_order_id)
        return bo.model_copy(update={"client_order_id": client_order_id, "stop_price": stop_price})
