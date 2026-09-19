"""Shadow broker: forward-test strategies on LIVE prices without sending a single order.

Sleeves with `broker: shadow` run through the normal engine, risk gate and journal, but their "broker"
is this simulator: prices, market hours and order sizes come from the real broker (read-only), fills
happen at the live price, and stops trigger when the live price crosses them. Positions are whatever the
journal says the shadow sleeves hold, so shadow state can never drift from its own records.

Use it to try new ideas cheaply before they earn a real paper-trading slice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from trader.brokers.base import Broker
from trader.config import Settings
from trader.journal.db import Journal
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

NAME = "shadow"


class ShadowBroker(Broker):
    name = NAME

    def __init__(self, real: Broker, journal: Journal, settings: Settings) -> None:
        self.real = real
        self.journal = journal
        self.settings = settings
        self.sleeves = [s for s in settings.sleeves.values() if s.broker == NAME]

    @property
    def is_paper(self) -> bool:
        return True

    # --- reads come from the real broker --------------------------------------------------------

    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        return self.real.get_quote(symbol, asset_class)

    def get_clock(self) -> MarketClock:
        return self.real.get_clock()

    def order_step(self, symbol: str, asset_class: AssetClass) -> tuple[float, float] | None:
        return self.real.order_step(symbol, asset_class)

    # --- virtual account from the journal ---------------------------------------------------------

    def _holdings(self) -> dict[str, float]:
        out: dict[str, float] = {}
        ids = {s.id for s in self.sleeves}
        for (sleeve, symbol), qty in self.journal.all_holdings().items():
            if sleeve in ids:
                out[symbol] = out.get(symbol, 0.0) + qty
        return out

    def get_positions(self) -> list[Position]:
        out = []
        for symbol, qty in self._holdings().items():
            asset_class = self.settings.asset_class(symbol) or AssetClass.US_EQUITY
            price = self.get_quote(symbol, asset_class).price
            out.append(Position(symbol=symbol, asset_class=asset_class, qty=qty, avg_entry_price=price,
                                market_value=qty * price, current_price=price))
        return out

    def get_account(self) -> Account:
        cash = sum(s.capital_usd + self.journal.sleeve_cash_flow(s.id) for s in self.sleeves)
        value = sum(p.market_value for p in self.get_positions())
        return Account(equity=cash + value, last_equity=None, cash=cash, buying_power=cash, status="SHADOW",
                       is_paper=True)

    # --- simulated orders -------------------------------------------------------------------------

    def _fillable_now(self, asset_class: AssetClass) -> bool:
        return asset_class is AssetClass.CRYPTO or self.real.get_clock().is_open

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        bo = BrokerOrder(broker_order_id=f"shadow-{uuid.uuid4().hex[:16]}", client_order_id=order.client_order_id,
                         symbol=order.symbol, side=order.side, status=OrderStatus.SUBMITTED, qty=order.qty,
                         notional=order.notional, order_type=order.order_type,
                         stop_price=order.stop_price if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) else None,
                         limit_price=order.limit_price, submitted_at=datetime.now(timezone.utc), raw_status="accepted")
        if order.order_type is OrderType.MARKET and self._fillable_now(asset_class):
            return self._fill(bo, asset_class)
        return bo  # stops rest; stock market orders wait for the open, like the real broker

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        row = self.journal.conn.execute("SELECT * FROM orders WHERE broker_order_id=?", (broker_order_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown shadow order {broker_order_id}")
        asset_class = AssetClass(row["asset_class"])
        bo = BrokerOrder(broker_order_id=broker_order_id, client_order_id=row["client_order_id"], symbol=row["symbol"],
                         side=Side(row["side"]), status=OrderStatus(row["status"]), qty=row["qty"],
                         notional=row["notional"], order_type=OrderType(row["order_type"]), stop_price=row["stop_price"],
                         limit_price=row["limit_price"], filled_qty=row["filled_qty"],
                         filled_avg_price=row["filled_avg_price"], raw_status=row["broker_status"] or "")
        if not bo.status.is_open or not self._fillable_now(asset_class):
            return bo
        if bo.order_type is OrderType.MARKET:
            return self._fill(bo, asset_class)
        if bo.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and bo.side is Side.SELL:
            price = self.get_quote(bo.symbol, asset_class).price
            if price <= (bo.stop_price or 0):
                return self._fill(bo, asset_class, price)
        return bo

    def _fill(self, bo: BrokerOrder, asset_class: AssetClass, price: float | None = None) -> BrokerOrder:
        price = price or self.get_quote(bo.symbol, asset_class).price
        qty = bo.qty if bo.qty is not None else (bo.notional or 0) / price
        return bo.model_copy(update={"status": OrderStatus.FILLED, "filled_qty": qty, "filled_avg_price": price,
                                     "raw_status": "filled (shadow)"})

    def get_open_orders(self) -> list[BrokerOrder]:
        return [self.get_order(r["broker_order_id"]) for r in self.journal.open_orders(NAME)]

    def cancel_order(self, broker_order_id: str) -> None:
        return None  # the engine marks it canceled in the journal, which is all a shadow order is

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        old = self.get_order(broker_order_id)
        return old.model_copy(update={"broker_order_id": f"shadow-{uuid.uuid4().hex[:16]}",
                                      "client_order_id": client_order_id, "stop_price": stop_price,
                                      "limit_price": limit_price or old.limit_price, "status": OrderStatus.SUBMITTED})
