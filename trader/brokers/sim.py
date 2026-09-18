"""In-memory broker for tests: fills market orders instantly at the current price."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from trader.brokers.base import Broker
from trader.models import (
    Account,
    AssetClass,
    BrokerOrder,
    MarketClock,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)


class SimBroker(Broker):
    name = "sim"

    def __init__(self, cash: float = 100_000.0, last_equity: float | None = None, fill_immediately: bool = True,
                 crypto_fee_bps: float = 0.0, name: str = "sim"):
        self.name = name  # tests can stand in for a real broker, e.g. name="alpaca"
        self.cash = cash
        self.crypto_fee = crypto_fee_bps / 10_000  # like Alpaca: taken in coins on buys, USD on sells
        self.last_equity = cash if last_equity is None else last_equity
        self.fill_immediately = fill_immediately
        self.prices: dict[str, Quote] = {}
        self.holdings: dict[str, tuple[AssetClass, float, float]] = {}  # symbol -> (class, qty, avg price)
        self.orders: dict[str, BrokerOrder] = {}
        self.order_classes: dict[str, AssetClass] = {}
        self.submitted: list[OrderRequest] = []
        self.clock = MarketClock(is_open=True, last_close=datetime.now(timezone.utc) - timedelta(hours=20),
                                 next_open=datetime.now(timezone.utc) + timedelta(hours=4))

    @property
    def is_paper(self) -> bool:
        return True

    def set_price(self, symbol: str, price: float, ts: datetime | None = None) -> None:
        """Move the price; resting stop orders at or above it trigger (like a broker-held stop)."""
        self.prices[symbol] = Quote(symbol=symbol, price=price, ts=ts or datetime.now(timezone.utc))
        for o in list(self.orders.values()):
            if (o.symbol == symbol and o.status is OrderStatus.SUBMITTED and o.side is Side.SELL
                    and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and price <= (o.stop_price or 0)):
                self._fill(o, self.order_classes[o.broker_order_id])

    def set_position(self, symbol: str, asset_class: AssetClass, qty: float, avg_price: float) -> None:
        self.holdings[symbol] = (asset_class, qty, avg_price)

    def get_account(self) -> Account:
        equity = self.cash + sum(p.market_value for p in self.get_positions())
        return Account(equity=equity, last_equity=self.last_equity, cash=self.cash, buying_power=self.cash, is_paper=True)

    def get_positions(self) -> list[Position]:
        out = []
        for symbol, (asset_class, qty, avg) in self.holdings.items():
            if qty == 0:
                continue
            price = self.prices[symbol].price if symbol in self.prices else avg
            out.append(
                Position(symbol=symbol, asset_class=asset_class, qty=qty, avg_entry_price=avg,
                         market_value=qty * price, current_price=price)
            )
        return out

    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        return self.prices[symbol]

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        self.submitted.append(order)
        broker_order = BrokerOrder(
            broker_order_id=uuid.uuid4().hex, client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side, status=OrderStatus.SUBMITTED, qty=order.qty, notional=order.notional,
            order_type=order.order_type, stop_price=order.stop_price if order.order_type in (
                OrderType.STOP, OrderType.STOP_LIMIT) else None, limit_price=order.limit_price,
            submitted_at=datetime.now(timezone.utc), raw_status="accepted",
        )
        resting = order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
        if self.fill_immediately and not resting:
            self._fill(broker_order, asset_class)
        self.orders[broker_order.broker_order_id] = broker_order
        self.order_classes[broker_order.broker_order_id] = asset_class
        return broker_order

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self.orders[broker_order_id]

    def get_open_orders(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if o.status.is_open]

    def cancel_order(self, broker_order_id: str) -> None:
        o = self.orders[broker_order_id]
        if o.status.is_open:
            o.status, o.raw_status = OrderStatus.CANCELED, "canceled"

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        old = self.orders[broker_order_id]
        old.status, old.raw_status = OrderStatus.CANCELED, "replaced"
        new = old.model_copy(update={"broker_order_id": uuid.uuid4().hex, "stop_price": stop_price,
                                     "client_order_id": client_order_id,
                                     "limit_price": limit_price or old.limit_price,
                                     "status": OrderStatus.SUBMITTED, "raw_status": "accepted"})
        self.orders[new.broker_order_id] = new
        self.order_classes[new.broker_order_id] = self.order_classes[broker_order_id]
        return new

    def get_clock(self) -> MarketClock:
        return self.clock

    def fill_pending(self) -> None:
        """Fill waiting market/limit orders (e.g. stock orders queued overnight)."""
        for o in list(self.orders.values()):
            if o.status is OrderStatus.SUBMITTED and o.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
                self._fill(o, self.order_classes[o.broker_order_id])

    def _fill(self, o: BrokerOrder, asset_class: AssetClass) -> None:
        price = self.prices[o.symbol].price
        qty = o.qty if o.qty is not None else (o.notional or 0) / price
        _, held, avg = self.holdings.get(o.symbol, (asset_class, 0.0, 0.0))
        fee = self.crypto_fee if asset_class is AssetClass.CRYPTO else 0.0
        if o.side is Side.BUY:
            received = qty * (1 - fee)
            new_qty = held + received
            avg = (held * avg + received * price) / new_qty
            self.cash -= qty * price
        else:
            if qty > held + 1e-12:
                raise RuntimeError(f"insufficient qty: holding {held}, selling {qty}")
            new_qty = held - qty
            self.cash += qty * price * (1 - fee)
        self.holdings[o.symbol] = (asset_class, new_qty, avg)
        o.status, o.filled_qty, o.filled_avg_price, o.raw_status = OrderStatus.FILLED, qty, price, "filled"
