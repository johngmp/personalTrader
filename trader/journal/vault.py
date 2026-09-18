"""Writes the Obsidian vault (vault/) from the journal. Open the folder in Obsidian to browse it.

The journal database stays the source of truth; notes are regenerated from it on every engine run.
Each note has an engine-owned part (YAML frontmatter + the block between the engine markers) and a
free part after `<!-- engine:end -->` that is never overwritten, for the weekly review and your notes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from trader.config import Settings, SleeveConfig
from trader.journal.db import DUST_QTY, Journal

if TYPE_CHECKING:
    from trader.engine.live import Engine

START, END = "<!-- engine:start -->", "<!-- engine:end -->"
DEFAULT_FREE_PART = "\n\n## Review\n\n_Added by the weekly review (Phase 4) or by you. The engine never edits below the line above._\n"


@dataclass
class Trip:
    """One round trip in a sleeve: from flat, through buys and sells, back to flat (or still open)."""

    sleeve_id: str
    symbol: str
    trade_id: str
    entry_ts: str
    entry_reason: str
    initial_stop: float | None
    buy_qty: float = 0.0
    buy_cost: float = 0.0
    sell_qty: float = 0.0
    proceeds: float = 0.0
    fees: float = 0.0
    fees_on_sells: float = 0.0
    haircut: float = 0.0
    qty: float = 0.0
    exit_ts: str | None = None
    exit_reason: str = ""
    fills: list[dict] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    @property
    def entry_price(self) -> float:
        return self.buy_cost / self.buy_qty if self.buy_qty else 0.0

    @property
    def exit_price(self) -> float | None:
        return (self.proceeds + self.fees_on_sells) / self.sell_qty if self.sell_qty else None

    def pnl(self, price: float | None = None) -> float:
        value = self.qty * price if (self.is_open and price) else 0.0
        return self.proceeds + value - self.buy_cost

    def r_multiple(self, price: float | None = None) -> float | None:
        if self.initial_stop is None or self.entry_price <= self.initial_stop:
            return None
        risk = (self.entry_price - self.initial_stop) * self.buy_qty
        return self.pnl(price) / risk if risk > 0 else None


def round_trips(journal: Journal, sleeve_id: str) -> list[Trip]:
    trips: list[Trip] = []
    open_trips: dict[str, Trip] = {}
    for f in journal.fills(sleeve_id):
        symbol = f["symbol"]
        trip = open_trips.get(symbol)
        if f["side"] == "buy":
            if trip is None:
                trip = Trip(sleeve_id=sleeve_id, symbol=symbol, trade_id=f["client_order_id"], entry_ts=f["ts"],
                            entry_reason=f["reason"] or "", initial_stop=f["order_stop"])
                open_trips[symbol] = trip
                trips.append(trip)
            trip.buy_qty += f["qty"]
            trip.buy_cost += f["qty"] * f["price"]
            trip.qty += f["qty"] - f["fee_qty"]
        else:
            if trip is None:
                continue
            trip.sell_qty += f["qty"]
            trip.proceeds += f["qty"] * f["price"] - f["broker_fee_usd"]
            trip.fees_on_sells += f["broker_fee_usd"]
            trip.qty -= f["qty"]
            trip.exit_reason = f["reason"] or ("stop" if f["order_type"] in ("stop", "stop_limit") else "")
        trip.fees += f["broker_fee_usd"]
        trip.haircut += f["cost_haircut_usd"]
        trip.fills.append(dict(f))
        if trip.qty <= DUST_QTY and trip.sell_qty > 0:
            trip.exit_ts = f["ts"]
            del open_trips[symbol]
    return trips


def _write_if_changed(path: Path, generated_head: str) -> None:
    """Replace the engine-owned part, keep everything after the end marker."""
    free = DEFAULT_FREE_PART
    if path.exists():
        existing = path.read_text()
        if END in existing:
            free = existing.split(END, 1)[1]
    content = generated_head.rstrip() + "\n" + END + free
    if not path.exists() or path.read_text() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _money(x: float | None) -> str:
    return "–" if x is None else f"${x:,.2f}"


def _yaml(d: dict) -> str:
    return "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in d.items()) + "\n---\n"


def trip_note_path(vault: Path, trip: Trip) -> Path:
    day = trip.entry_ts[:10]
    return vault / "Trades" / day[:4] / day[5:7] / f"{day}_{trip.sleeve_id}_{trip.symbol.replace('/', '-')}_{trip.trade_id[-6:]}.md"


def write_trip(vault: Path, trip: Trip, sleeve: SleeveConfig | None, price: float | None) -> Path:
    path = trip_note_path(vault, trip)
    pnl = trip.pnl(price)
    r = trip.r_multiple(price)
    front = {
        "id": trip.trade_id, "sleeve": trip.sleeve_id, "symbol": trip.symbol, "venue": "alpaca-paper", "side": "long",
        "status": "open" if trip.is_open else "closed",
        "strategy_params": sleeve.params if sleeve else {},
        "entry_date": trip.entry_ts[:10], "entry_price": round(trip.entry_price, 6),
        "exit_date": trip.exit_ts[:10] if trip.exit_ts else None,
        "exit_price": round(trip.exit_price, 6) if trip.exit_price else None,
        "qty": round(trip.buy_qty, 9), "initial_stop": trip.initial_stop,
        "pnl_usd": round(pnl, 2), "pnl_after_est_costs_usd": round(pnl - trip.haircut, 2),
        "r_multiple": round(r, 2) if r is not None else None,
        "reason_in": trip.entry_reason, "reason_out": trip.exit_reason or None,
        "tags": ["trade", trip.sleeve_id, trip.symbol.replace("/", "-")],
    }
    fills = "\n".join(f"| {f['ts'][:16].replace('T', ' ')} | {f['side']} | {f['qty']:g} | {_money(f['price'])} | "
                      f"{_money(f['broker_fee_usd'])} | {f['reason'] or ''} |" for f in trip.fills)
    status = f"open, marked at {_money(price)}" if trip.is_open else f"closed {trip.exit_ts[:10]}"  # type: ignore[index]
    head = f"""{_yaml(front)}
