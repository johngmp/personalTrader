"""What the live sleeves actually did -- measured, not assumed.

Everything here exists because the comparison Gate 2 needs was impossible to make. There has never been
a live Sharpe anywhere in this project: `dashboard.py` computes a simple return difference against B0
and nothing else. And the one cost figure that looked like a measurement -- `cost_haircut_usd` -- takes
the bps out of `config/costs.yaml` and re-applies them to live notional, so "cost after the haircut"
cannot disagree with the backtest's cost assumption by construction. It is the assumption restated in a
different font.

This module produces only numbers the broker and the journal generated, so they are free to disagree
with the backtest. That is the entire point: an assumption nothing can falsify is not evidence.

One trap worth naming, because getting it wrong would have been invisible and flattering. The engine
writes sleeve equity every fifteen minutes -- 39 to 88 rows a day on the live journal. Feeding those
straight into a Sharpe is not merely noisy, it is biased upward: equities are shut for most of those
rows, so the series barely moves between closes, the per-period standard deviation collapses, and the
annualisation factor climbs to roughly 24,000 periods a year. The number that comes out looks superb
and means nothing. So everything here resamples to one point per day first.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trader.journal.db import Journal
from trader.journal.vault import Trip, round_trips
from trader.research.metrics import Metrics, compute_metrics, risk_free_returns

# A stop that filled this far from where it was placed is a gap, not slippage -- a market that jumped
# past the level overnight. Worth reporting separately rather than averaging into a cost estimate.
GAP_BPS = 500.0


@dataclass
class LiveRecord:
    sleeve_id: str
    equity: pd.Series                 # one point per day
    metrics: Metrics | None           # None when there is not enough history to compute one
    trips: list[Trip]
    closed: list[Trip]
    first_day: pd.Timestamp | None
    last_day: pd.Timestamp | None

    @property
    def days_live(self) -> int:
        if self.first_day is None or self.last_day is None:
            return 0
        return int((self.last_day - self.first_day).days)


def daily_equity(journal: Journal, sleeve_id: str, column: str = "equity") -> pd.Series:
    """One equity point per calendar day: the last one recorded that day.

    See the module docstring. Annualising 15-minute snapshots inflates the Sharpe several times over,
    because most of those rows sit inside hours when the market this sleeve trades is closed.

    `column` may be "adjusted_equity", which is equity less the estimated cost haircut. Note what that
    haircut is and is not: it re-applies the bps from `config/costs.yaml` to live notional, so it is the
    backtest's own assumption carried forward, not a measurement. `stop_slippage` below is the
    measurement.
    """
    rows = [r for r in journal.sleeve_equity_history() if r["sleeve_id"] == sleeve_id]
    if not rows:
        return pd.Series(dtype=float)
    series = pd.Series([float(r[column]) for r in rows],
                       index=pd.to_datetime([r["ts"] for r in rows], utc=True, format="ISO8601"))
    return series.groupby(series.index.tz_convert(None).normalize()).last()


def live_record(journal: Journal, sleeve_id: str) -> LiveRecord:
    equity = daily_equity(journal, sleeve_id)
    trips = round_trips(journal, sleeve_id)
    closed = [t for t in trips if not t.is_open]
    metrics = None
    if len(equity) >= 3 and equity.std() > 0:
        metrics = compute_metrics(equity, risk_free=risk_free_returns(equity.index))
    return LiveRecord(sleeve_id=sleeve_id, equity=equity, metrics=metrics, trips=trips, closed=closed,
                      first_day=equity.index[0] if len(equity) else None,
                      last_day=equity.index[-1] if len(equity) else None)


@dataclass
class SlippageReport:
    """Measured from stop orders, where the price we meant to get is actually written down.

    Entries have no recorded decision price -- `orders` stores limit_price and stop_price but not the
    close the decision was taken on -- so entry slippage cannot be measured today. Stops can, they are
    where the money is lost, and this is the first number in the project that the cost model is unable
    to produce for itself.
    """

    n: int
    median_bps: float
    worst_bps: float
    gaps: int                         # fills worse than GAP_BPS: a market that jumped, not a spread

    @property
    def measured(self) -> bool:
        return self.n > 0


def stop_slippage(journal: Journal, sleeve_id: str) -> SlippageReport:
    """How far stop fills landed from the level they were placed at, in basis points.

    Only `stop` and `stop_limit` orders. A market entry also carries a `stop_price` -- the protective
    stop attached to it -- and comparing a market fill against that would measure the stop distance
    rather than any slippage, which is why the order type is filtered rather than the column.
    """
    bps: list[float] = []
    for f in journal.fills(sleeve_id):
        if f["order_type"] not in ("stop", "stop_limit"):
            continue
        intended, got = f["order_stop"], f["price"]
        if not intended or not got:
            continue
        # Signed so that a sell filling BELOW its stop counts as a cost, which is the only direction
        # that can happen to a protective stop, and an unsigned average would hide a favourable fill.
        cost = (intended - got) if f["side"] == "sell" else (got - intended)
        bps.append(10_000 * cost / intended)
    if not bps:
        return SlippageReport(n=0, median_bps=0.0, worst_bps=0.0, gaps=0)
    ordered = sorted(bps)
    return SlippageReport(n=len(ordered), median_bps=float(pd.Series(ordered).median()),
                          worst_bps=ordered[-1], gaps=sum(1 for b in ordered if b > GAP_BPS))


@dataclass
class Shape:
    """The behavioural fingerprint of a set of trades, for comparing live against its own backtest.

    Parameters agreeing is not the same as behaviour agreeing. A sleeve can run the settings its
    backtest chose and still trade nothing like it, because the data feed differs, or sizing binds, or
    an order is refused -- all of which have happened here. The shape is what notices.
    """

    n: int
    median_hold_days: float
    win_rate: float
    avg_win: float
    avg_loss: float
    exit_reasons: dict[str, int]

    @property
    def payoff(self) -> float:
        return abs(self.avg_win / self.avg_loss) if self.avg_loss else 0.0


def live_shape(closed: list[Trip]) -> Shape:
    if not closed:
        return Shape(0, 0.0, 0.0, 0.0, 0.0, {})
    holds, wins, losses, reasons = [], [], [], {}
    for t in closed:
        entry = pd.Timestamp(t.entry_ts)
        exit_ = pd.Timestamp(t.exit_ts)                              # type: ignore[arg-type]
        holds.append((exit_ - entry).total_seconds() / 86400)
        (wins if t.pnl() > 0 else losses).append(t.pnl())
        key = t.exit_reason or "unknown"
        reasons[key] = reasons.get(key, 0) + 1
    return Shape(n=len(closed), median_hold_days=float(pd.Series(holds).median()),
                 win_rate=len(wins) / len(closed),
                 avg_win=sum(wins) / len(wins) if wins else 0.0,
                 avg_loss=sum(losses) / len(losses) if losses else 0.0,
                 exit_reasons=dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
