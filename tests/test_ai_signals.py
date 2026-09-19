"""The AI path: strict signal validation, the S5 approve/veto gate and S6 weekly weights."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trader.brokers.sim import SimBroker
from trader.engine.executor import Executor
from trader.engine.live import Engine
from trader.notify.telegram import NullNotifier
from trader.signals import store as signals
from trader.strategies.ai_portfolio import LARGE_CAPS

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)
TREND = {"BTC/USD": 60_000.0, "ETH/USD": 3_000.0, "QQQ": 450.0, "SPY": 500.0}


class Bars:
    def __init__(self, start: dict[str, float], n: int = 260, end: str = "2026-09-21") -> None:
        idx = pd.bdate_range(end=end, periods=n)
        rng = np.random.default_rng(7)
        self.full = {}
        for s, p0 in start.items():
            close = p0 * np.cumprod(1 + 0.0015 + rng.normal(0, 0.003, n))
            self.full[s] = pd.DataFrame({"open": close, "high": close * 1.003, "low": close * 0.997, "close": close,
                                         "volume": 1.0}, index=idx)

    def completed_bars(self, symbol, asset_class, now, market):
        return self.full[symbol]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def ai_world(settings, tmp_path):
    from trader.journal.db import Journal

    clock = Clock()
    for sid in ("S5", "S6"):
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": True})
    for sid in ("B0", "S1"):  # keep the test focused on the AI sleeves
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": False})
    journal = Journal(tmp_path / "j.db", clock=clock)
    start = {**TREND, **{s: 100.0 + i * 20 for i, s in enumerate(LARGE_CAPS)}}
    bars = Bars(start)
    broker = SimBroker(cash=100_000, crypto_fee_bps=25, name="alpaca")
    for s, df in bars.full.items():
        broker.set_price(s, float(df["close"].iloc[-1]), NOW)
    engine = Engine(settings, broker, journal, bars, notifier=NullNotifier(), clock=clock, fill_wait_s=0,
                    executor=Executor(settings, broker, journal, clock=clock, sleep=lambda _: None))

    def step(hours: float):
        clock.now += timedelta(hours=hours)
        for s, df in bars.full.items():
            broker.set_price(s, float(df["close"].iloc[-1]), clock.now)
        return engine.run_once()

    yield dict(settings=settings, journal=journal, broker=broker, engine=engine, clock=clock, step=step)
    journal.close()


def verdict_signal(clock, bar, items, sleeve="S5") -> dict:
    return {"schema_version": 1, "id": uuid.uuid4().hex, "created_at": clock.now.isoformat(), "sleeve": sleeve,
            "kind": "entry_verdict", "bar_date": bar, "learnings_version": "none",
            "verdicts": [{"symbol": s, "verdict": v, "confidence": 0.7,
                          "reasoning": f"{v} because the analyst team agreed after debate"} for s, v in items]}


def weights_signal(clock, weights: dict[str, float]) -> dict:
    return {"schema_version": 1, "id": uuid.uuid4().hex, "created_at": clock.now.isoformat(), "sleeve": "S6",
            "kind": "target_weights", "bar_date": "2026-09-21", "learnings_version": "none",
            "weights": [{"symbol": s, "weight": w, "score": 0.5, "reasoning": "panel consensus on quality"}
                        for s, w in weights.items()]}


def test_s5_waits_for_the_ai_then_follows_approve_and_veto(ai_world):
    w, j = ai_world, ai_world["journal"]
    w["engine"].run_once()
    pending = signals.pending_requests(j, "S5")
    assert {p["symbol"] for p in pending} == set(TREND)            # the rules want all four; the AI must answer
    assert j.sleeve_holdings("S5") == {}                           # nothing bought without a verdict
    bar = pending[0]["bar_date"]
    assert "indicators" in pending[0]["context"] and pending[0]["context"]["stop"] > 0

    signals.submit(verdict_signal(w["clock"], bar, [("SPY", "approve"), ("QQQ", "veto")]), w["settings"], j,
                   now=w["clock"].now)
    w["step"](0.25)
    held = j.sleeve_holdings("S5")
    assert "SPY" in held and "QQQ" not in held
    assert "AI approved" in j.conn.execute("SELECT reason FROM orders WHERE sleeve_id='S5' AND symbol='SPY'").fetchone()[0]
    assert {p["symbol"] for p in signals.pending_requests(j, "S5")} == {"BTC/USD", "ETH/USD"}

    report = w["step"](9)                                          # no answer for crypto within 8 hours
    assert any("no AI verdict" in a for a in report.actions)
    assert signals.pending_requests(j, "S5") == []
    assert "BTC/USD" not in j.sleeve_holdings("S5")


def test_s6_applies_weekly_weights_once(ai_world):
    w, j = ai_world, ai_world["journal"]
    w["engine"].run_once()
    assert j.sleeve_holdings("S6") == {}                           # no weights yet: nothing
    signals.submit(weights_signal(w["clock"], {"MSFT": 0.15, "JPM": 0.12, "XOM": 0.10}), w["settings"], j,
                   now=w["clock"].now)
    w["step"](0.25)
    held = j.sleeve_holdings("S6")
    assert set(held) == {"MSFT", "JPM", "XOM"} and all(float(q).is_integer() for q in held.values())
    orders_before = len(w["broker"].submitted)
    w["step"](0.25)                                                # applied signals aren't re-applied
    assert len([o for o in w["broker"].submitted[orders_before:] if o.order_type.value == "market"]) == 0
    assert all(o.symbol in held for o in w["broker"].get_open_orders())  # every S6 position has its stop


@pytest.mark.parametrize(("mutate", "why"), [
    (lambda s: s.update(sleeve="S1"), "does not take"),
    (lambda s: s.update(sleeve="NOPE"), "unknown sleeve"),
    (lambda s: s.update(created_at=(NOW - timedelta(hours=3)).isoformat()), "must be recent"),
    (lambda s: s.update(created_at=(NOW + timedelta(hours=1)).isoformat()), "must be recent"),
    (lambda s: s["verdicts"][0].update(symbol="TSLA"), "outside sleeve"),
    (lambda s: s["verdicts"][0].update(verdict="maybe"), "invalid signal"),
    (lambda s: s.update(schema_version=2), "invalid signal"),
    (lambda s: s.update(extra_field=1), "invalid signal"),
])
def test_bad_verdicts_are_rejected(ai_world, mutate, why):
    w, j = ai_world, ai_world["journal"]
    w["engine"].run_once()
    bar = signals.pending_requests(j, "S5")[0]["bar_date"]
    raw = verdict_signal(w["clock"], bar, [("SPY", "approve")])
    mutate(raw)
    with pytest.raises(signals.SignalRejected, match=why):
        signals.submit(raw, w["settings"], j, now=w["clock"].now)
    assert j.conn.execute("SELECT status FROM ai_signals ORDER BY received_at DESC LIMIT 1").fetchone()[0] == "rejected"


def test_verdict_without_a_pending_candidate_and_duplicates_are_rejected(ai_world):
    w, j = ai_world, ai_world["journal"]
    with pytest.raises(signals.SignalRejected, match="no pending candidate"):
        signals.submit(verdict_signal(w["clock"], "2026-09-21", [("SPY", "approve")]), w["settings"], j, now=w["clock"].now)
    w["engine"].run_once()
    bar = signals.pending_requests(j, "S5")[0]["bar_date"]
    good = verdict_signal(w["clock"], bar, [("SPY", "approve")])
    signals.submit(good, w["settings"], j, now=w["clock"].now)
    with pytest.raises(signals.SignalRejected, match="already submitted"):
        signals.submit(good, w["settings"], j, now=w["clock"].now)


def test_weights_must_respect_sleeve_limits(ai_world):
    w, j = ai_world, ai_world["journal"]
    with pytest.raises(signals.SignalRejected, match="weight over 15%"):
        signals.submit(weights_signal(w["clock"], {"MSFT": 0.30}), w["settings"], j, now=w["clock"].now)
    too_much = {s: 0.15 for s in LARGE_CAPS[:7]}  # 105%
    with pytest.raises(signals.SignalRejected, match="add up to"):
        signals.submit(weights_signal(w["clock"], too_much), w["settings"], j, now=w["clock"].now)


def test_a_rejected_signal_can_be_fixed_and_resubmitted_with_the_same_id(ai_world):
    w, j = ai_world, ai_world["journal"]
    w["engine"].run_once()
    bar = signals.pending_requests(j, "S5")[0]["bar_date"]
    raw = verdict_signal(w["clock"], bar, [("SPY", "approve")])
    raw["verdicts"][0]["reasoning"] = "too short"
    with pytest.raises(signals.SignalRejected):
        signals.submit(raw, w["settings"], j, now=w["clock"].now)
    raw["verdicts"][0]["reasoning"] = "approve: trend, news and risk analysts agree after the debate"
    assert signals.submit(raw, w["settings"], j, now=w["clock"].now).id == raw["id"]
