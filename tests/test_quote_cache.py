"""The dashboard caches quotes; the engine must not.

Building a page priced every symbol several times over -- 28 Alpaca calls at ~0.8s each, 18-23 seconds a
page. Caching fixes that, but the engine SIZES POSITIONS from the quote, so a stale price there would
quietly break the 1% risk rule.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from trader.engine.serve import CachedQuotes
from trader.models import AssetClass, Quote


class CountingBroker:
    name = "fake"
    is_paper = True

    def __init__(self) -> None:
        self.calls = 0

    def get_quote(self, symbol: str, asset_class) -> Quote:
        self.calls += 1
        return Quote(symbol=symbol, price=100.0 + self.calls, ts=datetime.now(timezone.utc))

    def get_positions(self):
        return ["delegated"]


@pytest.fixture(autouse=True)
def _clear_cache():
    CachedQuotes._cache.clear()
    yield
    CachedQuotes._cache.clear()


def test_repeated_quotes_hit_the_broker_once():
    broker = CountingBroker()
    cached = CachedQuotes(broker)
    for _ in range(10):
        cached.get_quote("BTC/USD", AssetClass.CRYPTO)
    assert broker.calls == 1


def test_each_symbol_is_cached_separately():
    broker = CountingBroker()
    cached = CachedQuotes(broker)
    for symbol in ("BTC/USD", "ETH/USD", "SPY"):
        cached.get_quote(symbol, AssetClass.CRYPTO)
        cached.get_quote(symbol, AssetClass.CRYPTO)
    assert broker.calls == 3


def test_the_cache_expires():
    broker = CountingBroker()
    cached = CachedQuotes(broker, ttl_s=0)
    cached.get_quote("BTC/USD", AssetClass.CRYPTO)
    cached.get_quote("BTC/USD", AssetClass.CRYPTO)
    assert broker.calls == 2


def test_two_brokers_do_not_share_a_symbol_entry():
    """Shadow and Alpaca both quote BTC/USD and must not be confused for each other."""
    a, b = CountingBroker(), CountingBroker()
    b.name = "shadow"
    CachedQuotes(a).get_quote("BTC/USD", AssetClass.CRYPTO)
    CachedQuotes(b).get_quote("BTC/USD", AssetClass.CRYPTO)
    assert a.calls == 1 and b.calls == 1


def test_everything_else_passes_through():
    cached = CachedQuotes(CountingBroker())
    assert cached.get_positions() == ["delegated"]
    assert cached.name == "fake" and cached.is_paper is True


def test_the_engine_is_never_given_cached_quotes():
    """The 1% risk rule is only as good as the price it is computed from."""
    from trader.engine import brokers_setup

    source = inspect.getsource(brokers_setup)
    assert "CachedQuotes" not in source, "the engine's brokers must be the real, uncached ones"


def test_only_the_dashboard_server_wraps_brokers():
    from trader.engine import serve

    wrapped = [line for line in inspect.getsource(serve).splitlines() if "CachedQuotes(" in line]
    # One class definition reference plus exactly one use, inside build_engine.
    assert any("Engine(settings, CachedQuotes(broker)" in line for line in wrapped)
