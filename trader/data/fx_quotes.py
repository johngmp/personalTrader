"""Free forex prices from Yahoo, so FX strategies can run without a broker at all.

The shadow broker needs three read-only things from its "real" broker: a price, market hours, and an
order step. None of those require an account, and Yahoo gives daily FX bars and a delayed last price for
nothing. That makes an FX sleeve testable today, with no Spotware approval, no OANDA signup, and no
credentials of any kind.

This is deliberately a dead end for orders: every order method raises. It is a price feed wearing a
Broker's interface so `ShadowBroker` can wrap it, never a route to a market.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from trader.brokers.base import Broker, LiveTradingRefused
from trader.brokers.oanda import fx_clock
from trader.data.history import YAHOO_TICKERS
from trader.models import Account, AssetClass, BrokerOrder, MarketClock, OrderRequest, Position, Quote

from zoneinfo import ZoneInfo

NAME = "yahoo-fx"
EASTERN = ZoneInfo("America/New_York")
FX_DAY_CLOSE = time(17, 0)   # the forex day rolls at 5pm New York
QUOTE_CACHE_S = 60          # one engine run asks for the same symbol several times
FX_UNIT_STEP = (1.0, 1.0)   # whole units of the base currency


class FXQuoteError(RuntimeError):
    pass


class YahooFXFeed(Broker):
    """Prices only. Wrapped by ShadowBroker; it can never place an order."""

    name = NAME

    def __init__(self, clock=lambda: datetime.now(timezone.utc)) -> None:
        self._clock = clock
        self._cache: dict[str, tuple[datetime, Quote]] = {}

    @property
    def is_paper(self) -> bool:
        return True

    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        now = self._clock()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < timedelta(seconds=QUOTE_CACHE_S):
            return cached[1]
        quote = self._fetch(symbol, now)
        self._cache[symbol] = (now, quote)
        return quote

    def _fetch(self, symbol: str, now: datetime) -> Quote:
        import yfinance as yf

        ticker = YAHOO_TICKERS.get(symbol)
        if ticker is None:
            raise FXQuoteError(f"no Yahoo ticker mapped for {symbol}")
        handle = yf.Ticker(ticker)
        # Minute bars while the market is open. Over the weekend, and for gold (whose Yahoo feed has no
        # intraday history), fall back to the last daily close -- which genuinely IS the last traded price.
        frame = handle.history(period="1d", interval="1m")
        daily = frame.empty
        if daily:
            frame = handle.history(period="5d", interval="1d")
        if frame.empty:
            raise FXQuoteError(f"Yahoo returned no recent prices for {symbol} ({ticker})")
        # The bar's own timestamp, not `now`: a stale feed must look stale to the risk gate rather than
        # being quietly stamped as fresh. A DAILY bar is stamped at the forex close (5pm New York) rather
        # than at midnight, because that is when the price it carries was actually last traded.
        ts = frame.index[-1].to_pydatetime()
        if daily:
            ts = datetime.combine(ts.date(), FX_DAY_CLOSE, tzinfo=EASTERN)
        ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
        price = float(frame["Close"].iloc[-1])
        if price <= 0:
            raise FXQuoteError(f"Yahoo returned a non-positive price for {symbol} ({ticker})")
        return Quote(symbol=symbol, price=price, bid=None, ask=None, ts=ts)

    def get_clock(self) -> MarketClock:
        return fx_clock(self._clock())

    def order_step(self, symbol: str, asset_class: AssetClass) -> tuple[float, float] | None:
        return FX_UNIT_STEP

    # --- there is no account and no way to trade -------------------------------------------------

    def _refuse(self) -> None:
        raise LiveTradingRefused(
            f"{NAME} is a price feed, not a broker. It cannot hold an account or place an order."
        )

    def get_account(self) -> Account:
        self._refuse()
        raise AssertionError  # unreachable; keeps the return type honest

    def get_positions(self) -> list[Position]:
        return []  # ShadowBroker derives positions from the journal, never from here

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        self._refuse()
        raise AssertionError

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        self._refuse()
        raise AssertionError

    def get_open_orders(self) -> list[BrokerOrder]:
        return []

    def cancel_order(self, broker_order_id: str) -> None:
        self._refuse()

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        self._refuse()
        raise AssertionError
