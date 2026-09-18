from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.brokers.sim import SimBroker
from trader.config import PROJECT_ROOT, Settings, load_settings
from trader.journal.db import Journal

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """The real config/ files, with the kill switch and journal redirected to a temp folder."""
    s = load_settings(PROJECT_ROOT)
    s = s.model_copy(update={"root": tmp_path})
    # Enable S1 so tests can exercise a normal strategy sleeve ($10k capital).
    s.sleeves["S1"] = s.sleeves["S1"].model_copy(update={"enabled": True})
    return s


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db", clock=lambda: NOW)
    yield j
    j.close()


@pytest.fixture
def sim() -> SimBroker:
    b = SimBroker(cash=100_000)
    b.set_price("BTC/USD", 60_000, NOW)
    b.set_price("ETH/USD", 3_000, NOW)
    b.set_price("SPY", 500, NOW)
    b.set_price("QQQ", 450, NOW)
    return b
