"""OANDA v20 adapter for forex and gold. Practice (demo) accounts only.

- Only the practice API host is ever contacted; a live token can't authenticate there.
- The account must be in USD, because the journal values positions as units x USD price
  (so only USD-quoted instruments like EUR/USD, GBP/USD, AUD/USD and XAU/USD are traded).
- Protective stops are OANDA STOP orders with positionFill=REDUCE_ONLY, so a stop can only close
  a position, never open a short.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

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

PRACTICE_URL = "https://api-fxpractice.oanda.com"
EASTERN = ZoneInfo("America/New_York")
_STATES = {"PENDING": OrderStatus.SUBMITTED, "TRIGGERED": OrderStatus.SUBMITTED, "FILLED": OrderStatus.FILLED,
           "CANCELLED": OrderStatus.CANCELED}
_TYPES = {"MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT, "STOP": OrderType.STOP}


def to_instrument(symbol: str) -> str:
    return symbol.replace("/", "_")


def from_instrument(instrument: str) -> str:
    return instrument.replace("_", "/")


def parse_time(value: str | None) -> datetime | None:
    """OANDA sends RFC3339 with nanoseconds or Unix seconds, depending on account settings."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00" if frac else f"{head}+00:00")


class OandaError(RuntimeError):
    pass


class OandaBroker(Broker):
    name = "oanda"

    def __init__(self, token: str, account_id: str, client: httpx.Client | None = None) -> None:
        if os.environ.get("OANDA_ENV", "practice").strip().lower() not in ("practice", "demo", ""):
            raise LiveTradingRefused("OANDA_ENV is not 'practice'. This system only trades on OANDA demo accounts.")
        self.account_id = account_id
        self._client = client or httpx.Client(base_url=PRACTICE_URL, timeout=15,
                                              headers={"Authorization": f"Bearer {token}",
                                                       "Accept-Datetime-Format": "RFC3339"})
        if "fxpractice" not in str(self._client.base_url):
            raise LiveTradingRefused(f"OANDA client points at {self._client.base_url}, not the practice API.")
        self._instruments: dict[str, dict] = {}
        self._checked_currency = False

    # --- helpers ------------------------------------------------------------------------------

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self._client.request(method, f"/v3/accounts/{self.account_id}{path}", **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("errorMessage", r.text)
            except ValueError:
                msg = r.text
            raise OandaError(f"OANDA {r.status_code}: {msg}")
        return r.json()

    def _instrument(self, symbol: str) -> dict:
        inst = to_instrument(symbol)
        if inst not in self._instruments:
            data = self._req("GET", "/instruments", params={"instruments": inst})
            for i in data.get("instruments", []):
                self._instruments[i["name"]] = i
        if inst not in self._instruments:
            raise OandaError(f"{symbol} is not tradeable on this OANDA account")
        return self._instruments[inst]

    def _fmt_price(self, symbol: str, price: float) -> str:
        return f"{price:.{int(self._instrument(symbol).get('displayPrecision', 5))}f}"

    def _fmt_units(self, symbol: str, units: float) -> str:
        precision = int(self._instrument(symbol).get("tradeUnitsPrecision", 0))
        return f"{units:.{precision}f}"

    # --- Broker interface -----------------------------------------------------------------------

    @property
    def is_paper(self) -> bool:
        return True

    def get_account(self) -> Account:
        a = self._req("GET", "/summary")["account"]
        if a.get("currency") != "USD":
            raise OandaError(f"This OANDA account is in {a.get('currency')}; use a USD practice account "
                             "(OANDA Hub → add a practice sub-account with currency USD).")
        nav = float(a["NAV"])
        return Account(equity=nav, last_equity=None, cash=float(a["balance"]),
                       buying_power=float(a.get("marginAvailable", nav)), currency="USD",
                       status="ACTIVE", is_paper=True)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._req("GET", "/openPositions").get("positions", []):
            for side in ("long", "short"):
                units = float(p[side]["units"])
                if units == 0:
                    continue
                avg = float(p[side]["averagePrice"])
                value = units * avg + float(p[side].get("unrealizedPL", 0.0))
                out.append(Position(symbol=from_instrument(p["instrument"]), asset_class=AssetClass.FOREX, qty=units,
                                    avg_entry_price=avg, market_value=value, current_price=value / units))
        return out

    def get_quote(self, symbol: str, asset_class: AssetClass) -> Quote:
        prices = self._req("GET", "/pricing", params={"instruments": to_instrument(symbol)}).get("prices", [])
        if not prices:
            raise OandaError(f"no price for {symbol}")
        p = prices[0]
        bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
        return Quote(symbol=symbol, price=(bid + ask) / 2, bid=bid, ask=ask, ts=parse_time(p["time"]))  # type: ignore[arg-type]

    def order_step(self, symbol: str, asset_class: AssetClass) -> tuple[float, float] | None:
        inst = self._instrument(symbol)
        step = 10.0 ** -int(inst.get("tradeUnitsPrecision", 0))
        return step, float(inst.get("minimumTradeSize", step))

    def submit_order(self, order: OrderRequest, asset_class: AssetClass) -> BrokerOrder:
        if order.qty is None:
            raise OandaError("OANDA orders need a unit quantity, not a dollar amount")
        units = order.qty if order.side is Side.BUY else -order.qty
        body: dict = {"instrument": to_instrument(order.symbol), "units": self._fmt_units(order.symbol, units),
                      "positionFill": "REDUCE_ONLY" if order.side is Side.SELL else "DEFAULT",
                      "clientExtensions": {"id": order.client_order_id, "tag": order.sleeve_id,
                                           "comment": order.reason[:100]}}
        if order.order_type is OrderType.MARKET:
            body.update(type="MARKET", timeInForce="FOK")
        elif order.order_type is OrderType.LIMIT:
            body.update(type="LIMIT", timeInForce="GTC", price=self._fmt_price(order.symbol, order.limit_price))  # type: ignore[arg-type]
        else:  # STOP / STOP_LIMIT -> OANDA STOP (priceBound acts as the limit)
            body.update(type="STOP", timeInForce="GTC", price=self._fmt_price(order.symbol, order.stop_price))  # type: ignore[arg-type]
            if order.order_type is OrderType.STOP_LIMIT and order.limit_price:
                body["priceBound"] = self._fmt_price(order.symbol, order.limit_price)
        data = self._req("POST", "/orders", json={"order": body})
        created = data.get("orderCreateTransaction", {})
        base = BrokerOrder(broker_order_id=str(created.get("id", "")), client_order_id=order.client_order_id,
                           symbol=order.symbol, side=order.side, status=OrderStatus.SUBMITTED, qty=order.qty,
                           order_type=order.order_type, stop_price=order.stop_price, limit_price=order.limit_price,
                           submitted_at=parse_time(created.get("time")), raw_status="PENDING")
        if "orderFillTransaction" in data:
            fill = data["orderFillTransaction"]
            return base.model_copy(update={"status": OrderStatus.FILLED, "filled_qty": abs(float(fill["units"])),
                                           "filled_avg_price": float(fill["price"]), "raw_status": "FILLED"})
        if "orderCancelTransaction" in data:
            reason = data["orderCancelTransaction"].get("reason", "cancelled")
            return base.model_copy(update={"status": OrderStatus.REJECTED, "raw_status": f"CANCELLED: {reason}"})
        return base

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        o = self._req("GET", f"/orders/{broker_order_id}")["order"]
        units = float(o.get("units", 0))
        status = _STATES.get(o["state"], OrderStatus.SUBMITTED)
        filled_qty, price = 0.0, None
        if o["state"] == "FILLED" and o.get("fillingTransactionID"):
            tx = self._req("GET", f"/transactions/{o['fillingTransactionID']}")["transaction"]
            filled_qty, price = abs(float(tx.get("units", units))), float(tx["price"])
        return BrokerOrder(broker_order_id=str(o["id"]),
                           client_order_id=o.get("clientExtensions", {}).get("id", ""),
                           symbol=from_instrument(o["instrument"]), side=Side.BUY if units > 0 else Side.SELL,
                           status=status, qty=abs(units), order_type=_TYPES.get(o["type"], OrderType.MARKET),
                           stop_price=float(o["price"]) if o.get("price") else None, filled_qty=filled_qty,
                           filled_avg_price=price, submitted_at=parse_time(o.get("createTime")), raw_status=o["state"])

    def get_open_orders(self) -> list[BrokerOrder]:
        out = []
        for o in self._req("GET", "/pendingOrders").get("orders", []):
            if "instrument" not in o:  # trade-attached orders (we don't use them)
                continue
            units = float(o.get("units", 0))
            out.append(BrokerOrder(broker_order_id=str(o["id"]), client_order_id=o.get("clientExtensions", {}).get("id", ""),
                                   symbol=from_instrument(o["instrument"]), side=Side.BUY if units > 0 else Side.SELL,
                                   status=OrderStatus.SUBMITTED, qty=abs(units),
                                   order_type=_TYPES.get(o["type"], OrderType.MARKET),
                                   stop_price=float(o["price"]) if o.get("price") else None, raw_status=o["state"]))
        return out

    def cancel_order(self, broker_order_id: str) -> None:
        self._req("PUT", f"/orders/{broker_order_id}/cancel")

    def replace_stop(self, broker_order_id: str, client_order_id: str, stop_price: float,
                     limit_price: float | None = None) -> BrokerOrder:
        old = self.get_order(broker_order_id)
        body = {"type": "STOP", "instrument": to_instrument(old.symbol), "timeInForce": "GTC",
                "units": self._fmt_units(old.symbol, -(old.qty or 0)), "positionFill": "REDUCE_ONLY",
                "price": self._fmt_price(old.symbol, stop_price),
                "clientExtensions": {"id": client_order_id, "comment": "protective stop (trailed up)"}}
        if limit_price:
            body["priceBound"] = self._fmt_price(old.symbol, limit_price)
        created = self._req("PUT", f"/orders/{broker_order_id}", json={"order": body}).get("orderCreateTransaction", {})
        return old.model_copy(update={"broker_order_id": str(created.get("id", "")), "client_order_id": client_order_id,
                                      "stop_price": stop_price, "status": OrderStatus.SUBMITTED, "raw_status": "PENDING"})

    def get_clock(self) -> MarketClock:
        return fx_clock(datetime.now(timezone.utc))


def fx_clock(now: datetime) -> MarketClock:
    """Forex trades from Sunday 5pm to Friday 5pm New York time; each day closes at 5pm New York."""
    ny = now.astimezone(EASTERN)
    today_close = ny.replace(hour=17, minute=0, second=0, microsecond=0)
    weekday = ny.weekday()  # Mon=0 .. Sun=6
    closed = weekday == 5 or (weekday == 4 and ny >= today_close) or (weekday == 6 and ny < today_close)
    last_close = today_close if ny >= today_close else today_close - timedelta(days=1)
    while last_close.weekday() in (5, 6):  # no daily close on Saturday or Sunday
        last_close -= timedelta(days=1)
    if closed:
        days_to_sunday = (6 - weekday) % 7
        next_open = today_close + timedelta(days=days_to_sunday)
    else:
        next_open = now.astimezone(EASTERN)
    return MarketClock(is_open=not closed, last_close=last_close.astimezone(timezone.utc),
                       next_open=next_open.astimezone(timezone.utc))
