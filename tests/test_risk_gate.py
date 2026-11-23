"""Every risk limit in config/risk.yaml, tested against the S1 sleeve ($10k capital)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from trader.models import Account, AssetClass, MarketClock, OrderRequest, Position, Quote, Side
from trader.risk.gate import RecentOrder, RiskContext, RiskGate

from .conftest import NOW

BTC = 60_000.0


def ctx(**overrides) -> RiskContext:
    base = dict(
        now=NOW,
        broker_is_paper=True,
        account=Account(equity=100_000, last_equity=100_000, cash=100_000, buying_power=100_000, is_paper=True),
        positions=[],
        quote=Quote(symbol="BTC/USD", price=BTC, ts=NOW),
        asset_class=AssetClass.CRYPTO,
        sleeve_holdings={},
        peak_equity=100_000,
        orders_today=0,
    )
    base.update(overrides)
    return RiskContext(**base)


def buy(notional=2_000.0, stop=BTC * 0.98, sleeve="S1", symbol="BTC/USD") -> OrderRequest:
    return OrderRequest(sleeve_id=sleeve, symbol=symbol, side=Side.BUY, notional=notional, stop_price=stop)


def sell(qty: float, sleeve="S1", symbol="BTC/USD") -> OrderRequest:
    return OrderRequest(sleeve_id=sleeve, symbol=symbol, side=Side.SELL, qty=qty)


def failed(decision) -> set[str]:
    return {c.name for c in decision.failures}


@pytest.fixture
def gate(settings) -> RiskGate:
    return RiskGate(settings)


def test_normal_entry_passes_every_check(gate):
    d = gate.check(buy(), ctx())
    assert d.approved, d.summary()
    assert not d.halt_account


def test_kill_switch_blocks_entries_and_exits(gate):
    held = {"BTC/USD": 0.01}
    assert failed(gate.check(buy(), ctx(kill_switch_active=True))) == {"kill_switch"}
    assert failed(gate.check(sell(0.01), ctx(kill_switch_active=True, sleeve_holdings=held))) == {"kill_switch"}


def test_live_account_or_broker_is_refused(gate):
    live = Account(equity=100_000, last_equity=100_000, cash=0, buying_power=0, is_paper=False)
    assert "paper_only" in failed(gate.check(buy(), ctx(account=live)))
    assert "paper_only" in failed(gate.check(buy(), ctx(broker_is_paper=False)))


def test_live_mode_in_config_is_refused(settings):
    live_settings = settings.model_copy(update={"risk": settings.risk.model_copy(update={"mode": "live"})})
    assert "paper_only" in failed(RiskGate(live_settings).check(buy(), ctx()))


def test_unknown_or_disabled_sleeve_is_blocked(gate):
    assert "sleeve_known" in failed(gate.check(buy(sleeve="NOPE"), ctx()))
    spy = ctx(quote=Quote(symbol="SPY", price=500, ts=NOW), asset_class=AssetClass.US_EQUITY)
    assert "sleeve_enabled" in failed(gate.check(buy(sleeve="S2", symbol="SPY", stop=490), spy))
    assert "sleeve_enabled" in failed(gate.check(buy(), ctx(sleeve_disabled=True)))


def test_symbol_must_be_in_universe_and_sleeve(gate):
    efa = ctx(quote=Quote(symbol="EFA", price=80, ts=NOW), asset_class=AssetClass.US_EQUITY)
    assert failed(gate.check(buy(symbol="EFA", stop=78), efa)) == {"symbol_in_sleeve"}
    doge = ctx(quote=Quote(symbol="DOGE/USD", price=0.1, ts=NOW), asset_class=None)
    assert {"symbol_in_universe", "symbol_in_sleeve"} <= failed(gate.check(buy(symbol="DOGE/USD", stop=0.09), doge))


def test_stale_or_missing_price_is_blocked(gate):
    old = Quote(symbol="BTC/USD", price=BTC, ts=NOW - timedelta(minutes=10))
    assert failed(gate.check(buy(), ctx(quote=old))) == {"fresh_price"}
    zero = Quote(symbol="BTC/USD", price=0, ts=NOW)
    assert not gate.check(buy(), ctx(quote=zero)).approved


def test_entry_needs_a_stop_below_price(gate):
    assert "stop_required" in failed(gate.check(buy(stop=None), ctx()))
    assert "stop_required" in failed(gate.check(buy(stop=BTC * 1.01), ctx()))


def test_risk_per_trade_limit(gate):
    # $2,000 with a stop 10% away risks $200, over 1% of $10k = $100.
    assert failed(gate.check(buy(stop=BTC * 0.90), ctx())) == {"risk_per_trade"}
    # Same size with a 4% stop risks $80: allowed.
    assert gate.check(buy(stop=BTC * 0.96), ctx()).approved


def test_max_position_per_sleeve(gate):
    # 25% of $10k = $2,500 per position.
    assert "max_position_of_sleeve" in failed(gate.check(buy(notional=3_000, stop=BTC * 0.99), ctx()))
    held = {"BTC/USD": 2_000 / BTC}
    assert "max_position_of_sleeve" in failed(gate.check(buy(notional=1_000, stop=BTC * 0.99), ctx(sleeve_holdings=held)))


def test_sleeve_cannot_exceed_its_capital(gate):
    spy = Position(symbol="SPY", asset_class=AssetClass.US_EQUITY, qty=18, avg_entry_price=500,
                   market_value=9_000, current_price=500)
    c = ctx(positions=[spy], sleeve_holdings={"SPY": 18})
    assert "sleeve_leverage" in failed(gate.check(buy(notional=1_500, stop=BTC * 0.99), c))


def test_account_gross_exposure_limit(gate):
    big = Position(symbol="SPY", asset_class=AssetClass.US_EQUITY, qty=198, avg_entry_price=500,
                   market_value=99_000, current_price=500)
    assert "gross_exposure" in failed(gate.check(buy(), ctx(positions=[big])))


def test_correlated_exposure_limit(gate):
    eth = Position(symbol="ETH/USD", asset_class=AssetClass.CRYPTO, qty=13, avg_entry_price=3_000,
                   market_value=39_000, current_price=3_000)
    assert failed(gate.check(buy(), ctx(positions=[eth]))) == {"correlated_exposure[crypto]"}


def test_daily_loss_blocks_entries_but_not_exits(gate):
    down = Account(equity=96_000, last_equity=100_000, cash=96_000, buying_power=96_000, is_paper=True)
    assert failed(gate.check(buy(), ctx(account=down))) == {"daily_loss_limit"}
    assert gate.check(sell(0.01), ctx(account=down, sleeve_holdings={"BTC/USD": 0.01})).approved


def test_drawdown_limit_blocks_and_requests_halt(gate):
    d = gate.check(buy(), ctx(peak_equity=120_000))  # 100k is 16.7% below the 120k peak
    assert "drawdown_halt" in failed(d)
    assert d.halt_account
    assert not gate.check(buy(), ctx(peak_equity=115_000)).halt_account  # 13% below: fine


def test_no_short_selling(gate):
    assert failed(gate.check(sell(0.02), ctx(sleeve_holdings={"BTC/USD": 0.01}))) == {"no_short_selling"}
    assert gate.check(sell(0.01), ctx(sleeve_holdings={"BTC/USD": 0.01})).approved


def test_duplicate_orders_within_window_are_blocked(gate):
    recent = [RecentOrder("S1", "BTC/USD", Side.BUY, NOW - timedelta(seconds=30))]
    assert failed(gate.check(buy(), ctx(recent_orders=recent))) == {"no_duplicate"}
    older = [RecentOrder("S1", "BTC/USD", Side.BUY, NOW - timedelta(seconds=120))]
    assert gate.check(buy(), ctx(recent_orders=older)).approved
    other_sleeve = [RecentOrder("TEST", "BTC/USD", Side.BUY, NOW - timedelta(seconds=30))]
    assert gate.check(buy(), ctx(recent_orders=other_sleeve)).approved


def test_max_orders_per_day(gate, settings):
    """The boundary comes from the configured cap, not a literal.

    This hardcoded 60 and 59, so raising max_orders_per_day to 150 on 2026-10-08 failed a test about
    whether the rule works at its boundary. The rule is "at the cap you are refused, one below you are
    not", and that is true at any cap.
    """
    cap = settings.risk.account.max_orders_per_day
    assert failed(gate.check(buy(), ctx(orders_today=cap))) == {"max_orders_per_day"}
    assert gate.check(buy(), ctx(orders_today=cap - 1)).approved


def test_max_orders_per_day_blocks_entries_but_not_exits(gate, settings):
    """A protective stop is an exit. It must never be refused for burning the day's order budget.

    An entry and its stop are separate orders in the same engine cycle, with all sleeves' entries
    going out before that cycle's stops. If the account-wide cap applied to exits too, a busy day
    could exhaust the budget on entries and leave real positions with no stop for the rest of the
    day. So, like daily_loss_limit/drawdown_halt, this guard only constrains entries.
    """
    cap = settings.risk.account.max_orders_per_day
    held = {"BTC/USD": 0.01}
    assert gate.check(sell(0.01), ctx(orders_today=cap, sleeve_holdings=held)).approved
    assert gate.check(sell(0.01), ctx(orders_today=cap + 50, sleeve_holdings=held)).approved
    assert failed(gate.check(buy(), ctx(orders_today=cap))) == {"max_orders_per_day"}


def test_every_failure_is_reported_not_just_the_first(gate, settings):
    cap = settings.risk.account.max_orders_per_day
    d = gate.check(buy(notional=5_000, stop=None),
                   ctx(kill_switch_active=True, orders_today=cap + 39))
    assert {"kill_switch", "max_orders_per_day", "max_position_of_sleeve", "stop_required"} <= failed(d)


def test_stock_orders_after_the_close_use_the_latest_session_price(gate):
    closed = MarketClock(is_open=False, last_close=NOW - timedelta(hours=2), next_open=NOW + timedelta(hours=15))
    spy = dict(asset_class=AssetClass.US_EQUITY, market=closed)
    at_close = Quote(symbol="SPY", price=500, ts=NOW - timedelta(hours=2, minutes=1))
    assert gate.check(buy(symbol="SPY", stop=490), ctx(quote=at_close, **spy)).approved
    day_before = Quote(symbol="SPY", price=500, ts=NOW - timedelta(hours=26))
    assert failed(gate.check(buy(symbol="SPY", stop=490), ctx(quote=day_before, **spy))) == {"fresh_price"}
