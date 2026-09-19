"""Loads and validates the YAML files in config/ and secrets from .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.models import AssetClass

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in YAML fail loudly


class AccountLimits(_Strict):
    max_gross_exposure_pct: float = 100
    max_daily_loss_pct: float = 3
    max_drawdown_halt_pct: float = 15
    max_correlated_exposure_pct: float = 40
    max_orders_per_day: int = 60


class SleeveLimits(_Strict):
    max_position_pct_of_sleeve: float = 25
    risk_per_trade_pct: float = 1
    max_leverage: float = 1.0
    require_stop: bool = True
    max_drawdown_disable_pct: float = 25


class AISignalLimits(_Strict):
    inbox: str = "signals/inbox"
    schema_version: int = 1
    max_age_minutes: int = 90
    allowed_symbols_only: bool = True
    ai_sleeve_capital_pct: float = 5


class Guards(_Strict):
    kill_switch_file: str = "KILL_SWITCH"
    refuse_if_paper_flag_but_live_endpoint: bool = True
    stale_data_seconds: int = 300
    duplicate_order_window_s: int = 60


class RiskConfig(_Strict):
    mode: Literal["paper", "live"] = "paper"
    autonomy: Literal["propose_only", "approve_each", "auto"] = "auto"
    account: AccountLimits = Field(default_factory=AccountLimits)
    sleeve_defaults: SleeveLimits = Field(default_factory=SleeveLimits)
    ai_signals: AISignalLimits = Field(default_factory=AISignalLimits)
    guards: Guards = Field(default_factory=Guards)


class SleeveConfig(_Strict):
    id: str
    name: str
    enabled: bool = False
    broker: Literal["alpaca", "oanda", "liquid", "shadow"] = "alpaca"  # shadow = live prices, no orders sent
    capital_usd: float
    symbols: list[str]
    benchmark: str | None = None
    limits: SleeveLimits | None = None  # per-sleeve override of sleeve_defaults
    strategy: str | None = None         # strategy code the live engine runs (defaults to the sleeve id)
    params: dict[str, float | int | str] = Field(default_factory=dict)  # settings chosen by Gate 1
    # AI involvement (Phase 4): "analyst-team" = rule entries need an AI approve/veto verdict first;
    # "investor-panel" = target weights come from the weekly AI investor panel.
    ai: Literal["analyst-team", "investor-panel"] | None = None


class UniverseConfig(_Strict):
    symbols: dict[str, AssetClass]
    correlation_groups: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _groups_use_known_symbols(self) -> UniverseConfig:
        for group, members in self.correlation_groups.items():
            unknown = set(members) - set(self.symbols)
            if unknown:
                raise ValueError(f"correlation group {group!r} has unknown symbols {sorted(unknown)}")
        return self


class CostModel(_Strict):
    half_spread_bps: float
    slippage_bps: float
    fee_bps: float                # estimated fees the paper broker does NOT charge
    broker_fee_bps: float = 0.0   # fees the broker really charges, taken from what you receive

    @property
    def total_bps(self) -> float:
        """Estimated costs added on top of paper fills (broker-charged fees are already in the fill)."""
        return self.half_spread_bps + self.slippage_bps + self.fee_bps


class Settings(BaseModel):
    """Everything loaded from config/, cross-checked for consistency."""

    root: Path
    risk: RiskConfig
    sleeves: dict[str, SleeveConfig]
    universe: UniverseConfig
    costs: dict[AssetClass, CostModel]

    @model_validator(mode="after")
    def _cross_check(self) -> Settings:
        for sleeve in self.sleeves.values():
            unknown = set(sleeve.symbols) - set(self.universe.symbols)
            if unknown:
                raise ValueError(f"sleeve {sleeve.id} uses symbols missing from universe.yaml: {sorted(unknown)}")
        missing_costs = {self.universe.symbols[s] for s in self.universe.symbols} - set(self.costs)
        if missing_costs:
            raise ValueError(f"costs.yaml has no entry for {sorted(c.value for c in missing_costs)}")
        return self

    def sleeve_limits(self, sleeve_id: str) -> SleeveLimits:
        return self.sleeves[sleeve_id].limits or self.risk.sleeve_defaults

    def asset_class(self, symbol: str) -> AssetClass | None:
        return self.universe.symbols.get(symbol)

    @property
    def kill_switch_path(self) -> Path:
        return self.root / self.risk.guards.kill_switch_file

    @property
    def journal_path(self) -> Path:
        return self.root / "data" / "journal.db"


def _read_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_settings(root: Path = PROJECT_ROOT) -> Settings:
    config_dir = root / "config"
    sleeves = [SleeveConfig(**s) for s in _read_yaml(config_dir / "sleeves.yaml").get("sleeves", [])]
    ids = [s.id for s in sleeves]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate sleeve ids in sleeves.yaml: {ids}")
    return Settings(
        root=root,
        risk=RiskConfig(**_read_yaml(config_dir / "risk.yaml")),
        sleeves={s.id: s for s in sleeves},
        universe=UniverseConfig(**_read_yaml(config_dir / "universe.yaml")),
        costs={AssetClass(k): CostModel(**v) for k, v in _read_yaml(config_dir / "costs.yaml").items()},
    )


class Secrets(BaseModel):
    alpaca_api_key: str
    alpaca_secret_key: str


def load_secrets(root: Path = PROJECT_ROOT) -> Secrets:
    load_dotenv(root / ".env")
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Copy .env.example to .env and add your Alpaca PAPER keys."
        )
    return Secrets(alpaca_api_key=key, alpaca_secret_key=secret)
