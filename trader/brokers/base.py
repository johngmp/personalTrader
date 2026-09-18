"""The interface every broker adapter implements (Alpaca now; OANDA and Liquid in Phase 4b)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from trader.models import Account, AssetClass, BrokerOrder, OrderRequest, Position, Quote


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