# {trip.sleeve_id} · {trip.symbol} · {trip.entry_ts[:10]}

{START}
**Status:** {status} · **P&L:** {_money(pnl)} ({_money(pnl - trip.haircut)} after estimated extra costs) · **R:** {f"{r:.2f}" if r is not None else "–"}

- **Why in:** {trip.entry_reason or "–"}
- **Why out:** {trip.exit_reason or "–"}
- **Entry:** {_money(trip.entry_price)} · **initial stop:** {_money(trip.initial_stop)}
- **Sleeve:** [[Sleeves/{trip.sleeve_id}]]

| Time (UTC) | Side | Qty | Price | Alpaca fee | Reason |
|---|---|---|---|---|---|
{fills}
"""
    _write_if_changed(path, head)
    return path


def write_vault(settings: Settings, journal: Journal, engine: Engine) -> None:
    vault = settings.root / "vault"
    prices: dict[str, float] = {}

    def price(symbol: str) -> float | None:
        if symbol not in prices:
            try:
                prices[symbol] = engine.broker.get_quote(symbol, settings.asset_class(symbol)).price  # type: ignore[arg-type]
            except Exception:
                return None
        return prices[symbol]

    sleeve_lines, recent = [], []
    for sleeve in settings.sleeves.values():
        if sleeve.id == "TEST":  # pipeline smoke tests aren't strategy trades
            continue
        trips = round_trips(journal, sleeve.id)
        if not sleeve.enabled and not trips:
            continue
        links = []
        for trip in trips:
            path = write_trip(vault, trip, sleeve, price(trip.symbol) if trip.is_open else None)
            recent.append((trip.entry_ts, trip, path))
            links.append(f"- [[{path.relative_to(vault).with_suffix('').as_posix()}|{trip.entry_ts[:10]} {trip.symbol}]]"
                         f" · {'open' if trip.is_open else _money(trip.pnl())}")
        closed = [t for t in trips if not t.is_open]
        wins = [t for t in closed if t.pnl() > 0]
        rs = [t.r_multiple() for t in closed if t.r_multiple() is not None]
        equity, invested = engine.sleeve_value(sleeve) if sleeve.enabled else (sleeve.capital_usd, 0.0)
        open_rows = "\n".join(
            f"| {t.symbol} | {t.qty:g} | {_money(t.entry_price)} | {_money(price(t.symbol))} | {_money(t.pnl(price(t.symbol)))} |"
            for t in trips if t.is_open) or "| – | | | | |"
        head = f"""{_yaml({"sleeve": sleeve.id, "enabled": sleeve.enabled, "strategy": sleeve.strategy or sleeve.id,
                           "params": sleeve.params, "capital_usd": sleeve.capital_usd, "tags": ["sleeve"]})}
