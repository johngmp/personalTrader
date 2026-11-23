"""AI signals: the ONLY way AI output can influence trading.

AI skills (run as Claude scheduled tasks) write a JSON file and call `trader signal submit FILE`.
It is validated strictly here and stored in the journal. The engine then *reads* accepted signals;
every order it derives still goes through the risk gate. AI never talks to a broker.

Two kinds:
- entry_verdict  (sleeve with ai: analyst-team): approve/veto the rule strategy's entry candidates
- target_weights (sleeve with ai: investor-panel): weekly target portfolio weights
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trader.config import PROJECT_ROOT, Settings
from trader.journal.db import Journal

SCHEMA_VERSION = 1
LEARNINGS_PATH = PROJECT_ROOT / "vault" / "Learnings" / "learnings.md"
WEIGHTS_MAX_AGE = timedelta(days=8)

SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_signals (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    received_at        TEXT NOT NULL,
    sleeve_id          TEXT NOT NULL,
    kind               TEXT NOT NULL,
    bar_date           TEXT NOT NULL,
    learnings_version  TEXT,
    payload            TEXT NOT NULL,
    status             TEXT NOT NULL,     -- accepted | rejected | applied
    reason             TEXT
);
CREATE TABLE IF NOT EXISTS ai_requests (           -- entry candidates waiting for an AI verdict
    sleeve_id     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    bar_date      TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    context       TEXT NOT NULL,                   -- JSON: price, stop, reason, technicals
    status        TEXT NOT NULL,                   -- pending | answered | expired
    PRIMARY KEY (sleeve_id, symbol, bar_date)
);
"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Verdict(_Strict):
    symbol: str
    verdict: Literal["approve", "veto"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=20, max_length=4000)
    news_ids: list[str] = Field(default_factory=list)


class WeightItem(_Strict):
    symbol: str
    weight: float = Field(ge=0, le=1)
    score: float = Field(ge=-1, le=1)
    reasoning: str = Field(min_length=10, max_length=4000)


class Signal(_Strict):
    schema_version: int
    id: str = Field(min_length=6, max_length=80)
    created_at: datetime
    sleeve: str
    kind: Literal["entry_verdict", "target_weights"]
    bar_date: str                      # the finished daily bar the analysis is about (YYYY-MM-DD)
    learnings_version: str             # from `trader context`, so reviews can tell which learnings were used
    summary: str = Field(default="", max_length=4000)
    verdicts: list[Verdict] = Field(default_factory=list)
    weights: list[WeightItem] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at needs a timezone, e.g. 2026-09-19T10:05:00+10:00")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Signal:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        datetime.strptime(self.bar_date, "%Y-%m-%d")
        if self.kind == "entry_verdict" and (not self.verdicts or self.weights):
            raise ValueError("entry_verdict needs verdicts (and no weights)")
        if self.kind == "target_weights" and (not self.weights or self.verdicts):
            raise ValueError("target_weights needs weights (and no verdicts)")
        return self


class SignalRejected(ValueError):
    pass


def learnings_version(path: Path = LEARNINGS_PATH) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path.exists() else "none"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate(raw: dict, settings: Settings, journal: Journal, now: datetime | None = None) -> Signal:
    """Parse and check a signal against the schema, the sleeve and the risk config. Raises SignalRejected."""
    journal.conn.executescript(SIGNALS_SCHEMA)
    now = now or _now()
    try:
        sig = Signal(**raw)
    except ValidationError as exc:
        raise SignalRejected(f"invalid signal: {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}") from exc
    if journal.conn.execute("SELECT 1 FROM ai_signals WHERE id=? AND status != 'rejected'", (sig.id,)).fetchone():
        raise SignalRejected(f"signal id {sig.id} was already submitted")
    sleeve = settings.sleeves.get(sig.sleeve)
    if sleeve is None:
        raise SignalRejected(f"unknown sleeve {sig.sleeve}")
    expected = {"entry_verdict": "analyst-team", "target_weights": "investor-panel"}[sig.kind]
    if sleeve.ai != expected:
        raise SignalRejected(f"sleeve {sig.sleeve} does not take {sig.kind} signals")
    age = now - sig.created_at
    max_age = (timedelta(minutes=settings.risk.ai_signals.max_age_minutes) if sig.kind == "entry_verdict"
               else WEIGHTS_MAX_AGE)
    if age > max_age or age < -timedelta(minutes=5):
        raise SignalRejected(f"signal created {age} ago; must be recent (limit {max_age}) and not in the future")
    symbols = [v.symbol for v in sig.verdicts] + [w.symbol for w in sig.weights]
    if len(symbols) != len(set(symbols)):
        raise SignalRejected("a symbol appears twice")
    if settings.risk.ai_signals.allowed_symbols_only:
        outside = sorted(set(symbols) - set(sleeve.symbols))
        if outside:
            raise SignalRejected(f"symbols outside sleeve {sig.sleeve}: {outside}")
    if sig.kind == "entry_verdict":
        pending = {r["symbol"] for r in pending_requests(journal, sig.sleeve) if r["bar_date"] == sig.bar_date}
        unknown = sorted({v.symbol for v in sig.verdicts} - pending)
        if unknown:
            raise SignalRejected(f"no pending candidate for {unknown} on {sig.bar_date} (see `trader candidates`)")
    else:
        limits = settings.sleeve_limits(sig.sleeve)
        total = sum(w.weight for w in sig.weights)
        if total > limits.max_leverage + 1e-9:
            raise SignalRejected(f"weights add up to {total:.2f}, over the sleeve's {limits.max_leverage:g}x limit")
        too_big = [w.symbol for w in sig.weights if w.weight > limits.max_position_pct_of_sleeve / 100 + 1e-9]
        if too_big:
            raise SignalRejected(f"weight over {limits.max_position_pct_of_sleeve:g}% for {too_big}")
    return sig


def submit(raw: dict, settings: Settings, journal: Journal, now: datetime | None = None) -> Signal:
    """Validate and store. Rejections are stored too (with the reason) and re-raised."""
    journal.conn.executescript(SIGNALS_SCHEMA)
    now = now or _now()
    try:
        sig = validate(raw, settings, journal, now)
    except SignalRejected as exc:
        with journal.conn:
            journal.conn.execute(
                "INSERT OR IGNORE INTO ai_signals (id, created_at, received_at, sleeve_id, kind, bar_date, "
                "learnings_version, payload, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{raw.get('id', 'no-id')}#rejected-{now.timestamp()}", str(raw.get("created_at", "")), now.isoformat(),
                 str(raw.get("sleeve", "")), str(raw.get("kind", "")), str(raw.get("bar_date", "")),
                 str(raw.get("learnings_version", "")), json.dumps(raw)[:20000], "rejected", str(exc)))
        journal.log(f"AI signal rejected: {exc}", level="warning")
        raise
    with journal.conn:
        journal.conn.execute(
            "INSERT INTO ai_signals (id, created_at, received_at, sleeve_id, kind, bar_date, learnings_version, "
            "payload, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sig.id, sig.created_at.astimezone(timezone.utc).isoformat(), now.isoformat(), sig.sleeve, sig.kind,
             sig.bar_date, sig.learnings_version, sig.model_dump_json(), "accepted", None))
        for v in sig.verdicts:
            journal.conn.execute("UPDATE ai_requests SET status='answered' WHERE sleeve_id=? AND symbol=? AND bar_date=?",
                                 (sig.sleeve, v.symbol, sig.bar_date))
    return sig


# --- entry candidates (analyst team) ------------------------------------------------------------------

def request_verdict(journal: Journal, sleeve_id: str, symbol: str, bar_date: str, context: dict,
                    now: datetime | None = None) -> None:
    journal.conn.executescript(SIGNALS_SCHEMA)
    with journal.conn:
        journal.conn.execute(
            "INSERT OR IGNORE INTO ai_requests (sleeve_id, symbol, bar_date, requested_at, context, status) "
            "VALUES (?,?,?,?,?, 'pending')",
            (sleeve_id, symbol, bar_date, (now or _now()).isoformat(), json.dumps(context, default=str)))


def request_age(journal: Journal, sleeve_id: str, symbol: str, bar_date: str, now: datetime | None = None) -> timedelta | None:
    journal.conn.executescript(SIGNALS_SCHEMA)
    row = journal.conn.execute("SELECT requested_at FROM ai_requests WHERE sleeve_id=? AND symbol=? AND bar_date=?",
                               (sleeve_id, symbol, bar_date)).fetchone()
    return (now or _now()) - datetime.fromisoformat(row["requested_at"]) if row else None


def expire_request(journal: Journal, sleeve_id: str, symbol: str, bar_date: str) -> None:
    with journal.conn:
        journal.conn.execute("UPDATE ai_requests SET status='expired' WHERE sleeve_id=? AND symbol=? AND bar_date=?",
                             (sleeve_id, symbol, bar_date))


def pending_requests(journal: Journal, sleeve_id: str | None = None) -> list[dict]:
    journal.conn.executescript(SIGNALS_SCHEMA)
    q = "SELECT * FROM ai_requests WHERE status='pending'"
    rows = journal.conn.execute(q + (" AND sleeve_id=?" if sleeve_id else "") + " ORDER BY requested_at",
                                (sleeve_id,) if sleeve_id else ()).fetchall()
    return [{**dict(r), "context": json.loads(r["context"])} for r in rows]


def verdict_for(journal: Journal, sleeve_id: str, symbol: str, bar_date: str) -> Verdict | None:
    journal.conn.executescript(SIGNALS_SCHEMA)
    rows = journal.conn.execute(
        "SELECT payload FROM ai_signals WHERE sleeve_id=? AND bar_date=? AND kind='entry_verdict' "
        "AND status IN ('accepted','applied') ORDER BY received_at DESC", (sleeve_id, bar_date)).fetchall()
    for r in rows:
        for v in Signal.model_validate_json(r["payload"]).verdicts:
            if v.symbol == symbol:
                return v
    return None


# --- target weights (investor panel) ------------------------------------------------------------------

def latest_weights(journal: Journal, sleeve_id: str, now: datetime | None = None) -> tuple[str, Signal] | None:
    """The newest accepted, not-too-old target_weights signal that hasn't been applied yet."""
    journal.conn.executescript(SIGNALS_SCHEMA)
    row = journal.conn.execute(
        "SELECT id, payload, status FROM ai_signals WHERE sleeve_id=? AND kind='target_weights' "
        "AND status IN ('accepted','applied') ORDER BY received_at DESC LIMIT 1", (sleeve_id,)).fetchone()
    if row is None or row["status"] == "applied":
        return None
    sig = Signal.model_validate_json(row["payload"])
    if (now or _now()) - sig.created_at > WEIGHTS_MAX_AGE:
        return None
    return row["id"], sig


def mark_applied(journal: Journal, signal_id: str) -> None:
    with journal.conn:
        journal.conn.execute("UPDATE ai_signals SET status='applied' WHERE id=?", (signal_id,))


def recent_signals(journal: Journal, limit: int = 20) -> list[dict]:
    journal.conn.executescript(SIGNALS_SCHEMA)
    return [dict(r) for r in journal.conn.execute(
        "SELECT id, received_at, sleeve_id, kind, bar_date, learnings_version, status, reason FROM ai_signals "
        "ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()]
