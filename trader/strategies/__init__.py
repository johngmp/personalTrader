from trader.strategies.base import Holding, Strategy, Target
from trader.strategies.mean_reversion import MeanReversion
from trader.strategies.rotation import Rotation
from trader.strategies.trend import FXTrend, TrendFollowing

REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in (TrendFollowing, Rotation, MeanReversion, FXTrend)}

__all__ = ["REGISTRY", "Holding", "Strategy", "Target"]
