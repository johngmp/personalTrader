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
    # Enable S1 so tests can exercise a normal strategy sleeve ($10k capital), and un-retire it: it was
    # wound down into S8 on 2026-10-08, and a retiring sleeve sells instead of entering, which would
    # silently turn every test that needs an ordinary trading sleeve into a test of the wind-down.
    # Tests that want the retiring behaviour set the flag themselves.
    s.sleeves["S1"] = s.sleeves["S1"].model_copy(update={"enabled": True, "retiring": False})
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


@pytest.fixture(scope="session")
def live_page() -> str:
    """The real dashboard, rendered ONCE for the whole session.

    Four test files each had their own module-scoped copy of this, so the suite built live broker
    engines and rendered the whole page six times over. Every one of those is network work -- Alpaca
    quotes, Yahoo history -- which is slow, repeated for no benefit, and the reason a run could hang for
    minutes when a feed went quiet.

    Session-scoped is safe here because every test using it asserts on the page's STRUCTURE, and none
    of them mutate it.
    """
    from pathlib import Path as _Path

    from trader.config import load_settings
    from trader.engine.brokers_setup import configured_brokers
    from trader.engine.dashboard import dashboard_html
    from trader.engine.live import Engine
    from trader.journal.db import Journal

    settings = load_settings(PROJECT_ROOT)
    journal = Journal(_Path("data/journal.db"))
    engines = [Engine(settings, b, journal, bars) for b, bars in configured_brokers(settings, journal)]
    try:
        return dashboard_html(settings, journal, engines)
    finally:
        journal.close()


@pytest.fixture(autouse=True)
def trial_ledger(tmp_path, monkeypatch):
    """Point the trial ledger at a temp file for every test.

    `run_backtest` records a trial on every call -- that is the point of it -- so without this the suite
    would append thousands of rows to the real ledger and inflate the Deflated Sharpe's N for every
    strategy. Autouse rather than opt-in: a test that forgot it would silently corrupt the real
    research record, which is exactly the kind of "by diligence" failure the ledger exists to stop.
    """
    from trader.research import ledger

    path = tmp_path / "trials.jsonl"
    monkeypatch.setattr(ledger, "TRIAL_LEDGER", path)
    return path
