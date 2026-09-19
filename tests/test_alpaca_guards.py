"""The Alpaca adapter must refuse anything that looks like live trading. No network calls here."""

import pytest

from trader.brokers.alpaca import AlpacaBroker, check_paper_credentials, from_alpaca_symbol
from trader.brokers.base import LiveTradingRefused
from trader.models import AssetClass


def test_live_key_prefix_is_refused():
    with pytest.raises(LiveTradingRefused, match="LIVE key"):
        check_paper_credentials("AKXXXXXXXXXXXXXX")


def test_paper_trade_flag_false_is_refused(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    with pytest.raises(LiveTradingRefused):
        check_paper_credentials("PKXXXXXXXXXXXXXX")


def test_paper_key_builds_a_paper_client(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    broker = AlpacaBroker("PKXXXXXXXXXXXXXX", "secret")
    assert broker.is_paper


@pytest.mark.parametrize(
    ("raw", "asset_class", "expected"),
    [
        ("BTCUSD", AssetClass.CRYPTO, "BTC/USD"),
        ("ETH/USD", AssetClass.CRYPTO, "ETH/USD"),
        ("SOLUSDT", AssetClass.CRYPTO, "SOL/USDT"),
        ("SPY", AssetClass.US_EQUITY, "SPY"),
    ],
)
def test_crypto_symbols_are_normalised(raw, asset_class, expected):
    assert from_alpaca_symbol(raw, asset_class) == expected


def test_every_alpaca_request_gets_a_timeout(monkeypatch):
    """Regression: alpaca-py has no request timeout; a stuck request once froze a run for ~17 minutes."""
    from requests.adapters import HTTPAdapter

    from trader.brokers.alpaca import HTTP_TIMEOUT_S

    import requests

    seen = []

    def fake_send(self, request, **kw):
        seen.append(kw.get("timeout"))
        response = requests.Response()
        response.status_code, response._content, response.url = 200, b"{}", request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    broker = AlpacaBroker("PKXXXXXXXXXXXXXX", "secret")
    for client in (broker._trading, broker._stocks, broker._crypto):
        client._session.send(requests.Request("GET", "https://paper-api.alpaca.markets/v2/clock").prepare())
    assert seen == [HTTP_TIMEOUT_S] * 3
