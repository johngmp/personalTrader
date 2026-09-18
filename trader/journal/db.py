"""SQLite journal: the source of truth for every order, risk decision, fill and equity snapshot."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from trader.config import CostModel
from trader.journal.cost_haircut import cost_haircut_usd
from trader.models import BrokerOrder, Fill, OrderRequest, OrderStatus, RiskDecision, Side
from trader.risk.gate import RecentOrder

# Daily limits reset on the US trading day, matching Alpaca's "last_equity" (previous close).
TRADING_DAY_TZ = ZoneInfo("America/New_York")

# Brokers round quantities (Alpaca crypto: 9 decimals), so a sleeve can be left with a tiny remainder
# the broker no longer holds. Anything smaller than this counts as flat.
DUST_QTY = 1e-8

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    sleeve_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    asset_class       TEXT,
    side              TEXT NOT NULL,
    qty               REAL,
    notional          REAL,
    order_type        TEXT NOT NULL,
    limit_price       REAL,
    stop_price        REAL,
    reason            TEXT,
    is_test           INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    broker            TEXT,
    broker_order_id   TEXT,
    broker_status     TEXT,
    filled_qty        REAL NOT NULL DEFAULT 0,
    filled_avg_price  REAL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_sleeve ON orders(sleeve_id, symbol);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    client_order_id   TEXT NOT NULL REFERENCES orders(client_order_id),
    approved          INTEGER NOT NULL,
    summary           TEXT NOT NULL,
    checks_json       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    client_order_id   TEXT NOT NULL REFERENCES orders(client_order_id),
    sleeve_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               REAL NOT NULL,
    price             REAL NOT NULL,
    cost_haircut_usd  REAL NOT NULL,
    fee_qty           REAL NOT NULL DEFAULT 0,
    broker_fee_usd    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_sleeve ON fills(sleeve_id, symbol);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    broker            TEXT NOT NULL,
    equity            REAL NOT NULL,
    cash              REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions_state (      -- strategy state for each open sleeve position
    sleeve_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    entry_price       REAL NOT NULL,
    entry_date        TEXT NOT NULL,
    stop              REAL,
    initial_stop      REAL,
    high_since_entry  REAL NOT NULL,
    bars_held         INTEGER NOT NULL DEFAULT 0,
    stop_order_id     TEXT,                        -- client_order_id of the resting broker stop order
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, symbol)
);

CREATE TABLE IF NOT EXISTS decisions (             -- last completed daily bar each sleeve acted on
    sleeve_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    bar_date          TEXT NOT NULL,
    decided_at        TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, symbol)
);

CREATE TABLE IF NOT EXISTS sleeve_equity (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    sleeve_id         TEXT NOT NULL,
    equity            REAL NOT NULL,              -- capital + trading P&L at market prices
    adjusted_equity   REAL NOT NULL,              -- equity minus the estimated cost haircut
    invested          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sleeve_equity ON sleeve_equity(sleeve_id, ts);

CREATE TABLE IF NOT EXISTS kv (
    key               TEXT PRIMARY KEY,
    value             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    level             TEXT NOT NULL,
    message           TEXT NOT NULL
);
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def trading_day_start(now: datetime) -> datetime:
    local = now.astimezone(TRADING_DAY_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


class Journal:
    def __init__(self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # --- writes -----------------------------------------------------------------------------

    def record_order(self, order: OrderRequest, asset_class: str | None) -> None:
        now = self._now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO orders (client_order_id, created_at, sleeve_id, symbol, asset_class, side, qty,
                   notional, order_type, limit_price, stop_price, reason, is_test, status, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order.client_order_id, now, order.sleeve_id, order.symbol, asset_class, order.side.value,
                 order.qty, order.notional, order.order_type.value, order.limit_price, order.stop_price,
                 order.reason, int(order.is_test), OrderStatus.PROPOSED.value, now),
            )

    def record_decision(self, client_order_id: str, decision: RiskDecision) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO risk_decisions (ts, client_order_id, approved, summary, checks_json) VALUES (?,?,?,?,?)",
                (self._now(), client_order_id, int(decision.approved), decision.summary(),
                 json.dumps([c.model_dump() for c in decision.checks])),
            )
            if not decision.approved:
                self._set_status(client_order_id, OrderStatus.REJECTED)

    def record_broker_order(self, broker: str, bo: BrokerOrder) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE orders SET status=?, broker=?, broker_order_id=?, broker_status=?, updated_at=?
                   WHERE client_order_id=?""",
                (bo.status.value, broker, bo.broker_order_id, bo.raw_status, self._now(), bo.client_order_id),
            )

    def record_broker_error(self, client_order_id: str, error: str) -> None:
        with self.conn:
            self._set_status(client_order_id, OrderStatus.REJECTED, broker_status=f"error: {error}"[:500])

    def record_fill_progress(self, bo: BrokerOrder, sleeve_id: str, cost_model: CostModel) -> Fill | None:
        """Record any newly filled quantity since the last update. Returns the new fill, if any."""
        row = self.conn.execute(
            "SELECT filled_qty FROM orders WHERE client_order_id=?", (bo.client_order_id,)
        ).fetchone()
        already = row["filled_qty"] if row else 0.0
        new_qty = bo.filled_qty - already
        fill = None
        with self.conn:
            if new_qty > 1e-12 and bo.filled_avg_price:
                # Price of the new slice, derived from the change in the running average.
                total_cost = bo.filled_qty * bo.filled_avg_price
                prev_cost = already * (self._avg_price(bo.client_order_id) or bo.filled_avg_price)
                slice_price = (total_cost - prev_cost) / new_qty
                ts = self.clock()
                notional = new_qty * slice_price
                fee_rate = cost_model.broker_fee_bps / 10_000
                # The broker takes its fee from what you receive: coins on a buy, dollars on a sell.
                fee_qty = new_qty * fee_rate if bo.side is Side.BUY else 0.0
                fill = Fill(client_order_id=bo.client_order_id, sleeve_id=sleeve_id, symbol=bo.symbol, side=bo.side,
                            qty=new_qty, price=slice_price, fee_qty=fee_qty, broker_fee_usd=notional * fee_rate,
                            cost_haircut_usd=cost_haircut_usd(notional, cost_model), ts=ts)
                self.conn.execute(
                    """INSERT INTO fills (ts, client_order_id, sleeve_id, symbol, side, qty, price, cost_haircut_usd,
                       fee_qty, broker_fee_usd) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (_iso(ts), fill.client_order_id, sleeve_id, fill.symbol, fill.side.value, fill.qty, fill.price,
                     fill.cost_haircut_usd, fill.fee_qty, fill.broker_fee_usd),
                )
            self.conn.execute(
                """UPDATE orders SET status=?, broker_status=?, filled_qty=?, filled_avg_price=?, updated_at=?
                   WHERE client_order_id=?""",
                (bo.status.value, bo.raw_status, bo.filled_qty, bo.filled_avg_price, self._now(), bo.client_order_id),
            )
        return fill

    def snapshot_equity(self, broker: str, equity: float, cash: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO equity_snapshots (ts, broker, equity, cash) VALUES (?,?,?,?)",
                (self._now(), broker, equity, cash),
            )

    def log(self, message: str, level: str = "info") -> None:
        with self.conn:
            self.conn.execute("INSERT INTO events (ts, level, message) VALUES (?,?,?)", (self._now(), level, message))

    # --- reads ------------------------------------------------------------------------------

    def sleeve_holdings(self, sleeve_id: str) -> dict[str, float]:
        rows = self.conn.execute(
            """SELECT symbol, SUM(CASE WHEN side='buy' THEN qty - fee_qty ELSE -qty END) AS qty
               FROM fills WHERE sleeve_id=? GROUP BY symbol""",
            (sleeve_id,),
        ).fetchall()
        return {r["symbol"]: r["qty"] for r in rows if abs(r["qty"]) > DUST_QTY}

    def all_holdings(self) -> dict[tuple[str, str], float]:
        rows = self.conn.execute(
            """SELECT sleeve_id, symbol, SUM(CASE WHEN side='buy' THEN qty - fee_qty ELSE -qty END) AS qty
               FROM fills GROUP BY sleeve_id, symbol"""
        ).fetchall()
        return {(r["sleeve_id"], r["symbol"]): r["qty"] for r in rows if abs(r["qty"]) > DUST_QTY}

    def peak_equity(self, broker: str) -> float:
        row = self.conn.execute("SELECT MAX(equity) AS peak FROM equity_snapshots WHERE broker=?", (broker,)).fetchone()
        return row["peak"] or 0.0

    def orders_submitted_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE created_at >= ? AND broker_order_id IS NOT NULL",
            (_iso(since),),
        ).fetchone()
        return row["n"]

    def recent_orders(self, within: timedelta, now: datetime) -> list[RecentOrder]:
        """Recent trade orders, for the duplicate check. Resting protective stops aren't trades."""
        rows = self.conn.execute(
            """SELECT sleeve_id, symbol, side, created_at FROM orders
               WHERE created_at >= ? AND status NOT IN (?, ?) AND order_type IN ('market', 'limit')""",
            (_iso(now - within), OrderStatus.REJECTED.value, OrderStatus.PROPOSED.value),
        ).fetchall()
        return [RecentOrder(r["sleeve_id"], r["symbol"], Side(r["side"]), datetime.fromisoformat(r["created_at"]))
                for r in rows]

    def open_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN (?, ?) AND broker_order_id IS NOT NULL",
            (OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value),
        ).fetchall()

    def order(self, client_order_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()

    def last_decision(self, client_order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM risk_decisions WHERE client_order_id=? ORDER BY id DESC LIMIT 1", (client_order_id,)
        ).fetchone()

    def recent_order_rows(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    # --- live strategy state ------------------------------------------------------------------

    def position_states(self, sleeve_id: str | None = None) -> list[sqlite3.Row]:
        if sleeve_id is None:
            return self.conn.execute("SELECT * FROM positions_state ORDER BY sleeve_id, symbol").fetchall()
        return self.conn.execute("SELECT * FROM positions_state WHERE sleeve_id=?", (sleeve_id,)).fetchall()

    def position_state(self, sleeve_id: str, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM positions_state WHERE sleeve_id=? AND symbol=?",
                                 (sleeve_id, symbol)).fetchone()

    def upsert_position_state(self, sleeve_id: str, symbol: str, **fields) -> None:
        existing = self.position_state(sleeve_id, symbol)
        with self.conn:
            if existing is None:
                cols = {"sleeve_id": sleeve_id, "symbol": symbol, "updated_at": self._now(), **fields}
                self.conn.execute(f"INSERT INTO positions_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                  tuple(cols.values()))
            elif fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.conn.execute(f"UPDATE positions_state SET {sets}, updated_at=? WHERE sleeve_id=? AND symbol=?",
                                  (*fields.values(), self._now(), sleeve_id, symbol))

    def delete_position_state(self, sleeve_id: str, symbol: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM positions_state WHERE sleeve_id=? AND symbol=?", (sleeve_id, symbol))

    def last_decided_bar(self, sleeve_id: str, symbol: str) -> str | None:
        row = self.conn.execute("SELECT bar_date FROM decisions WHERE sleeve_id=? AND symbol=?",
                                (sleeve_id, symbol)).fetchone()
        return row["bar_date"] if row else None

    def set_decided_bar(self, sleeve_id: str, symbol: str, bar_date: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO decisions (sleeve_id, symbol, bar_date, decided_at) VALUES (?,?,?,?) "
                "ON CONFLICT(sleeve_id, symbol) DO UPDATE SET bar_date=excluded.bar_date, decided_at=excluded.decided_at",
                (sleeve_id, symbol, bar_date, self._now()))

    def sleeve_cash_flow(self, sleeve_id: str) -> float:
        """Money in minus money out from trading: sells (after the broker's USD fee) minus buys."""
        row = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN side='sell' THEN qty*price - broker_fee_usd ELSE -qty*price END), 0) AS cf
               FROM fills WHERE sleeve_id=?""", (sleeve_id,)).fetchone()
        return row["cf"]

    def sleeve_haircut(self, sleeve_id: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost_haircut_usd), 0) AS h FROM fills WHERE sleeve_id=?",
                                (sleeve_id,)).fetchone()
        return row["h"]

    def record_sleeve_equity(self, sleeve_id: str, equity: float, adjusted: float, invested: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sleeve_equity (ts, sleeve_id, equity, adjusted_equity, invested) VALUES (?,?,?,?,?)",
                (self._now(), sleeve_id, equity, adjusted, invested))

    def sleeve_equity_history(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sleeve_equity ORDER BY ts").fetchall()

    def fills(self, sleeve_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT f.*, o.stop_price AS order_stop, o.order_type, o.reason FROM fills f " \
            "JOIN orders o ON o.client_order_id = f.client_order_id"
        if sleeve_id is None:
            return self.conn.execute(q + " ORDER BY f.ts, f.id").fetchall()
        return self.conn.execute(q + " WHERE f.sleeve_id=? ORDER BY f.ts, f.id", (sleeve_id,)).fetchall()

    def mark_order(self, client_order_id: str, status: OrderStatus, broker_status: str) -> None:
        with self.conn:
            self._set_status(client_order_id, status, broker_status=broker_status)

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- internals ----------------------------------------------------------------------------

    def _now(self) -> str:
        return _iso(self.clock())

    def _migrate(self) -> None:
        """Add columns introduced after a journal file was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(fills)")}
        with self.conn:
            for name in ("fee_qty", "broker_fee_usd"):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE fills ADD COLUMN {name} REAL NOT NULL DEFAULT 0")

    def _avg_price(self, client_order_id: str) -> float | None:
        row = self.conn.execute(
            "SELECT filled_avg_price FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        return row["filled_avg_price"] if row else None

    def _set_status(self, client_order_id: str, status: OrderStatus, broker_status: str | None = None) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, broker_status=COALESCE(?, broker_status), updated_at=? WHERE client_order_id=?",
            (status.value, broker_status, self._now(), client_order_id),
        )
