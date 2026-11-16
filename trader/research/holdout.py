"""A slice of history that research cannot reach, and a receipt for the one time it is read.

The problem this solves is visible in this project's own record. `research_log.jsonl` shows the same
out-of-sample window read nineteen times, six of them for S4 alone, and every Gate 1 report prints the
out-of-sample Sharpe of all eighteen grid cells -- the full surface, in front of the researcher, before
the next decision. PLAN.md line 418 records the consequence in plain words: "that was measured by
peeking at out-of-sample and cannot be acted on." A window read like that is not a test any more. It is
part of the training set, arrived at slowly.

So the most recent years are fenced off in the DATA rather than by a rule asking people not to look.
`research_bars` is the only door research price history comes through, and it truncates. That is the
difference the 2026 papers draw between a structural control and a procedural one: a skill file saying
"never tune on out-of-sample results" is worth having and is not worth relying on.

Opening the holdout costs a receipt, written and flushed to disk BEFORE any data is returned, so a
crash, a stray exception or a power cut still spends it. That is deliberate and is the whole mechanism:
if a failed read were free, the holdout could be read until it gave a good answer.

**An honest limit, recorded on every receipt rather than buried here.** Sealing the window today does
not unsee it. S1, S4, S8 and S9 have all been evaluated repeatedly against data from this period, so
their first holdout read is weak evidence and says so. The seal is only clean for strategies that
arrive after it. Treating a fresh constant as though it made a fresh slice would be the exact
self-deception this module exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from trader.config import PROJECT_ROOT, Settings

# Pinned in code, not in config. A seal date that can be set from a YAML file is a tunable, and the one
# thing this must not be is adjustable by whoever is disappointed by the result. Moving it is a code
# change, a test change and a line in git history -- which is the point.
#
# 2022-01-01 leaves 27 years of research history for SPY (a median of 4,892 bars across S8's forty
# markets, 631 for the thinnest) and a five-year holdout spanning the 2022 rate shock, the 2023-24
# melt-up and 2025-26. Varied enough not to be one regime, recent enough to be the part that matters.
HOLDOUT_START = pd.Timestamp("2022-01-01")

RECEIPTS_DIR = PROJECT_ROOT / "reports" / "holdout_receipts"
CONFIRMATION = "open holdout for {strategy}"


class HoldoutRefused(RuntimeError):
    """A precondition failed. Nothing was read and no receipt was spent."""


class HoldoutAlreadyOpened(RuntimeError):
    """This exact candidate has had its one read. Changing a parameter earns a new receipt; wishing
    does not."""


def research_bars(symbols: list[str], *, refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Every bar strictly before the seal. The only price history research can obtain."""
    from trader.data.history import load_many

    return {s: df.loc[df.index < HOLDOUT_START] for s, df in load_many(symbols, refresh=refresh).items()}


def reaches_past_the_seal(bars: dict[str, pd.DataFrame]) -> list[str]:
    """Symbols whose data runs into the holdout. Used by `evaluate` to refuse a hand-built dict."""
    return sorted(s for s, df in bars.items() if len(df.index) and df.index.max() >= HOLDOUT_START)


def candidate_id(strategy: str, params: dict, symbols: list[str]) -> str:
    from trader.research.ledger import _canonical, _sha

    return _sha(_canonical({"strategy": strategy, "params": params, "symbols": sorted(symbols)}))[:12]


@dataclass
class Receipt:
    strategy: str
    candidate: str
    params: dict
    symbols: list[str]
    opened_at: str
    opened_by: str                   # the exact phrase the human typed
    gate1_ts: str
    gate1_oos_sharpe: float
    ledger_head: str
    pre_existing_exposure: bool
    used_at: str | None = None
    result: dict | None = None

    @property
    def path(self) -> Path:
        return RECEIPTS_DIR / f"{self.strategy}-{self.candidate}.json"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(asdict(self), f, indent=2)
            f.flush()
            import os
            os.fsync(f.fileno())
        tmp.replace(self.path)


