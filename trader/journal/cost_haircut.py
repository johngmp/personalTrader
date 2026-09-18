"""Estimated real-world trading costs that paper fills leave out (spread, slippage, fees)."""

from __future__ import annotations

from trader.config import CostModel


def cost_haircut_usd(notional_usd: float, model: CostModel) -> float:
    """Cost of one side of a trade (entry or exit) in USD."""
    return abs(notional_usd) * model.total_bps / 10_000
