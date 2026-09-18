"""Turns a strategy's desired weight into a position size that respects the sleeve's risk limits.

Used by both the backtester and (from Phase 3) the live engine, so tested sizing is live sizing.
The risk gate re-checks every live order independently.
"""

from __future__ import annotations

from trader.config import SleeveLimits
from trader.strategies.base import Target


def capped_weight(weight: float, price: float, stop: float | None, limits: SleeveLimits) -> float:
    """Largest allowed fraction of sleeve equity for one position.

    - never above max_position_pct_of_sleeve
    - with a stop: the loss if the stop is hit stays within risk_per_trade_pct of the sleeve
    - without a stop: zero when stops are required
    """
    if weight <= 0 or price <= 0:
        return 0.0
    w = min(weight, limits.max_position_pct_of_sleeve / 100)
    if stop is not None and 0 < stop < price:
        risk_fraction_per_unit_weight = (price - stop) / price
        w = min(w, (limits.risk_per_trade_pct / 100) / risk_fraction_per_unit_weight)
    elif limits.require_stop:
        return 0.0
    return max(w, 0.0)


def fit_to_leverage(new_weight: float, other_weights: float, limits: SleeveLimits) -> float:
    """Shrink a new position so the sleeve's total exposure stays within max_leverage."""
    room = limits.max_leverage - other_weights
    return max(0.0, min(new_weight, room))


def plan_weights(targets: dict[str, Target], current: dict[str, float], prices: dict[str, float],
                 limits: SleeveLimits) -> dict[str, float]:
    """Desired weights for every symbol whose position should change today.

    `current` holds each position's weight of sleeve equity now. Exits come back as 0.0; entries and
    resizes are capped by the sleeve limits and then scaled down together so the sleeve never exceeds
    max_leverage. Exits are planned first, so selling one asset frees room to buy another on the same day.
    Symbols that should just keep their position are left out.
    """
    planned = dict(current)
    changeable: dict[str, float] = {}
    exits: dict[str, float] = {}
    for symbol, target in targets.items():
        if target.weight <= 0:
            if current.get(symbol, 0.0) > 0:
                exits[symbol] = 0.0
            planned[symbol] = 0.0
        elif symbol not in current or target.resize:
            changeable[symbol] = capped_weight(target.weight, prices[symbol], target.stop, limits)
    fixed = sum(w for s, w in planned.items() if s not in changeable)
    wanted = sum(changeable.values())
    room = fit_to_leverage(wanted, fixed, limits)
    scale = room / wanted if wanted > 0 else 0.0
    return {**exits, **{s: w * scale for s, w in changeable.items()}}
