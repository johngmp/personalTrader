"""OANDA adapter (against a mock OANDA API), forex clock, candles, and two brokers side by side."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from trader.brokers.base import LiveTradingRefused
from trader.brokers.oanda import OandaBroker, OandaError, fx_clock, parse_time
from trader.brokers.sim import SimBroker
from trader.data.oanda_bars import candles_to_frame
from trader.engine.executor import Executor
from trader.engine.live import Coordinator, Engine
from trader.models import AssetClass, OrderRequest, OrderStatus, OrderType, Side
from trader.notify.telegram import NullNotifier


class FakeOanda:
    """Just enough of the v20 API to exercise the adapter."""

    def __init__(self, currency: str = "USD") -> None:
        self.currency = currency
        self.orders: dict[str, dict] = {}
        self.next_id = 100
        self.requests: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.split("/v3/accounts/ACC")[-1]
        self.requests.append((request.method, path, body))
        if path == "/summary":
            return httpx.Response(200, json={"account": {"currency": self.currency, "NAV": "100050.5",
                                                         "balance": "100000", "marginAvailable": "99000"}})
        if path == "/instruments":
            return httpx.Response(200, json={"instruments": [
                {"name": "EUR_USD", "displayPrecision": 5, "tradeUnitsPrecision": 0, "minimumTradeSize": "1"}]})
        if path == "/pricing":
            return httpx.Response(200, json={"prices": [{"instrument": "EUR_USD", "time": "2026-09-18T07:00:00.123456789Z",
                                                         "bids": [{"price": "1.17000"}], "asks": [{"price": "1.17010"}]}]})
        if path == "/openPositions":
            return httpx.Response(200, json={"positions": [{"instrument": "EUR_USD",
                                                            "long": {"units": "2000", "averagePrice": "1.16", "unrealizedPL": "20"},
                                                            "short": {"units": "0", "averagePrice": "0", "unrealizedPL": "0"}}]})
        if path == "/orders" and request.method == "POST":
            order = body["order"]
            self.next_id += 1
            oid = str(self.next_id)
            if order["type"] == "MARKET":
                if order["units"] == "999999999":
                    return httpx.Response(201, json={"orderCreateTransaction": {"id": oid},
                                                     "orderCancelTransaction": {"reason": "INSUFFICIENT_MARGIN"}})
                return httpx.Response(201, json={"orderCreateTransaction": {"id": oid, "time": "2026-09-18T07:00:00Z"},
                                                 "orderFillTransaction": {"units": order["units"], "price": "1.17010"}})
            self.orders[oid] = {"id": oid, **order, "state": "PENDING", "createTime": "2026-09-18T07:00:00Z"}
            return httpx.Response(201, json={"orderCreateTransaction": {"id": oid}})
        if path.startswith("/orders/") and path.endswith("/cancel"):
            self.orders[path.split("/")[2]]["state"] = "CANCELLED"
            return httpx.Response(200, json={"orderCancelTransaction": {"reason": "CLIENT_REQUEST"}})
        if path.startswith("/orders/") and request.method == "PUT":
            old = path.split("/")[2]
            self.orders[old]["state"] = "CANCELLED"
            self.next_id += 1
            new = str(self.next_id)
            self.orders[new] = {"id": new, **body["order"], "state": "PENDING"}
            return httpx.Response(201, json={"orderCreateTransaction": {"id": new}})
        if path.startswith("/orders/"):
            return httpx.Response(200, json={"order": self.orders[path.split("/")[2]]})
        if path == "/pendingOrders":
            return httpx.Response(200, json={"orders": [o for o in self.orders.values() if o["state"] == "PENDING"]})
        return httpx.Response(404, json={"errorMessage": f"unknown path {path}"})


@pytest.fixture
def oanda():
    fake = FakeOanda()
    client = httpx.Client(base_url="https://api-fxpractice.oanda.com", transport=httpx.MockTransport(fake.handler))
    return OandaBroker("TOKEN", "ACC", client=client), fake


def test_account_positions_and_quote(oanda):
    broker, _ = oanda
    acct = broker.get_account()
    assert acct.equity == 100050.5 and acct.last_equity is None and acct.is_paper
    (pos,) = broker.get_positions()
    assert pos.symbol == "EUR/USD" and pos.qty == 2000 and pos.market_value == pytest.approx(2340)
    q = broker.get_quote("EUR/USD", AssetClass.FOREX)
    assert q.price == pytest.approx(1.17005) and q.ts.microsecond == 123456
    assert broker.order_step("EUR/USD", AssetClass.FOREX) == (1.0, 1.0)


def test_market_buy_fills_and_stop_is_reduce_only(oanda):
    broker, fake = oanda
    bo = broker.submit_order(OrderRequest(sleeve_id="S4", symbol="EUR/USD", side=Side.BUY, qty=2000, stop_price=1.15),
                             AssetClass.FOREX)
    assert bo.status is OrderStatus.FILLED and bo.filled_qty == 2000 and bo.filled_avg_price == pytest.approx(1.1701)
    stop = broker.submit_order(OrderRequest(sleeve_id="S4", symbol="EUR/USD", side=Side.SELL, qty=2000,
                                            order_type=OrderType.STOP, stop_price=1.15), AssetClass.FOREX)
    sent = fake.requests[-1][2]["order"]
    assert sent["type"] == "STOP" and sent["units"] == "-2000" and sent["positionFill"] == "REDUCE_ONLY"
    assert sent["price"] == "1.15000" and sent["timeInForce"] == "GTC"
    assert stop.status is OrderStatus.SUBMITTED
    moved = broker.replace_stop(stop.broker_order_id, "pt-s4-new", 1.16)
    assert moved.broker_order_id != stop.broker_order_id and moved.client_order_id == "pt-s4-new"
    assert fake.orders[stop.broker_order_id]["state"] == "CANCELLED"
    assert [o.broker_order_id for o in broker.get_open_orders()] == [moved.broker_order_id]


def test_rejected_order_and_non_usd_account(oanda):
    broker, fake = oanda
    bo = broker.submit_order(OrderRequest(sleeve_id="S4", symbol="EUR/USD", side=Side.BUY, qty=999999999,
                                          stop_price=1.1), AssetClass.FOREX)
    assert bo.status is OrderStatus.REJECTED and "INSUFFICIENT_MARGIN" in bo.raw_status
    fake.currency = "AUD"
    with pytest.raises(OandaError, match="USD practice account"):
        broker.get_account()


def test_live_setups_are_refused(monkeypatch):
    monkeypatch.setenv("OANDA_ENV", "live")
    with pytest.raises(LiveTradingRefused):
        OandaBroker("TOKEN", "ACC")
    monkeypatch.delenv("OANDA_ENV")
    live = httpx.Client(base_url="https://api-fxtrade.oanda.com")
    with pytest.raises(LiveTradingRefused):
        OandaBroker("TOKEN", "ACC", client=live)


def test_fx_clock_weekend_and_week():
    ny = timezone(timedelta(hours=-4))
    sat = fx_clock(datetime(2026, 9, 19, 12, 0, tzinfo=ny))
    assert not sat.is_open and sat.next_open.astimezone(ny).strftime("%a %H:%M") == "Sun 17:00"
    assert sat.last_close.astimezone(ny).strftime("%a %H:%M") == "Fri 17:00"
    wed = fx_clock(datetime(2026, 9, 16, 12, 0, tzinfo=ny))
    assert wed.is_open and wed.last_close.astimezone(ny).strftime("%a %H:%M") == "Tue 17:00"
    fri_late = fx_clock(datetime(2026, 9, 18, 18, 0, tzinfo=ny))
    assert not fri_late.is_open


def test_candles_use_only_complete_days_labelled_by_close_date():
    candles = [{"complete": True, "time": "2026-09-16T21:00:00.000000000Z", "volume": 10,
                "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"}},
               {"complete": False, "time": "2026-09-17T21:00:00.000000000Z", "volume": 3,
                "mid": {"o": "1.15", "h": "1.16", "l": "1.14", "c": "1.155"}}]
    df = candles_to_frame(candles)
    assert list(df.index) == [pd.Timestamp("2026-09-17")] and df["close"].iloc[0] == 1.15
    assert parse_time("1726642800.000000000").year == 2024


# --- two brokers side by side ---------------------------------------------------------------------

FX = {"EUR/USD": 1.10, "AUD/USD": 0.66, "GBP/USD": 1.27, "XAU/USD": 2600.0}
STOCKS_CRYPTO = {"BTC/USD": 60_000.0, "ETH/USD": 3_000.0, "QQQ": 450.0, "SPY": 500.0}


class Bars:
    def __init__(self, start: dict[str, float], n: int = 260) -> None:
        idx = pd.bdate_range("2025-01-01", periods=n)
        rng = np.random.default_rng(3)
        self.full = {}
        for s, p0 in start.items():
            close = p0 * np.cumprod(1 + 0.0015 + rng.normal(0, 0.003, n))
            self.full[s] = pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998, "close": close,
                                         "volume": 1.0}, index=idx)

    def completed_bars(self, symbol, asset_class, now, market):
        return self.full[symbol]


def test_two_brokers_each_trade_only_their_own_sleeves(settings, journal):
    # Pin the broker here rather than inheriting it from config/sleeves.yaml: this test is about routing
    # sleeves to the right broker, and it shouldn't break when a sleeve is repointed at a different one.
    s4 = settings.sleeves["S4"].model_copy(update={"enabled": True, "broker": "oanda",
                                                   "params": {"fast": 50, "slow": 150, "atr_mult": 3.5}})
    settings.sleeves["S4"] = s4
    # Same reason the broker is pinned above: this test is about routing, and S8's 40-market basket is
    # not priced by either fake world here.
    settings.sleeves["S8"] = settings.sleeves["S8"].model_copy(update={"enabled": False})
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    alpaca, oanda = SimBroker(name="alpaca", crypto_fee_bps=25), SimBroker(name="oanda")  # like the real brokers
    for broker, start, bars in ((alpaca, STOCKS_CRYPTO, None), (oanda, FX, None)):
        for s, p in Bars(start).full.items():
            broker.set_price(s, float(p["close"].iloc[-1]), now)
    notifier = NullNotifier()
    engines = [Engine(settings, b, journal, Bars(start), notifier=notifier, clock=lambda: now, fill_wait_s=0,
                      executor=Executor(settings, b, journal, clock=lambda: now, sleep=lambda _: None))
               for b, start in ((alpaca, STOCKS_CRYPTO), (oanda, FX))]
    reports = Coordinator(settings, journal, engines, notifier, clock=lambda: now).run_once()
    problems = [p for r in reports for p in r.problems]
    assert not problems, problems

    assert {p.symbol for p in oanda.get_positions()} <= set(FX)
    assert {p.symbol for p in alpaca.get_positions()} <= set(STOCKS_CRYPTO)
    s4_held = journal.sleeve_holdings("S4")
    assert s4_held and all(float(q).is_integer() for q in s4_held.values())      # whole FX units
    assert {o.symbol for o in oanda.get_open_orders()} == set(s4_held)            # every S4 position has a stop
    assert all(o.order_type is OrderType.STOP for o in oanda.get_open_orders())

    # A second round: nothing is flagged as a mismatch across brokers.
    again = Coordinator(settings, journal, engines, notifier, clock=lambda: now).run_once()
    assert [r.mismatched for r in again] == [set(), set()]
    page = (settings.root / "data" / "dashboard.html").read_text()
    assert "OANDA (forex)" in page and "S4" in page
