"""Turns a strategy's desired weight into a position size that respects the sleeve's risk limits.

Used by both the backtester and (from Phase 3) the live engine, so tested sizing is live sizing.
The risk gate re-checks every live order independently.
"""

from __future__ import annotations

from trader.config import SleeveLimits


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
