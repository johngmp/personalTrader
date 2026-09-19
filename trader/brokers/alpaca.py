"""Alpaca adapter. Paper trading only: live keys and live endpoints are refused."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.enums import OrderType as AlpacaOrderType
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)

from trader.brokers.base import Broker, LiveTradingRefused
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

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
EASTERN = ZoneInfo("America/New_York")

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,   # still live until Alpaca completes the replacement
    "replaced": OrderStatus.CANCELED,           # final: superseded by the replacement order
    "pending_cancel": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.CANCELED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "stopped": OrderStatus.CANCELED,
    "suspended": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}

_ORDER_TYPES = {"market": OrderType.MARKET, "limit": OrderType.LIMIT, "stop": OrderType.STOP,
                "stop_limit": OrderType.STOP_LIMIT}

_QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BTC")


def check_paper_credentials(api_key: str) -> None:
    """Refuse anything that looks like a live setup before a single request is sent."""
    if api_key.upper().startswith("AK"):
        raise LiveTradingRefused(
            "This looks like an Alpaca LIVE key (starts with 'AK'). Use your PAPER keys (start with 'PK')."
        )
    if os.environ.get("ALPACA_PAPER_TRADE", "true").strip().lower() in ("false", "0", "no"):
        raise LiveTradingRefused("ALPACA_PAPER_TRADE is set to false. This system only trades on paper.")


HTTP_TIMEOUT_S = 20


def with_timeout(client, seconds: float = HTTP_TIMEOUT_S):
    """alpaca-py sends requests with NO timeout, so one stuck connection can freeze the engine (it happened on
    2026-09-19: a price request hung for ~17 minutes). Give every request on this client a time limit."""
    from requests.adapters import HTTPAdapter

    class _TimeoutAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = seconds
            return super().send(request, **kwargs)

    session = getattr(client, "_session", None)
    if session is not None:
        adapter = _TimeoutAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return client


def _retry_reads(fn):
    """Retry a READ-ONLY call once after a short pause if the connection drops (never used for orders)."""
    import functools
    import time

    import requests

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(2)
            return fn(*args, **kwargs)
    return wrapper


def time_in_force(order: OrderRequest, asset_class: AssetClass) -> TimeInForce:
    """Crypto: GTC. Stocks: GTC for whole-share limit/stop orders (so stops persist), otherwise DAY
    (Alpaca requires DAY for market and fractional orders)."""
    if asset_class is AssetClass.CRYPTO:
        return TimeInForce.GTC
    whole = order.qty is not None and float(order.qty).is_integer()
    if whole and order.order_type in (OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT):
        return TimeInForce.GTC
    return TimeInForce.DAY


def from_alpaca_symbol(symbol: str, asset_class: AssetClass) -> str:
    """Alpaca reports crypto positions as 'BTCUSD'; orders and our config use 'BTC/USD'."""
    if asset_class is not AssetClass.CRYPTO or "/" in symbol:
        return symbol
    for quote in _QUOTE_CURRENCIES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}/{quote}"
    return symbol


def _asset_class(alpaca_asset_class: object) -> AssetClass:
    value = getattr(alpaca_asset_class, "value", str(alpaca_asset_class))
    return AssetClass.CRYPTO if value == "crypto" else AssetClass.US_EQUITY


def _f(value: object) -> float | None:
    return None if value in (None, "") else float(value)  # type: ignore[arg-type]


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str) -> None:
        check_paper_credentials(api_key)
        self._trading = with_timeout(TradingClient(api_key, secret_key, paper=True))
        base_url = getattr(self._trading, "_base_url", PAPER_BASE_URL)
        base_url = str(getattr(base_url, "value", base_url))
        if "paper" not in base_url:
            raise LiveTradingRefused(f"Alpaca client points at {base_url}, not the paper endpoint.")
        self._stocks = with_timeout(StockHistoricalDataClient(api_key, secret_key))
        self._crypto = with_timeout(CryptoHistoricalDataClient(api_key, secret_key))

    @property
    def is_paper(self) -> bool:
        return True

    @_retry_reads
    def get_account(self) -> Account:
        a = self._trading.get_account()
        return Account(
            equity=float(a.equity),
            last_equity=float(a.last_equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            currency=a.currency or "USD",
            status=str(getattr(a.status, "value", a.status)),
            is_paper=True,
        )

    @_retry_reads
    def get_positions(self) -> list[Position]:
        positions = []
        for p in self._trading.get_all_positions():
            asset_class = _asset_class(p.asset_class)
            positions.append(
                Position(
                    symbol=from_alpaca_symbol(p.symbol, asset_class),
                    asset_class=asset_class,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    market_value=float(p.market_value or 0),
                    current_price=float(p.current_price or 0),
                )
            )
        return positions

    @_retry_reads
    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        if asset_class is AssetClass.CRYPTO:
            q = self._crypto.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
            t = self._crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
        elif asset_class is AssetClass.US_EQUITY:
            q = self._stocks.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
            t = self._stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
        else:
            raise ValueError(f"Alpaca does not trade {asset_class.value}")
        bid, ask = _f(q.bid_price), _f(q.ask_price)
        if bid and ask and ask >= bid:
            price, ts = (bid + ask) / 2, q.timestamp
        else:  # one-sided or crossed quote: fall back to the last trade
            price, ts = float(t.price), t.timestamp
        return Quote(symbol=symbol, price=price, bid=bid, ask=ask, ts=ts or datetime.now(timezone.utc))

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        common = dict(
            symbol=order.symbol,
            qty=order.qty,
            notional=order.notional,
            side=OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL,
            time_in_force=time_in_force(order, asset_class),
            client_order_id=order.client_order_id,
        )
        if order.order_type is OrderType.LIMIT:
            request = LimitOrderRequest(type=AlpacaOrderType.LIMIT, limit_price=order.limit_price, **common)
        elif order.order_type is OrderType.STOP:
            request = StopOrderRequest(type=AlpacaOrderType.STOP, stop_price=order.stop_price, **common)
        elif order.order_type is OrderType.STOP_LIMIT:
            request = StopLimitOrderRequest(type=AlpacaOrderType.STOP_LIMIT, stop_price=order.stop_price,
                                            limit_price=order.limit_price, **common)
        else:
            request = MarketOrderRequest(type=AlpacaOrderType.MARKET, **common)
        return self._convert(self._trading.submit_order(request), asset_class)

    @_retry_reads
    def get_open_orders(self) -> list[BrokerOrder]:
        orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [self._convert(o, _asset_class(o.asset_class)) for o in orders]

    def cancel_order(self, broker_order_id: str) -> None:
        self._trading.cancel_order_by_id(broker_order_id)

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        o = self._trading.replace_order_by_id(
            broker_order_id, ReplaceOrderRequest(stop_price=round(stop_price, 2), client_order_id=client_order_id,
                                                 limit_price=round(limit_price, 2) if limit_price else None))
        return self._convert(o, _asset_class(o.asset_class))

    def order_step(self, symbol: str, asset_class: AssetClass) -> tuple[float, float] | None:
        # Whole shares for stocks, so protective stops can rest at Alpaca as GTC orders; crypto by dollar amount.
        return (1.0, 1.0) if asset_class is AssetClass.US_EQUITY else None

    @_retry_reads
    def get_clock(self) -> MarketClock:
        clock = self._trading.get_clock()
        today = clock.timestamp.astimezone(EASTERN).date()
        sessions = self._trading.get_calendar(GetCalendarRequest(start=today - timedelta(days=10), end=today))
        now = clock.timestamp.astimezone(timezone.utc)
        closes = [s.close.replace(tzinfo=EASTERN).astimezone(timezone.utc) for s in sessions]
        last_close = max((c for c in closes if c <= now), default=now - timedelta(days=1))
        return MarketClock(is_open=clock.is_open, last_close=last_close,
                           next_open=clock.next_open.astimezone(timezone.utc))

    @_retry_reads
    def get_order(self, broker_order_id: str) -> BrokerOrder:
        o = self._trading.get_order_by_id(broker_order_id)
        return self._convert(o, _asset_class(o.asset_class))

    @staticmethod
    def _convert(o: Any, asset_class: AssetClass) -> BrokerOrder:
        raw = str(getattr(o.status, "value", o.status))
        side = getattr(o.side, "value", o.side)
        return BrokerOrder(
            broker_order_id=str(o.id),
            client_order_id=o.client_order_id,
            symbol=from_alpaca_symbol(o.symbol, asset_class),
            side=Side(side),
            status=_STATUS_MAP.get(raw, OrderStatus.SUBMITTED),
            order_type=_ORDER_TYPES.get(str(getattr(o.order_type, "value", o.order_type)), OrderType.MARKET),
            stop_price=_f(o.stop_price),
            limit_price=_f(o.limit_price),
            qty=_f(o.qty),
            notional=_f(o.notional),
            filled_qty=_f(o.filled_qty) or 0.0,
            filled_avg_price=_f(o.filled_avg_price),
            submitted_at=o.submitted_at,
            raw_status=raw,
        )