def read_receipt(strategy: str, candidate: str) -> Receipt | None:
    path = RECEIPTS_DIR / f"{strategy}-{candidate}.json"
    return Receipt(**json.loads(path.read_text())) if path.exists() else None


def _has_pre_existing_exposure(strategy: str) -> bool:
    """Has this strategy already been measured against data from inside the holdout?

    True for everything that ran before the seal existed, which is every sleeve trading today. The flag
    is not a technicality: it is the difference between a clean test and a formality.
    """
    from trader.research.gate2 import RESEARCH_LOG

    if not RESEARCH_LOG.exists():
        return False
    for line in RESEARCH_LOG.read_text().splitlines():
        if line.strip() and json.loads(line).get("strategy") == strategy:
            return True
    return False


def request_receipt(strategy: str, settings: Settings, *, confirmation: str,
                    sleeve_id: str | None = None) -> Receipt:
    """Every precondition, then write and flush the receipt. Raises rather than returning a reason."""
    from trader.research.gate2 import latest_gate1
    from trader.research.ledger import last_hash, verify_chain

    expected = CONFIRMATION.format(strategy=strategy)
    if confirmation != expected:
        # A phrase naming the strategy, not a --yes flag. A flag is something a script emits as readily
        # as a person; typing this sentence is a deliberate act by someone who knows what it spends.
        raise HoldoutRefused(f'say exactly: --confirm "{expected}"')

    ok, why = verify_chain()
    if not ok:
        raise HoldoutRefused(f"the trial ledger has been altered ({why}); fix that first")

    sleeve = settings.sleeves.get(sleeve_id or strategy)
    if sleeve is None:
        raise HoldoutRefused(f"no sleeve found for {sleeve_id or strategy}")

    g1 = latest_gate1(strategy, list(sleeve.symbols))
    if g1 is None:
        raise HoldoutRefused(f"{strategy} has no passing Gate 1; the holdout is not for fishing")
    if dict(sleeve.params or {}) != dict(g1.chosen_params):
        raise HoldoutRefused(
            f"{sleeve.id} runs {dict(sleeve.params or {})} but Gate 1 chose {dict(g1.chosen_params)}. "
            f"Spending the holdout on settings you are not running tests nothing you can act on.")

    candidate = candidate_id(strategy, dict(sleeve.params or {}), list(sleeve.symbols))
    existing = read_receipt(strategy, candidate)
    if existing is not None:
        raise HoldoutAlreadyOpened(
            f"{strategy} opened its holdout for these settings on {existing.opened_at[:10]}"
            + (f" and scored {existing.result.get('sharpe'):+.2f}" if existing.result else
               " and the read did not complete -- which still spends it"))

    receipt = Receipt(
        strategy=strategy, candidate=candidate, params=dict(sleeve.params or {}),
        symbols=sorted(sleeve.symbols), opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        opened_by=confirmation, gate1_ts=g1.ts, gate1_oos_sharpe=g1.oos_sharpe,
        ledger_head=last_hash(), pre_existing_exposure=_has_pre_existing_exposure(strategy))
    receipt.save()
    return receipt


def holdout_bars(symbols: list[str], receipt: Receipt, *,
                 refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Bars from the seal onward. SPENDS the receipt before returning anything.

    The ordering is the mechanism, not an implementation detail. Stamping `used_at` after the evaluation
    would make a crashed or disappointing read free, and a holdout that can be re-read until it gives a
    good answer is not a holdout.
    """
    from trader.data.history import load_many

    if receipt.used_at is not None:
        raise HoldoutAlreadyOpened(f"this receipt was spent at {receipt.used_at}")
    receipt.used_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    receipt.save()
    return {s: df.loc[df.index >= HOLDOUT_START] for s, df in load_many(symbols, refresh=refresh).items()}
