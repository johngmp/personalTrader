"""Shadow sleeves trade on live prices through the full engine, but never send an order to the real broker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from trader.brokers.shadow import ShadowBroker
from trader.brokers.sim import SimBroker
from trader.config import SleeveConfig
from trader.engine.executor import Executor
from trader.engine.live import Coordinator, Engine
from trader.notify.telegram import NullNotifier

START = {"SPY": 500.0, "QQQ": 450.0, "BTC/USD": 60_000.0}


class Bars:
    def __init__(self) -> None:
        idx = pd.bdate_range(end="2026-09-18", periods=260)
        rng = np.random.default_rng(5)
        self.full = {s: pd.DataFrame({"close": p * np.cumprod(1 + 0.002 + rng.normal(0, 0.003, 260))}, index=idx)
                     for s, p in START.items()}
        for df in self.full.values():
            df["open"], df["high"], df["low"], df["volume"] = df["close"], df["close"] * 1.003, df["close"] * 0.997, 1.0

    def completed_bars(self, symbol, asset_class, now, market):
        return self.full[symbol]


def test_shadow_sleeve_trades_without_touching_the_real_broker(settings, journal):
    settings.sleeves["SX"] = SleeveConfig(id="SX", name="shadow trend", enabled=True, broker="shadow", capital_usd=10_000,
                                          symbols=list(START), strategy="S1",
                                          params={"fast": 20, "slow": 200, "atr_mult": 2.5})
    for sid in ("B0", "S1", "S8"):
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": False})
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    clock = [now]
    real = SimBroker(name="alpaca", crypto_fee_bps=25)
    bars = Bars()
    for s, df in bars.full.items():
        real.set_price(s, float(df["close"].iloc[-1]), now)
    shadow = ShadowBroker(real, journal, settings)
    engines = [Engine(settings, b, journal, bars, notifier=NullNotifier(), clock=lambda: clock[0], fill_wait_s=0,
                      executor=Executor(settings, b, journal, clock=lambda: clock[0], sleep=lambda _: None))
               for b in (real, shadow)]
    Coordinator(settings, journal, engines, NullNotifier(), clock=lambda: clock[0]).run_once()

    held = journal.sleeve_holdings("SX")
    assert set(held) == set(START)                       # the shadow sleeve "bought" everything in trend
    assert real.submitted == [] and real.get_positions() == []   # ...and the real broker saw nothing
    assert {o.symbol for o in shadow.get_open_orders()} == set(START)  # shadow stops are resting
    assert shadow.get_account().status == "SHADOW"

    stop = next(o for o in shadow.get_open_orders() if o.symbol == "QQQ").stop_price
    clock[0] = now + timedelta(hours=1)
    real.set_price("QQQ", stop * 0.98, clock[0])          # the live price falls through the shadow stop
    for s in ("SPY", "BTC/USD"):
        real.set_price(s, float(bars.full[s]["close"].iloc[-1]), clock[0])
    reports = Coordinator(settings, journal, engines, NullNotifier(), clock=lambda: clock[0]).run_once()
    assert "QQQ" not in journal.sleeve_holdings("SX")
    assert any(f.symbol == "QQQ" and f.sleeve_id == "SX" for r in reports for f in r.fills)
    assert real.submitted == []
    assert not any(r.mismatched for r in reports)


def test_one_sleeve_failing_does_not_stop_the_others(settings, journal):
    """Regression: a sleeve with missing price data used to abort every sleeve's decisions in that run."""
    settings.sleeves["SX"] = SleeveConfig(id="SX", name="ok", enabled=True, broker="alpaca", capital_usd=10_000,
                                          symbols=["SPY"], strategy="S1", params={"fast": 20, "slow": 200, "atr_mult": 2.5})
    settings.sleeves["SY"] = SleeveConfig(id="SY", name="broken", enabled=True, broker="alpaca", capital_usd=10_000,
                                          symbols=["QQQ", "EFA"], strategy="S1")
    for sid in ("B0", "S1", "S8"):
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": False})
    for sid in ("SY", "SX"):  # the broken one decides first
        settings.sleeves[sid] = settings.sleeves.pop(sid)
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    real = SimBroker(name="alpaca")
    bars = Bars()  # has no EFA -> SY fails
    for s, df in bars.full.items():
        real.set_price(s, float(df["close"].iloc[-1]), now)
    engine = Engine(settings, real, journal, bars, notifier=NullNotifier(), clock=lambda: now, fill_wait_s=0,
                    executor=Executor(settings, real, journal, clock=lambda: now, sleep=lambda _: None))
    report = engine.run_once()
    assert any(p.startswith("SY decisions failed") for p in report.problems)
    assert "SPY" in journal.sleeve_holdings("SX")
