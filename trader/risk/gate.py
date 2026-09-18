"""The risk gate: plain, deterministic checks that every order must pass before it reaches a broker.

No AI is involved here. Every check runs (not just the first failure) so the journal records
exactly why an order was blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trader.config import Settings
from trader.models import Account, AssetClass, CheckResult, OrderRequest, Position, Quote, RiskDecision, Side


@dataclass
class RecentOrder:
    sleeve_id: str
    symbol: str
    side: Side
    ts: datetime


@dataclass
class RiskContext:
    """Everything the gate needs to know about the world at the moment an order is proposed."""

    now: datetime
    broker_is_paper: bool
    account: Account
    positions: list[Position]
    quote: Quote
    asset_class: AssetClass | None
    sleeve_holdings: dict[str, float]          # this sleeve's qty per symbol, from the journal
    peak_equity: float
    orders_today: int
    recent_orders: list[RecentOrder] = field(default_factory=list)
    kill_switch_active: bool = False
    kill_switch_reason: str = ""
    sleeve_disabled: bool = False


def _pct(value: float, of: float) -> float:
    return 100.0 * value / of if of else 0.0


class RiskGate:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        s = self.settings
        risk = s.risk
        sleeve = s.sleeves.get(order.sleeve_id)
        checks: list[CheckResult] = []

        def add(name: str, passed: bool, detail: str = "") -> None:
            checks.append(CheckResult(name=name, passed=passed, detail="" if passed else detail))

        # --- hard stops -------------------------------------------------------------------
        add("kill_switch", not ctx.kill_switch_active, f"kill switch is on ({ctx.kill_switch_reason})")
        add("paper_only", risk.mode == "paper" and ctx.broker_is_paper and ctx.account.is_paper,
            "only paper trading is allowed in this phase")
        add("sleeve_known", sleeve is not None, f"unknown sleeve {order.sleeve_id!r}")
        if sleeve is None:
            return RiskDecision(approved=False, checks=checks)
        limits = s.sleeve_limits(sleeve.id)
        add("sleeve_enabled", sleeve.enabled and not ctx.sleeve_disabled, f"sleeve {sleeve.id} is disabled")
        add("symbol_in_universe", ctx.asset_class is not None, f"{order.symbol} is not in universe.yaml")
        add("symbol_in_sleeve", order.symbol in sleeve.symbols, f"{order.symbol} is not in sleeve {sleeve.id}'s symbols")

        # --- price data ---------------------------------------------------------------------
        price = ctx.quote.price
        age = (ctx.now - ctx.quote.ts).total_seconds()
        add("fresh_price", 0 < price and age <= risk.guards.stale_data_seconds,
            f"price is {age:.0f}s old (limit {risk.guards.stale_data_seconds}s); market may be closed")
        if price <= 0:
            return RiskDecision(approved=False, checks=checks)

        qty = order.qty if order.qty is not None else order.notional / price  # type: ignore[operator]
        notional = qty * price
        held = ctx.sleeve_holdings.get(order.symbol, 0.0)
        is_entry = order.side is Side.BUY

        # --- exits: long-only, can't sell more than the sleeve holds -------------------------
        if not is_entry:
            add("no_short_selling", qty <= held * (1 + 1e-9),
                f"sleeve {sleeve.id} holds {held:g} {order.symbol}, cannot sell {qty:g}")

        # --- activity limits ------------------------------------------------------------------
        add("max_orders_per_day", ctx.orders_today < risk.account.max_orders_per_day,
            f"{ctx.orders_today} orders today (limit {risk.account.max_orders_per_day})")
        window = risk.guards.duplicate_order_window_s
        dupes = [o for o in ctx.recent_orders
                 if o.sleeve_id == order.sleeve_id and o.symbol == order.symbol and o.side is order.side
                 and 0 <= (ctx.now - o.ts).total_seconds() <= window]
        add("no_duplicate", not dupes, f"same {order.side.value} {order.symbol} order within the last {window}s")

        # --- account-level loss limits (block entries, always allow exits) -------------------
        halt = False
        if is_entry:
            day_pnl_pct = _pct(ctx.account.equity - ctx.account.last_equity, ctx.account.last_equity)
            add("daily_loss_limit", day_pnl_pct > -risk.account.max_daily_loss_pct,
                f"account is {day_pnl_pct:.2f}% today (limit -{risk.account.max_daily_loss_pct}%)")
            peak = max(ctx.peak_equity, ctx.account.equity)
            drawdown_pct = _pct(ctx.account.equity - peak, peak)
            halt = drawdown_pct <= -risk.account.max_drawdown_halt_pct
            add("drawdown_halt", not halt,
                f"account is {drawdown_pct:.2f}% below its peak (limit -{risk.account.max_drawdown_halt_pct}%)")

        # --- sizing (entries only) --------------------------------------------------------------
        if is_entry:
            capital = sleeve.capital_usd
            sleeve_exposure = sum(abs(q) * self._price(sym, ctx) for sym, q in ctx.sleeve_holdings.items())
            position_after = (held * price) + notional
            add("max_position_of_sleeve", position_after <= capital * limits.max_position_pct_of_sleeve / 100 + 1e-6,
                f"{order.symbol} would be ${position_after:,.2f}, over {limits.max_position_pct_of_sleeve}% "
                f"of sleeve capital ${capital:,.0f}")
            add("sleeve_leverage", sleeve_exposure + notional <= capital * limits.max_leverage + 1e-6,
                f"sleeve exposure would be ${sleeve_exposure + notional:,.2f}, over {limits.max_leverage}x "
                f"its ${capital:,.0f} capital")

            if limits.require_stop:
                stop = order.stop_price
                add("stop_required", stop is not None and 0 < stop < price,
                    "entries need a stop price below the current price")
                if stop is not None and 0 < stop < price:
                    trade_risk = (price - stop) * qty
                    risk_budget = capital * limits.risk_per_trade_pct / 100
                    add("risk_per_trade", trade_risk <= risk_budget + 1e-6,
                        f"risking ${trade_risk:,.2f} to the stop, over {limits.risk_per_trade_pct}% of "
                        f"sleeve capital (${risk_budget:,.2f})")

            equity = ctx.account.equity
            gross_after = sum(abs(p.market_value) for p in ctx.positions) + notional
            add("gross_exposure", gross_after <= equity * risk.account.max_gross_exposure_pct / 100 + 1e-6,
                f"account exposure would be {_pct(gross_after, equity):.1f}% of equity "
                f"(limit {risk.account.max_gross_exposure_pct}%)")

            for group, members in s.universe.correlation_groups.items():
                if order.symbol not in members:
                    continue
                group_after = sum(abs(p.market_value) for p in ctx.positions if p.symbol in members) + notional
                limit = risk.account.max_correlated_exposure_pct
                add(f"correlated_exposure[{group}]", group_after <= equity * limit / 100 + 1e-6,
                    f"'{group}' exposure would be {_pct(group_after, equity):.1f}% of equity (limit {limit}%)")

        approved = all(c.passed for c in checks)
        return RiskDecision(approved=approved, checks=checks, halt_account=halt)

    @staticmethod
    def _price(symbol: str, ctx: RiskContext) -> float:
        if symbol == ctx.quote.symbol:
            return ctx.quote.price
        for p in ctx.positions:
            if p.symbol == symbol:
                return p.current_price
        return 0.0
