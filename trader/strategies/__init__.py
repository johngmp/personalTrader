from trader.strategies.ai_portfolio import AIPortfolio
from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.buy_and_hold import BuyAndHold
from trader.strategies.mean_reversion import MeanReversion
from trader.strategies.rotation import Rotation
from trader.strategies.trend import FXTrend, TrendFollowing

# Strategies that go through Gate 1 research.
REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in (TrendFollowing, Rotation, MeanReversion, FXTrend)}
# Everything the live engine can run (research strategies + the buy-and-hold benchmark).
LIVE_REGISTRY: dict[str, type[Strategy]] = {**REGISTRY, BuyAndHold.name: BuyAndHold, AIPortfolio.name: AIPortfolio}

__all__ = ["LIVE_REGISTRY", "REGISTRY", "Holding", "Strategy", "Target"]
