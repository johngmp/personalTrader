"""cTrader Open API adapter for forex. Demo (paper) accounts only.

Fusion Markets, like most cTrader brokers, offers no REST API of its own: cTrader is the platform
(built by Spotware) and the Open API is its official door for programs. This module holds the parts
that need no credentials — the demo-only guard, symbol mapping, volume and price conversion — so they
can be tested before a connection exists. The networking is added once Spotware approves the app.

Two properties do the safety work:

- **Demo and live are separate servers.** `demo.ctraderapi.com` and `live.ctraderapi.com` are fully
  isolated; accounts on one are invisible on the other. We only ever dial the demo host, so a live
  account cannot be reached even if it shares the same cTrader ID.
- **Volume is counted in hundredths of a unit** ("cents"), not in units and not in lots. 1,000 units of
  EUR is volume 100000. Getting this wrong by 100x would size every trade 100x too large or too small,
  so every conversion goes through `to_volume_cents` and is rounded DOWN to the broker's step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.brokers.base import LiveTradingRefused
from trader.brokers.oanda import fx_clock  # forex hours are the same wherever you trade them

__all__ = ["CTraderError", "SymbolSpec", "DEMO_HOST", "LIVE_HOST", "PORT", "VOLUME_SCALE",
           "check_demo_only", "to_ctrader_name", "from_ctrader_name", "build_symbol_map",
           "to_volume_cents", "from_volume_cents", "round_price", "round_stop", "fx_clock"]

DEMO_HOST = "demo.ctraderapi.com"
LIVE_HOST = "live.ctraderapi.com"
PORT = 5035
VOLUME_SCALE = 100  # cTrader volumes are in hundredths of a unit of the base currency


class CTraderError(RuntimeError):
    pass


def check_demo_only(host: str) -> None:
    """Refuse any host but the demo endpoint. This is the paper-only guard, and it is not overridable."""
    if host.strip().lower() != DEMO_HOST:
        raise LiveTradingRefused(
            f"cTrader host is {host!r}, not {DEMO_HOST!r}. This system only trades on cTrader demo accounts."
        )


@dataclass(frozen=True)
class SymbolSpec:
    """What the broker says about one tradeable symbol. The *_cents fields are cTrader's own units."""

    symbol_id: int
    name: str                # cTrader's name for it, e.g. "EURUSD"
    digits: int              # decimal places in a price
    lot_size_cents: int      # one standard lot, in cents (EUR/USD: 10_000_000 = 100,000 units)
    min_volume_cents: int
    max_volume_cents: int
    step_volume_cents: int

    def __post_init__(self) -> None:
        if self.step_volume_cents <= 0:
            raise CTraderError(f"{self.name}: step volume must be positive, got {self.step_volume_cents}")
        if self.min_volume_cents > self.max_volume_cents:
            raise CTraderError(f"{self.name}: min volume {self.min_volume_cents} exceeds max {self.max_volume_cents}")

    @property
    def min_units(self) -> float:
        return from_volume_cents(self.min_volume_cents)

    @property
    def step_units(self) -> float:
        return from_volume_cents(self.step_volume_cents)


# --- symbol names ---------------------------------------------------------------------------------
# We say "EUR/USD"; cTrader says "EURUSD". Brokers also add suffixes to distinguish account types
# (Fusion uses e.g. "EURUSD.r" on some feeds), so matching ignores anything after the base name.

def to_ctrader_name(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "").upper()


def from_ctrader_name(name: str, ours: list[str]) -> str | None:
    """Map a broker symbol name back to one of our symbols, or None if we don't trade it."""
    wanted = to_ctrader_name(name).split(".")[0]
    for symbol in ours:
        if to_ctrader_name(symbol) == wanted:
            return symbol
    return None


def build_symbol_map(specs: list[SymbolSpec], ours: list[str]) -> dict[str, SymbolSpec]:
    """Index the broker's symbol list by OUR symbol names, ignoring everything we don't trade.

    Raises if one of our symbols matches more than one broker symbol: silently picking the wrong feed
    (say a CFD variant with different sizing) would mis-size every trade in it.
    """
    out: dict[str, SymbolSpec] = {}
    for spec in specs:
        ours_name = from_ctrader_name(spec.name, ours)
        if ours_name is None:
            continue
        if ours_name in out:
            raise CTraderError(
                f"{ours_name} matches two cTrader symbols ({out[ours_name].name}, {spec.name}); "
                "name the exact one in config so sizing is unambiguous"
            )
        out[ours_name] = spec
    return out


# --- volume ---------------------------------------------------------------------------------------

def to_volume_cents(units: float, spec: SymbolSpec) -> int:
    """Units of the base currency -> cTrader's integer volume, snapped DOWN to the broker's step.

    Rounding down matters: the quantity handed in was sized by the risk gate to risk at most 1% of the
    account, so trading *more* than it would breach that limit. Trading slightly less never does.
    """
    if units <= 0:
        raise CTraderError(f"{spec.name}: order units must be positive, got {units}")
    steps = math.floor(round(units * VOLUME_SCALE, 6) / spec.step_volume_cents)
    cents = steps * spec.step_volume_cents
    if cents < spec.min_volume_cents:
        raise CTraderError(
            f"{spec.name}: {units} units rounds to {cents} cents, below the broker minimum of "
            f"{spec.min_volume_cents} ({spec.min_units} units)"
        )
    return min(cents, spec.max_volume_cents)


def from_volume_cents(cents: float) -> float:
    """cTrader's integer volume -> units of the base currency."""
    return cents / VOLUME_SCALE


# --- prices ---------------------------------------------------------------------------------------

def round_price(price: float, spec: SymbolSpec, mode: str = "nearest") -> float:
    """Round to the symbol's price precision. `mode` is "nearest", "up" or "down"."""
    if price <= 0:
        raise CTraderError(f"{spec.name}: price must be positive, got {price}")
    scale = 10.0**spec.digits
    scaled = round(price * scale, 6)  # kill float dust before the directional rounding
    if mode == "up":
        return math.ceil(scaled) / scale
    if mode == "down":
        return math.floor(scaled) / scale
    if mode == "nearest":
        return round(scaled) / scale
    raise CTraderError(f"unknown rounding mode {mode!r}")


def round_stop(price: float, spec: SymbolSpec) -> float:
    """Round a protective sell stop UP, i.e. towards the market.

    The stop defines the worst case the risk gate budgeted for. Rounding it down would widen that loss
    past the budget; rounding up keeps it inside, at the cost of a marginally tighter exit.
    """
    return round_price(price, spec, mode="up")
