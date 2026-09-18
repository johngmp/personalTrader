"""The interface every broker adapter implements (Alpaca now; OANDA and Liquid in Phase 4b)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from trader.models import Account, AssetClass, BrokerOrder, MarketClock, OrderRequest, Position, Quote


class LiveTradingRefused(RuntimeError):
    """Raised when anything suggests a live (real-money) connection. This plan is paper-only."""


class Broker(ABC):
    name: str

    @property
    @abstractmethod
    def is_paper(self) -> bool: ...

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote: ...

    @abstractmethod
    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder: ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrder: ...

    @abstractmethod
    def get_open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        """Move a resting stop order (trailing stops). The replacement gets `client_order_id`, so its fills
        are still attributed to the right sleeve."""

    @abstractmethod
    def get_clock(self) -> MarketClock: ...

    def order_step(self, symbol: str, asset_class: AssetClass) -> tuple[float, float] | None:
        """(quantity step, minimum quantity) for orders in `symbol`, or None if dollar amounts are accepted."""
        return None
