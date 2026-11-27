"""Loads and validates the YAML files in config/ and secrets from .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Forex prices come from a free delayed feed (Yahoo), not a broker, so 300s is the wrong yardstick:
    # gold routinely arrives 10-15 minutes old. The check exists to catch a DEAD feed, and for a daily
    # strategy holding for weeks a price this old changes position size by a rounding error. Anything
    # older than this while the FX market is open does mean something is broken.
    stale_forex_seconds: int = 3600
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
    # shadow / shadow_fx = live prices, no orders sent anywhere (shadow_fx prices FX from Yahoo, no broker)
    broker: Literal["alpaca", "oanda", "ctrader", "liquid", "shadow", "shadow_fx"] = "alpaca"
    capital_usd: float
    symbols: list[str]
    benchmark: str | None = None
    limits: SleeveLimits | None = None  # per-sleeve override of sleeve_defaults
    strategy: str | None = None         # strategy code the live engine runs (defaults to the sleeve id)
    params: dict[str, float | int | str] = Field(default_factory=dict)  # settings chosen by Gate 1
    # AI involvement (Phase 4): "analyst-team" = rule entries need an AI approve/veto verdict first;
    # "investor-panel" = target weights come from the weekly AI investor panel.
    ai: Literal["analyst-team", "investor-panel"] | None = None
    # Wind the sleeve down: it stops entering and sells what it holds through the normal exit path, so
    # stops are cancelled and every close is journalled like any other. Set this rather than flipping
    # `enabled` straight to false -- a disabled sleeve is simply not managed, so its positions would sit
    # at the broker with stops that never trail and nothing watching them. Disable it once it is flat.
    retiring: bool = False


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
    # Cost of HOLDING, charged per day on the position's value, not per trade. Forex pays this as swap
    # (overnight financing) every night. For a strategy that holds for weeks it dwarfs the trading cost,
    # so leaving it at zero flatters any slow strategy. Positive = it costs you to hold.
    carry_bps_per_day: float = 0.0

    @property
    def total_bps(self) -> float:
        """Estimated costs added on top of paper fills (broker-charged fees are already in the fill)."""
        return self.half_spread_bps + self.slippage_bps + self.fee_bps


class IntradayConfig(BaseModel):
    """The session-reversion watcher. It sends signals and never places an order.

    Deliberately not a sleeve. A sleeve holds capital, sends orders through the risk gate and decides
    once per daily bar; this holds nothing, sends nothing, and decides inside the day. Forcing it into
    the sleeve machinery would hide that difference rather than show it.
    """

    enabled: bool = False
    # Every pair runs the SAME parameters, chosen once on EUR/USD's first 60% of history. That is the
    # whole basis on which a second pair is allowed: it is not re-tuned, so adding one cannot be a way
    # of fitting the grid again. A pair earns its place by surviving those settings unchanged.
    symbols: list[str] = ["EUR/USD"]
    drop_atr: float = 0.5
    stop_atr: float = 0.3
    target_frac: float = 1.0
    atr_days: int = 20
    open_utc: str = "07:00"
    close_utc: str = "16:00"
    # How late after the open a plan may still be built. The levels are measured FROM the session open,
    # so a plan built at 11:20 from the 11:20 rate is a different trade from the one that was tested.
    # Miss the window -- a restart, an outage, a laptop asleep -- and the day is skipped instead.
    plan_within_minutes: int = 30
    capital_usd: float = 10_000.0
    risk_pct: float = 1.0
    # The broker's smallest tradable volume, in lots. On an account too small for risk-based sizing to
    # clear it, THIS sets the position size and the risk per trade -- so a wrong value here is wrong on
    # every single trade, in the direction of risking more than intended.
    min_lot: float = 0.01
    # The London session (07:00-16:00 UTC) lands at 18:00-03:00 Sydney local, so a record-only alert --
    # one telling you what the broker's own SL/TP already did, not one asking you to do something -- can
    # just as easily land at 2am as at 7pm. Queuing those overnight and delivering one batched message
    # the next time anything runs after the window ends means a quiet night does not mean a buzzing
    # phone. Half past eleven rather than eleven on the dot: the close is 03:00 Sydney and most
    # record-only exits cluster either side of midnight, so starting a little into the evening still
    # catches all of those without swallowing the last hour of a normal evening session. Plan and buy
    # alerts -- the ones that need you to actually do something -- are never delayed by this, whatever
    # these are set to.
    quiet_hours_start: str = "23:30"
    quiet_hours_end: str = "07:00"

    @field_validator("symbols")
    @classmethod
    def _only_pairs_whose_pips_this_code_understands(cls, symbols: list[str]) -> list[str]:
        """Refuse a pair quoted to 3 decimals, because every pip figure would be 100x too large.

        `PIP` in intraday_watch is the constant 1e-4, which is a pip on a 5-decimal major and is NOT one
        on a JPY pair, where a pip is 0.01. Adding USD/JPY to this list produced a plan reading "typical
        day 6000 pips", a "1800 pips" stop and a 3000-pip target -- prices and position size correct,
        every pip count wrong by a factor of a hundred, and the MIN_ATR floor that exists to reject bad
        data rendered meaningless because 0.001 is nothing next to a real JPY range of 0.6.

        A wrong number on a message you act on by hand is worse than an unsupported pair, so this fails
        at startup instead. Supporting one means making pip size a property of the pair rather than a
        module constant, and that is a prerequisite for ever testing USD/JPY, not an afterthought.
        """
        bad = [s for s in symbols if s.split("/")[-1] == "JPY" or s.startswith("XAU")]
        if bad:
            raise ValueError(
                f"intraday.symbols cannot include {bad}: a pip on these is not 1e-4, so every pip "
                f"figure in the messages would be wrong by 100x. Make pip size per-pair first.")
        return symbols


class Settings(BaseModel):
    """Everything loaded from config/, cross-checked for consistency."""

    root: Path
    risk: RiskConfig
    sleeves: dict[str, SleeveConfig]
    universe: UniverseConfig
    costs: dict[AssetClass, CostModel]
    intraday: IntradayConfig = IntradayConfig()

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


def _read_yaml(path: Path, missing_ok: bool = False) -> dict:
    """Load a config file. `missing_ok` is for files that have usable defaults and are off by default.

    Deliberately not the default: a missing risk.yaml quietly falling back to built-in limits is exactly
    the kind of silence this project should not have.
    """
    if missing_ok and not path.exists():
        return {}
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
        intraday=IntradayConfig(**_read_yaml(config_dir / "intraday.yaml", missing_ok=True)),
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
