"""Gate 2: does the thing that is running reproduce the thing that was tested?

PLAN.md has called this "the real test" since September and it has never existed as code. Gate 1 asks
whether a rule worked on history. Only Gate 2 asks whether THIS sleeve, as configured, with this
broker, on this data feed, is doing what that history promised -- and nothing in the project asked that
question, so nothing noticed that S8 claims an out-of-sample Sharpe of 0.85 and is running at -4.50.

Three states, not two. PENDING comes only from missing evidence -- too few days, too few closed trades
-- and never from a bad result. A bare FAIL on three weeks of data would be noise dressed as a verdict;
a bare PENDING on a sleeve that is losing badly would hide the thing most worth knowing. So a sleeve
whose clock has not run out reports what the answer WOULD be today, and says that it is provisional.

Three checks here are not in PLAN.md's spec and are the ones that bite:

- **Check 6, that the sleeve is running what Gate 1 chose.** This is the check that found something.
  S8 traded `atr_mult: 2.5` for two months while the 40-market Gate 1 its own config cited had chosen
  `4.5`: a tighter stop than anything certified, with the certification quoted on the dashboard. Without
  this check, check 5 compares live results against a backtest of different rules and reports the
  outcome as confirmation. Corrected in sleeves.yaml on 2026-11-17; the check stays, because nothing
  else would have noticed and nothing stops it happening again.

  Note which record it compares against: `latest_gate1` cites only PASSING runs, deliberately, so the
  best cell of a failing run cannot authorise what the sleeve trades. The cost is that a sleeve whose
  most recent Gate 1 FAILED is still measured against an older PASS, which is S8's position today.
- **Check 7, that the cited Gate 1 respects the current seal.** Check 6 passing only means the sleeve
  runs the rules some passing backtest chose; it says nothing about whether that backtest is still
  worth anything. S8's 2026-10-03 PASS was scored before the holdout existed, on the out-of-sample
  window `research_log.jsonl` shows read nineteen times with every grid cell's out-of-sample Sharpe on
  the page. Checks 5 and 6 would both go green on it and the sleeve would look certified. A Gate 1 row
  carries `data_end` since 2026-11-17; a row without one is treated as pre-seal, because a record that
  cannot show it respected the seal must not get the benefit of the doubt.
- **Check 7, measured cost against modelled cost.** `cost_haircut_usd` re-applies the configured bps to
  live notional, so the dashboard's "after estimated costs" figure cannot disagree with the backtest by
  construction. `stop_slippage` measures what the broker actually did, which can.

Check 5 needs its `and > 0` conjunct for a reason that is pure arithmetic: `forward >= 0.5 x backtest`
is satisfied when the backtest Sharpe is negative (-0.10 >= 0.5 x -0.18 is true), so without it a losing
sleeve would pass the check that exists to catch exactly that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from trader.config import PROJECT_ROOT, Settings
from trader.journal.db import Journal
from trader.research.holdout import HOLDOUT_START

# When the holdout was sealed: commit 9332810, "Seal the last five years, and make reading them cost a
# receipt". Before this, `research_bars` did not exist and no Gate 1 truncated anything, so any run
# logged earlier read the whole history and its out-of-sample window is the burned one. Used only to
# tell "demonstrably read past the seal" from "ran under the seal but did not record its span", which
# are different enough to deserve different verdicts. Spans have been recorded since 2026-11-17, so
# this constant stops mattering as the pre-seal rows age out of being cited.
SEALED_SINCE = pd.Timestamp("2026-11-16")
from trader.research.forward import LiveRecord, Shape, SlippageReport, daily_equity, live_record, live_shape, stop_slippage
from trader.research.metrics import Metrics, compute_metrics, risk_free_returns

RESEARCH_LOG = PROJECT_ROOT / "reports" / "research_log.jsonl"

MIN_DAYS = 90
MIN_CLOSED_TRADES = 30
SHARPE_RETENTION = 0.5
# How far the measured cost may exceed the modelled one before it stops being the same strategy. Two
# times is generous on purpose: this is a tripwire for "the cost model is wrong by an order of
# magnitude", not a precision instrument.
COST_TOLERANCE = 2.0

PASS, FAIL, PENDING, NA = "pass", "fail", "pending", "na"


@dataclass
class Check:
    name: str
    state: str
    detail: str

    @property
    def mark(self) -> str:
        return {PASS: "PASS", FAIL: "FAIL", PENDING: "WAIT", NA: "n/a"}[self.state]


@dataclass
class Gate1Record:
    ts: str
    strategy: str
    symbols: list[str]
    chosen_params: dict
    oos_sharpe: float
    verdict: str
    data_end: str | None = None    # last bar the run saw; None on rows written before spans were logged

    def seal_state(self) -> tuple[str, str]:
        """Was this run scored on data the current seal allows -- and can it show that it was?

        Four cases, and they are genuinely different, so the check reports them differently rather than
        collapsing "demonstrably burned" and "cannot be shown" into one verdict.

        A row whose span is recorded settles the question outright, either way. A row without one is
        read against when the seal landed: anything before `SEALED_SINCE` ran when nothing truncated
        anything, so its out-of-sample window IS the burned one -- that is a fact about this project's
        history, not an inference. Anything after it very likely came through `research_bars` and so
        respected the seal, but cannot prove it, and a check that cannot tell says PENDING instead of
        picking the answer it prefers. Re-running Gate 1 settles it and costs nothing.
        """
        seal = HOLDOUT_START.date().isoformat()
        if self.data_end is None:
            if pd.Timestamp(self.ts[:10]) < SEALED_SINCE:
                return FAIL, (f"the Gate 1 of {self.ts[:10]} ran before the holdout was sealed on "
                              f"{SEALED_SINCE.date().isoformat()}, so its out-of-sample window is the "
                              f"one inside the data now sealed at {seal}, which research_log.jsonl "
                              f"shows read nineteen times; re-run Gate 1")
            return PENDING, (f"the Gate 1 of {self.ts[:10]} ran under the seal but did not record the "
                             f"data span it used, so it cannot show which years it read; re-run Gate 1 "
                             f"to settle it")
        if pd.Timestamp(self.data_end) >= HOLDOUT_START:
            return FAIL, (f"the Gate 1 of {self.ts[:10]} read data to {self.data_end}, past the seal at "
                          f"{seal}; its out-of-sample Sharpe is not a held-out number")
        return PASS, (f"the Gate 1 of {self.ts[:10]} was scored on data ending {self.data_end}, "
                      f"before the seal at {seal}")


def latest_gate1(strategy: str, symbols: list[str] | None = None,
                 log: Any = None) -> Gate1Record | None:
    """The most recent PASSING Gate 1 for this strategy, preferring one run on the same symbols.

    The basket matters: S8's log holds both a 19-market and a 40-market run, and they chose different
    settings. Citing the wrong one is how a sleeve ends up borrowing the credibility of a backtest it
    is not running.
    """
    path = log if log is not None else RESEARCH_LOG
    if not path.exists():
        return None
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("strategy") == strategy and entry.get("verdict") == "PASS":
            rows.append(entry)
    if not rows:
        return None
    if symbols:
        same = [r for r in rows if sorted(r.get("symbols", [])) == sorted(symbols)]
        rows = same or rows
    best = max(rows, key=lambda r: r["ts"])
    return Gate1Record(ts=best["ts"], strategy=strategy, symbols=best.get("symbols", []),
                       chosen_params=best.get("chosen_params") or {},
                       oos_sharpe=float(best.get("oos_sharpe") or 0.0), verdict=best["verdict"],
                       data_end=best.get("data_end"))


def forward_sharpe_ok(live_sharpe: float, gate1_oos_sharpe: float) -> bool:
    """Does the live result keep enough of what the backtest promised, and is it positive at all?

    The second half is load-bearing and is not in PLAN.md's spec. "Forward Sharpe at least ~50% of
    backtest Sharpe" is satisfied by arithmetic whenever the backtest Sharpe is negative: half of -0.18
    is -0.09, and a live Sharpe of -0.10... fails, but -0.05 passes. A sleeve losing money would clear
    the check that exists to catch a sleeve losing money, and the worse the backtest was, the easier it
    would get. Keeping a fraction of a promise to lose money is not evidence of anything.
    """
    return live_sharpe >= SHARPE_RETENTION * gate1_oos_sharpe and live_sharpe > 0


def _benchmark_metrics(journal: Journal, settings: Settings, benchmark: str,
                       days: pd.DatetimeIndex) -> tuple[Metrics | None, str]:
    """The benchmark measured over EXACTLY the sleeve's own days.

    Comparing a sleeve's life-to-date return against a benchmark's is meaningless when they started on
    different dates, and here they did: B0 began on 2026-09-18 and S8 on 2026-10-08. Over its whole life
    S8 is down 7.45% against B0's 7.69% and looks like the better of the two; over the days they share,
    it need not be.
    """
    if not benchmark:
        return None, "no benchmark configured"
    if benchmark in settings.sleeves:
        series = daily_equity(journal, benchmark).reindex(days).dropna()
        label = f"sleeve {benchmark}"
    else:
        from trader.data.history import load_history
        try:
            closes = load_history(benchmark)["close"]
        except Exception:
            return None, f"{benchmark} history unavailable"
        closes.index = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
        series = closes.reindex(days).ffill().dropna()
        label = f"buy & hold {benchmark}"
    if len(series) < 3 or series.std() == 0:
        return None, f"{label}: not enough overlapping days"
    return compute_metrics(series, risk_free=risk_free_returns(series.index)), label


def unexplained_mismatches(journal: Journal, symbols: list[str], rec: LiveRecord) -> list[str]:
    """Broker-vs-journal differences inside this sleeve's live window that nobody has accounted for.

    PLAN.md asks for "no unexplained broker vs journal differences" and never said what makes one
    explained, which left the check unimplementable: a single stale difference would hold a sleeve back
    for ever with no way to clear it. So an explanation is itself a journal row -- write
    `mismatch_ok:{symbol}:{date}` with the reason and it stops counting, visibly and on the record,
    rather than by someone deciding privately that it did not matter.
    """
    if rec.first_day is None:
        return []
    out = []
    for key in journal.kv_keys("mismatch:%"):
        parts = key.split(":")
        if len(parts) != 3:
            continue
        _, symbol, day = parts
        if symbol not in symbols:
            continue
        when = pd.Timestamp(day)
        if not (rec.first_day <= when <= rec.last_day):          # type: ignore[operator]
            continue
        if journal.kv_get(f"mismatch_ok:{symbol}:{day}") is None:
            out.append(f"{symbol} on {day}")
    return out


@dataclass
class Gate2Result:
    sleeve_id: str
    strategy: str
    record: LiveRecord
    shape: Shape
    slippage: SlippageReport
    gate1: Gate1Record | None
    benchmark: Metrics | None
    benchmark_label: str
    checks: list[Check] = field(default_factory=list)

    @property
    def clock_ready(self) -> bool:
        return not any(c.state == PENDING for c in self.checks)

    @property
    def failing(self) -> list[Check]:
        return [c for c in self.checks if c.state == FAIL]

    @property
    def verdict(self) -> str:
        if not self.clock_ready:
            return "NOT ENOUGH EVIDENCE YET"
        return "FAIL" if self.failing else "PASS"

    @property
    def provisional(self) -> str:
        """What the verdict would be if the clock had run out. Only meaningful while PENDING."""
        return "would FAIL today" if self.failing else "would PASS today"

    @property
    def headline(self) -> str:
        return (f"{self.verdict} ({self.provisional})" if not self.clock_ready else self.verdict)


def evaluate_gate2(sleeve_id: str, settings: Settings, journal: Journal) -> Gate2Result:
    sleeve = settings.sleeves[sleeve_id]
    strategy = sleeve.strategy or sleeve_id
    rec = live_record(journal, sleeve_id)
    shape = live_shape(rec.closed)
    slip = stop_slippage(journal, sleeve_id)
    g1 = latest_gate1(strategy, list(sleeve.symbols))
    bench, bench_label = _benchmark_metrics(journal, settings, sleeve.benchmark or "", rec.equity.index)

    checks: list[Check] = []

    # 1 and 2: the clocks. These can be PENDING or PASS and must never FAIL -- a sleeve is not wrong
    # for being young, and calling it wrong would turn "we do not know yet" into a verdict.
    checks.append(Check("Live long enough", PASS if rec.days_live >= MIN_DAYS else PENDING,
                        f"{rec.days_live} of {MIN_DAYS} days"))
    checks.append(Check("Enough closed trades", PASS if len(rec.closed) >= MIN_CLOSED_TRADES else PENDING,
                        f"{len(rec.closed)} of {MIN_CLOSED_TRADES} closed round trips"))

    # 3: after the estimated cost haircut.
    adjusted = daily_equity(journal, sleeve_id, "adjusted_equity")
    if len(adjusted) >= 2 and adjusted.iloc[0]:
        ret = float(adjusted.iloc[-1] / adjusted.iloc[0] - 1)
        checks.append(Check("Positive after the cost haircut", PASS if ret > 0 else FAIL, f"{ret:+.2%}"))
    else:
        checks.append(Check("Positive after the cost haircut", PENDING, "no adjusted equity recorded"))

    # 4: against the benchmark, over the same days and nothing else.
    if rec.metrics is None:
        checks.append(Check("Beats its benchmark", PENDING, "not enough history for a live Sharpe"))
    elif bench is None:
        checks.append(Check("Beats its benchmark", NA, bench_label))
    else:
        better = rec.metrics.sharpe > bench.sharpe
        checks.append(Check("Beats its benchmark", PASS if better else FAIL,
                            f"Sharpe {rec.metrics.sharpe:+.2f} vs {bench_label} {bench.sharpe:+.2f} "
                            f"over the same {len(rec.equity)} days"))

    # 5: the forward result against what the backtest promised.
    if rec.metrics is None:
        checks.append(Check("Forward Sharpe holds up", PENDING, "not enough history for a live Sharpe"))
    elif g1 is None:
        checks.append(Check("Forward Sharpe holds up", NA, f"no passing Gate 1 recorded for {strategy}"))
    else:
        bar = SHARPE_RETENTION * g1.oos_sharpe
        ok = forward_sharpe_ok(rec.metrics.sharpe, g1.oos_sharpe)
        checks.append(Check("Forward Sharpe holds up", PASS if ok else FAIL,
                            f"live {rec.metrics.sharpe:+.2f} vs {SHARPE_RETENTION:.0%} of the "
                            f"{g1.oos_sharpe:+.2f} Gate 1 measured on {g1.ts[:10]} (need "
                            f"{max(bar, 0.0):+.2f} and above zero)"))

    # 6: is this even the strategy that was tested?
    if g1 is None:
        checks.append(Check("Running the settings Gate 1 chose", NA, f"no passing Gate 1 for {strategy}"))
    else:
        live_params, chosen = dict(sleeve.params or {}), dict(g1.chosen_params)
        differs = {k: (live_params.get(k), chosen.get(k)) for k in set(live_params) | set(chosen)
                   if live_params.get(k) != chosen.get(k)}
        checks.append(Check("Running the settings Gate 1 chose", PASS if not differs else FAIL,
                            "same settings the backtest chose" if not differs else
                            "; ".join(f"{k}: running {a}, Gate 1 chose {b}" for k, (a, b) in sorted(differs.items()))))

    # 7: and was that test run on data the seal still allows?
    #
    # Check 6 can pass on a citation that no longer means anything. S8's parameters agree with the Gate 1
    # of 2026-10-03, but that run was scored before the holdout was sealed, on a window this project has
    # since declared burned -- the same out-of-sample years read nineteen times, with every grid cell's
    # out-of-sample Sharpe printed in front of the researcher each time. "Passed Gate 1" then reads as a
    # clean claim while resting on a number that is not out-of-sample in any useful sense.
    #
    # This is the same defect class as the hardcoded `GateCheck("Realistic backtest", True, ...)` that
    # this project removed: a green tick that does not mean what it looks like. The difference is that
    # the stale citation is invisible rather than merely unearned, so nothing would ever have surfaced
    # it. It is a FAIL and not a note, because a sleeve trading on a certification that does not hold is
    # failing the thing Gate 2 exists to ask.
    if g1 is None:
        checks.append(Check("Gate 1 respects the seal", NA, f"no passing Gate 1 for {strategy}"))
    else:
        checks.append(Check("Gate 1 respects the seal", *g1.seal_state()))

    # 8: the only cost number the configuration cannot produce for itself.
    modelled = max((settings.costs[c].half_spread_bps + settings.costs[c].slippage_bps)
                   for c in {settings.asset_class(s) for s in sleeve.symbols} if c) if sleeve.symbols else 0.0
    if not slip.measured:
        checks.append(Check("Costs match the model", PENDING, "no stop fills to measure yet"))
    else:
        within = slip.median_bps <= COST_TOLERANCE * modelled
        checks.append(Check("Costs match the model", PASS if within else FAIL,
                            f"stops filled {slip.median_bps:+.1f} bps from where they were placed "
                            f"(median of {slip.n}); the model assumes {modelled:.1f} bps"
                            + (f"; {slip.gaps} gapped badly, worst {slip.worst_bps:+.0f} bps" if slip.gaps else "")))

    # 9: shadow sleeves have no broker to disagree with.
    if sleeve.broker.startswith("shadow"):
        checks.append(Check("Broker and journal agree", NA, "shadow sleeve: there is no broker"))
    else:
        unexplained = unexplained_mismatches(journal, list(sleeve.symbols), rec)
        checks.append(Check("Broker and journal agree", PASS if not unexplained else FAIL,
                            "no unexplained differences in this window" if not unexplained else
                            f"{len(unexplained)} unexplained: {', '.join(sorted(unexplained)[:4])}"))

    return Gate2Result(sleeve_id=sleeve_id, strategy=strategy, record=rec, shape=shape, slippage=slip,
                       gate1=g1, benchmark=bench, benchmark_label=bench_label, checks=checks)


GATE2_LOG = PROJECT_ROOT / "reports" / "gate2_log.jsonl"


def log_gate2(result: Gate2Result, log: Any = None) -> None:
    """Append one line per Gate 2 run, to its OWN file.

    Deliberately not `research_log.jsonl`. The dashboard's `latest_research` keys that file by STRATEGY
    and keeps the newest timestamp, so a Gate 2 line would quietly replace S8's Gate 1 card and render
    its out-of-sample Sharpe as a dash; `distinct_trials` also used to read it as a trial ledger. Two
    different questions, two different files.
    """
    from datetime import datetime, timezone

    path = log if log is not None else GATE2_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = result.record
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sleeve": result.sleeve_id, "strategy": result.strategy,
        "verdict": result.verdict, "provisional": None if result.clock_ready else result.provisional,
        "days_live": rec.days_live, "closed_trades": len(rec.closed),
        "live_sharpe": round(rec.metrics.sharpe, 3) if rec.metrics else None,
        "live_return": round(rec.metrics.total_return, 4) if rec.metrics else None,
        "benchmark_sharpe": round(result.benchmark.sharpe, 3) if result.benchmark else None,
        "benchmark": result.benchmark_label,
        "gate1_oos_sharpe": result.gate1.oos_sharpe if result.gate1 else None,
        "gate1_ts": result.gate1.ts if result.gate1 else None,
        "stop_slippage_bps": round(result.slippage.median_bps, 2) if result.slippage.measured else None,
        "win_rate": round(result.shape.win_rate, 3) if result.shape.n else None,
        "payoff": round(result.shape.payoff, 3) if result.shape.n else None,
        "checks": {c.name: c.state for c in result.checks},
        "failing": [c.name for c in result.failing],
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