# {sleeve.id} · {sleeve.name}

{START}
**Value:** {_money(equity)} ({equity / sleeve.capital_usd - 1:+.2%}) · **invested:** {_money(invested)} · \
**closed trades:** {len(closed)} · **win rate:** {f"{len(wins) / len(closed):.0%}" if closed else "–"} · \
**average R:** {f"{sum(rs) / len(rs):.2f}" if rs else "–"}

Symbols: {", ".join(sleeve.symbols)} · settings: `{json.dumps(sleeve.params)}`

## Open positions
| Symbol | Qty | Entry | Now | P&L |
|---|---|---|---|---|
{open_rows}

## Trades
{chr(10).join(reversed(links)) or "_none yet_"}
"""
        _write_if_changed(vault / "Sleeves" / f"{sleeve.id}.md", head)
        sleeve_lines.append(f"| [[Sleeves/{sleeve.id}\\|{sleeve.id}]] | {sleeve.name} | {_money(equity)} | "
                            f"{equity / sleeve.capital_usd - 1:+.2%} | {len(closed)} |")

    recent.sort(key=lambda x: x[0], reverse=True)
    home = f"""{_yaml({"tags": ["home"]})}
# Personal Trader

{START}
Paper trading on Alpaca. Last engine run: {journal.kv_get("last_run", "never")} · kill switch: \
{"ON" if engine.kill_switch.active else "off"}

| Sleeve | Strategy | Value | Return | Closed trades |
|---|---|---|---|---|
{chr(10).join(sleeve_lines)}

## Latest trades
{chr(10).join(f"- [[{p.relative_to(vault).with_suffix('').as_posix()}|{t.entry_ts[:10]} {t.sleeve_id} {t.symbol}]]" for _, t, p in recent[:15]) or "_none yet_"}

See also: [[Learnings/learnings]] · [[Changelog]]
"""
    _write_if_changed(vault / "Home.md", home)

    learnings = vault / "Learnings" / "learnings.md"
    if not learnings.exists():
        learnings.parent.mkdir(parents=True, exist_ok=True)
        learnings.write_text(LEARNINGS_TEMPLATE)
    changelog = vault / "Changelog.md"
    if not changelog.exists():
        changelog.write_text(CHANGELOG_TEMPLATE)


LEARNINGS_TEMPLATE = """# Learnings

Curated by the weekly review (Phase 4). Every entry needs linked trades and a sample size; see PLAN.md §6b.
Statuses: hypothesis · testing · adopted · rejected · expired. Keep this file under ~150 lines.

_No learnings yet: too few trades._
"""

CHANGELOG_TEMPLATE = """# Changelog

Every strategy or setting change, and who approved it.

- 2026-09-18 · **B0** buy-and-hold benchmark (SPY, QQQ, BTC/USD) enabled for paper trading · approved by you
- 2026-09-18 · **S1** trend following v1 enabled for paper trading with `{"fast": 20, "slow": 200, "atr_mult": 2.5}` (chosen by Gate 1) · approved by you
"""
