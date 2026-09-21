"""Core domain objects shared by brokers, the risk gate, the journal and strategies."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class AssetClass(str, Enum):
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"
    FOREX = "forex"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"              # sell when price falls to stop_price (stocks)
    STOP_LIMIT = "stop_limit"  # at stop_price, sell with a limit_price floor (crypto)


class OrderStatus(str, Enum):
    PROPOSED = "proposed"      # created, not yet checked
    REJECTED = "rejected"      # blocked by the risk gate or the broker
    SUBMITTED = "submitted"    # accepted by the broker, not filled yet
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"      # canceled or expired at the broker

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)


def new_client_order_id(sleeve_id: str) -> str:
    return f"pt-{sleeve_id.lower()}-{uuid.uuid4().hex[:16]}"


class OrderRequest(BaseModel):
    """An order a sleeve wants to place. Exactly one of qty or notional is set."""

    sleeve_id: str
    symbol: str
    side: Side
    qty: float | None = None
    notional: float | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    reason: str = ""
    is_test: bool = False
    client_order_id: str = ""
    # For entries, stop_price is the protective stop the risk check uses. For STOP / STOP_LIMIT
    # orders it is the trigger price of the order itself.

    @model_validator(mode="after")
    def _check(self) -> OrderRequest:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("set exactly one of qty or notional")
        if (self.qty or self.notional or 0) <= 0:
            raise ValueError("qty/notional must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and not self.limit_price:
            raise ValueError(f"{self.order_type.value} orders need a limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and not self.stop_price:
            raise ValueError(f"{self.order_type.value} orders need a stop_price (the trigger)")
        if not self.client_order_id:
            self.client_order_id = new_client_order_id(self.sleeve_id)
        return self


def price_decimals(price: float, asset_class: AssetClass) -> int:
    """How many decimals a price in this market is quoted to.

    Stocks and crypto are cents. Forex is not: rounding EUR/USD's 0.69823 to 0.70 moves the stop 18 pips,
    which silently changes the risk the gate approved. Gold trades in the thousands and quotes to cents;
    JPY pairs sit in the hundreds and quote to three places; the majors quote to five.
    """
    if asset_class is not AssetClass.FOREX:
        return 2
    if price >= 1000:
        return 2
    if price >= 100:
        return 3
    return 5


def round_price(price: float, asset_class: AssetClass) -> float:
    return round(price, price_decimals(price, asset_class))


def format_price(price: float, asset_class: AssetClass) -> str:
    return f"{price:,.{price_decimals(price, asset_class)}f}"


class Quote(BaseModel):
    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    ts: datetime


class Account(BaseModel):
    equity: float
    last_equity: float | None   # equity at the previous close (None if the broker doesn't report it)
    cash: float
    buying_power: float
    currency: str = "USD"
    status: str = ""
    is_paper: bool


class MarketClock(BaseModel):
    """US stock market session state (crypto trades around the clock)."""

    is_open: bool
    last_close: datetime       # end of the most recent regular session
    next_open: datetime


class Position(BaseModel):
    symbol: str
    asset_class: AssetClass
    qty: float
    avg_entry_price: float
    market_value: float
    current_price: float


class BrokerOrder(BaseModel):
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    status: OrderStatus
    order_type: OrderType = OrderType.MARKET
    stop_price: float | None = None
    limit_price: float | None = None
    qty: float | None = None
    notional: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    raw_status: str = ""


class Fill(BaseModel):
    client_order_id: str
    sleeve_id: str
    symbol: str
    side: Side
    qty: float                   # quantity filled at the broker
    price: float
    fee_qty: float = 0.0         # broker fee taken in the asset (crypto buys): you hold qty - fee_qty
    broker_fee_usd: float = 0.0  # every broker-charged fee, valued in USD
    cost_haircut_usd: float      # estimated extra costs paper trading leaves out
    ts: datetime

    @property
    def net_qty(self) -> float:
        return self.qty - self.fee_qty


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class RiskDecision(BaseModel):
    approved: bool
    checks: list[CheckResult] = Field(default_factory=list)
    halt_account: bool = False   # drawdown limit hit: engage the kill switch

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
