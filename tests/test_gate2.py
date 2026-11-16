"""Gate 2: the forward test PLAN.md has called "the real test" since September and that never existed.

The trap these tests mostly guard is not a wrong threshold. It is a comparison that cannot fail --
measuring the sleeve against something derived from the same assumption, or over a window that flatters
it. Both were present in the code before this: "after estimated costs" re-applies the configured bps to
live notional, and the dashboard compares life-to-date returns between sleeves that started on
different dates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.research.forward import daily_equity, live_shape, stop_slippage
from trader.research.gate2 import (FAIL, MIN_CLOSED_TRADES, MIN_DAYS, NA, PASS, PENDING, SEALED_SINCE,
                                   Check, Gate1Record, Gate2Result, latest_gate1, log_gate2)
from trader.research.holdout import HOLDOUT_START

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _equity(journal, sleeve_id, days, per_day=1, start=10_000.0, step=10.0):
    for d in range(days):
        for k in range(per_day):
            ts = (START + timedelta(days=d, minutes=15 * k)).isoformat()
            value = start + step * d
            journal.conn.execute(
                "INSERT INTO sleeve_equity (ts, sleeve_id, equity, adjusted_equity, invested) "
                "VALUES (?,?,?,?,?)", (ts, sleeve_id, value, value * 0.99, 0.5))
    journal.conn.commit()


# --- the one that would have been invisible and flattering ------------------------------------------

def test_the_live_sharpe_comes_from_daily_points_not_from_every_engine_cycle(journal):
    """The engine writes sleeve equity every fifteen minutes -- 39 to 88 rows a day on the real journal.

    Feeding those straight into a Sharpe is not merely noisy, it is biased upward: the market this
    sleeve trades is shut for most of those rows, so the series barely moves between closes, the
    per-period standard deviation collapses, and the annualisation factor climbs towards 24,000 periods
    a year. The number that comes out looks superb and means nothing.
    """
    _equity(journal, "S1", days=40, per_day=1)
    _equity(journal, "S8", days=40, per_day=96)

    one_a_day = daily_equity(journal, "S1")
    many_a_day = daily_equity(journal, "S8")
    assert len(one_a_day) == len(many_a_day) == 40, "both must collapse to one point per day"
    assert (one_a_day.values == many_a_day.values).all(), "the extra rows must change nothing at all"

    raw = [r["equity"] for r in journal.sleeve_equity_history() if r["sleeve_id"] == "S8"]
    assert len(raw) == 40 * 96, "the test is not exercising the resample unless the raw rows are there"


def test_a_sleeve_is_measured_against_its_benchmark_over_the_same_days(journal, settings):
    """Comparing life-to-date returns between sleeves that started on different dates is not a
    comparison. On the real journal B0 began on 2026-09-18 and S8 on 2026-10-08, and over its whole
    life S8 looks like the better of the two. Over the days they actually share, it is not.
    """
    from trader.research.gate2 import _benchmark_metrics

    _equity(journal, "B0", days=60, start=10_000.0, step=5.0)       # the benchmark, running longer
    _equity(journal, "S8", days=20, start=10_000.0, step=1.0)       # the sleeve, started later

    sleeve_days = daily_equity(journal, "S8").index
    metrics, label = _benchmark_metrics(journal, settings, "B0", sleeve_days)
    assert metrics is not None and "B0" in label
    assert metrics.start == str(sleeve_days[0].date()), "the benchmark must start where the sleeve does"
    assert metrics.end == str(sleeve_days[-1].date()), "and end where it ends"


# --- the clocks can wait, but must never condemn -------------------------------------------------------

def test_a_young_sleeve_is_pending_and_never_failed_for_being_young(journal, settings):
    from trader.research.gate2 import evaluate_gate2

    _equity(journal, "S1", days=10)
    result = evaluate_gate2("S1", settings, journal)
    clocks = [c for c in result.checks if c.name in ("Live long enough", "Enough closed trades")]
    assert len(clocks) == 2
    assert all(c.state == PENDING for c in clocks), "a sleeve is not wrong for being young"
    assert result.verdict == "NOT ENOUGH EVIDENCE YET"


def test_a_pending_verdict_still_says_what_the_answer_would_be_today():
    """A bare PENDING would hide the thing most worth knowing. S8 has sixty closed trades, a 28% win
    rate and a payoff of 0.19 -- waiting for day ninety will not rescue that, and the report should not
    imply it might."""
    losing = Gate2Result(sleeve_id="S8", strategy="S8", record=None, shape=None, slippage=None,  # type: ignore[arg-type]
                         gate1=None, benchmark=None, benchmark_label="",
                         checks=[Check("Live long enough", PENDING, ""),
                                 Check("Positive after the cost haircut", FAIL, "-7.93%")])
    assert losing.verdict == "NOT ENOUGH EVIDENCE YET"
    assert losing.provisional == "would FAIL today"
    assert "would FAIL today" in losing.headline

    winning = Gate2Result(sleeve_id="S1", strategy="S1", record=None, shape=None, slippage=None,  # type: ignore[arg-type]
                          gate1=None, benchmark=None, benchmark_label="",
                          checks=[Check("Live long enough", PENDING, ""),
                                  Check("Positive after the cost haircut", PASS, "+1.4%")])
    assert winning.provisional == "would PASS today"


def test_a_sleeve_whose_clock_has_run_out_gets_a_real_verdict():
    done = [Check("Live long enough", PASS, ""), Check("Beats its benchmark", PASS, "")]
    assert Gate2Result("S1", "S1", None, None, None, None, None, "", done).verdict == "PASS"  # type: ignore[arg-type]
    done[1] = Check("Beats its benchmark", FAIL, "")
    assert Gate2Result("S1", "S1", None, None, None, None, None, "", done).verdict == "FAIL"  # type: ignore[arg-type]


def test_a_check_that_does_not_apply_does_not_block_a_pass():
    """A shadow sleeve has no broker to disagree with, and B0 has no Gate 1. Neither is a failure."""
    checks = [Check("Live long enough", PASS, ""), Check("Broker and journal agree", NA, "shadow")]
    assert Gate2Result("S9", "S4", None, None, None, None, None, "", checks).verdict == "PASS"  # type: ignore[arg-type]


# --- the three checks that are not in PLAN.md's spec, and are the ones that bite -----------------------

def test_a_sleeve_running_settings_gate1_did_not_choose_fails(journal, settings, tmp_path):
    """S8 trades atr_mult 2.5 while the 40-market Gate 1 its own config cites chose 4.5. Without this,
    the forward-Sharpe check compares live results against a backtest of different rules and reports
    the outcome as confirmation."""
    from trader.research.gate2 import evaluate_gate2

    log = tmp_path / "research_log.jsonl"
    log.write_text(json.dumps({
        "ts": "2026-10-03T06:29:04+00:00", "strategy": "S1", "symbols": sorted(settings.sleeves["S1"].symbols),
        "verdict": "PASS", "oos_sharpe": 0.85, "chosen_params": {"fast": 20, "slow": 200, "atr_mult": 4.5},
    }) + "\n")
    import trader.research.gate2 as g2
    settings.sleeves["S1"] = settings.sleeves["S1"].model_copy(
        update={"params": {"fast": 20, "slow": 200, "atr_mult": 2.5}})
    _equity(journal, "S1", days=10)

    original, g2.RESEARCH_LOG = g2.RESEARCH_LOG, log
    try:
        result = evaluate_gate2("S1", settings, journal)
    finally:
        g2.RESEARCH_LOG = original
    check = next(c for c in result.checks if c.name == "Running the settings Gate 1 chose")
    assert check.state == FAIL
    assert "2.5" in check.detail and "4.5" in check.detail, "it must name both values"


def test_the_cost_check_measures_what_the_broker_did_not_what_the_config_says(journal):
    """`cost_haircut_usd` re-applies the configured bps to live notional, so "cost after the haircut"
    cannot disagree with the backtest by construction. It is the assumption restated. Stop fills are
    the one place the price we MEANT to get is written down, so they can disagree -- and must."""
    journal.conn.execute(
        "INSERT INTO orders (client_order_id, created_at, updated_at, sleeve_id, symbol, side, "
        "order_type, stop_price, status, qty) "
        "VALUES ('o1', ?, ?, 'S8', 'SPY', 'sell', 'stop_limit', 100.0, 'filled', 1)",
        (START.isoformat(), START.isoformat()))
    journal.conn.execute(
        "INSERT INTO fills (client_order_id, ts, sleeve_id, symbol, side, qty, price, broker_fee_usd, "
        "fee_qty, cost_haircut_usd) VALUES ('o1', ?, 'S8', 'SPY', 'sell', 1, 99.0, 0, 0, 0)",
        (START.isoformat(),))
    journal.conn.commit()

    slip = stop_slippage(journal, "S8")
    assert slip.measured and slip.n == 1
    assert slip.median_bps == pytest.approx(100.0), "a stop at 100 filling at 99 is 100 bps of cost"


def test_a_market_entrys_protective_stop_is_not_mistaken_for_slippage(journal):
    """A market buy also carries a stop_price -- the protective stop attached to it. Comparing the fill
    against that would measure the stop DISTANCE and report it as a cost, which on the real journal
    would have read as roughly 700 bps of slippage on every entry."""
    journal.conn.execute(
        "INSERT INTO orders (client_order_id, created_at, updated_at, sleeve_id, symbol, side, "
        "order_type, stop_price, status, qty) "
        "VALUES ('o2', ?, ?, 'S8', 'SPY', 'buy', 'market', 90.0, 'filled', 1)",
        (START.isoformat(), START.isoformat()))
    journal.conn.execute(
        "INSERT INTO fills (client_order_id, ts, sleeve_id, symbol, side, qty, price, broker_fee_usd, "
        "fee_qty, cost_haircut_usd) VALUES ('o2', ?, 'S8', 'SPY', 'buy', 1, 100.0, 0, 0, 0)",
        (START.isoformat(),))
    journal.conn.commit()
    assert not stop_slippage(journal, "S8").measured, "market orders have no intended price recorded"


# --- the arithmetic trap in check 5 ----------------------------------------------------------------------

def test_a_losing_sleeve_cannot_pass_by_beating_half_of_a_negative_backtest():
    """The trap is pure arithmetic. Half of a NEGATIVE backtest Sharpe is a bar below zero, so a sleeve
    that is losing money clears it -- and the worse the backtest was, the easier the bar gets."""
    from trader.research.gate2 import SHARPE_RETENTION, forward_sharpe_ok

    backtest = -0.18
    live = -0.05
    assert live >= SHARPE_RETENTION * backtest, \
        "the premise of this test: the retention half of the rule is satisfied by a LOSING sleeve"
    assert not forward_sharpe_ok(live, backtest), "and it must still fail, on the `> 0` conjunct"


def test_the_forward_check_still_passes_a_sleeve_that_is_genuinely_holding_up():
    """A rule that rejects everything is not a check."""
    from trader.research.gate2 import forward_sharpe_ok

    assert forward_sharpe_ok(0.60, 0.85), "keeps most of a good backtest"
    assert not forward_sharpe_ok(0.20, 0.85), "keeps less than half of it"
    assert not forward_sharpe_ok(0.0, 0.85), "flat is not holding up"


# --- the explained-mismatch mechanism -------------------------------------------------------------------

def test_a_broker_difference_can_be_explained_on_the_record_rather_than_privately(journal, settings):
    """PLAN.md asks for "no unexplained broker vs journal differences" and never said what makes one
    explained, which left a single stale difference able to hold a sleeve back for ever. An explanation
    is now itself a journal row, so clearing one is visible instead of being someone's private call."""
    from trader.research.forward import live_record
    from trader.research.gate2 import unexplained_mismatches

    _equity(journal, "S1", days=10)
    rec = live_record(journal, "S1")
    symbol = settings.sleeves["S1"].symbols[0]
    day = str(rec.first_day.date())                                   # type: ignore[union-attr]
    journal.kv_set(f"mismatch:{symbol}:{day}", "1")
    assert unexplained_mismatches(journal, list(settings.sleeves["S1"].symbols), rec)

    journal.kv_set(f"mismatch_ok:{symbol}:{day}", "a dividend the broker booked a day early")
    assert not unexplained_mismatches(journal, list(settings.sleeves["S1"].symbols), rec)


def test_another_sleeves_mismatch_is_not_held_against_this_one(journal, settings):
    from trader.research.forward import live_record
    from trader.research.gate2 import unexplained_mismatches

    _equity(journal, "S1", days=10)
    rec = live_record(journal, "S1")
    journal.kv_set(f"mismatch:NOTMINE:{rec.first_day.date()}", "1")   # type: ignore[union-attr]
    assert not unexplained_mismatches(journal, list(settings.sleeves["S1"].symbols), rec)


# --- picking the right Gate 1 to compare against ------------------------------------------------------------

def test_the_gate1_used_is_the_one_run_on_this_sleeves_own_symbols(tmp_path):
    """S8's log holds a 19-market and a 40-market run that chose different settings. Citing the wrong
    one is how a sleeve borrows the credibility of a backtest it is not running."""
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-09-19T09:22:28+00:00", "strategy": "S8", "symbols": ["A", "B"], "verdict": "PASS",
         "oos_sharpe": 0.98, "chosen_params": {"atr_mult": 2.5}},
        {"ts": "2026-10-03T06:29:04+00:00", "strategy": "S8", "symbols": ["A", "B", "C"], "verdict": "PASS",
         "oos_sharpe": 0.85, "chosen_params": {"atr_mult": 4.5}},
    ]) + "\n")
    assert latest_gate1("S8", ["A", "B"], log).chosen_params == {"atr_mult": 2.5}
    assert latest_gate1("S8", ["A", "B", "C"], log).chosen_params == {"atr_mult": 4.5}


def test_a_failed_gate1_is_never_cited_as_the_backtest_to_live_up_to(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-09-18T00:00:00+00:00", "strategy": "S4", "symbols": ["A"], "verdict": "PASS",
         "oos_sharpe": 0.29, "chosen_params": {"atr_mult": 2.5}},
        {"ts": "2026-10-02T00:00:00+00:00", "strategy": "S4", "symbols": ["A"], "verdict": "FAIL",
         "oos_sharpe": -0.18, "chosen_params": {"atr_mult": 3.5}},
    ]) + "\n")
    found = latest_gate1("S4", ["A"], log)
    assert found.verdict == "PASS" and found.oos_sharpe == 0.29, "the later run FAILED and proves nothing"


# --- persistence ------------------------------------------------------------------------------------------

def test_gate2_records_go_to_their_own_file_not_the_research_log(journal, settings, tmp_path):
    """The dashboard's `latest_research` keys research_log.jsonl by STRATEGY and keeps the newest
    timestamp, so a Gate 2 line written there would quietly replace S8's Gate 1 card and render its
    out-of-sample Sharpe as a dash."""
    from trader.research.gate2 import evaluate_gate2

    _equity(journal, "S1", days=10)
    out = tmp_path / "gate2_log.jsonl"
    log_gate2(evaluate_gate2("S1", settings, journal), log=out)
    row = json.loads(out.read_text().splitlines()[0])
    assert row["sleeve"] == "S1" and row["verdict"] == "NOT ENOUGH EVIDENCE YET"
    assert "checks" in row and row["provisional"] in ("would PASS today", "would FAIL today")


def test_the_payoff_and_win_rate_describe_the_same_trades():
    """S8's shape is the finding: a 0.19 payoff needs an 84% win rate to break even and gets 28%. If
    these two came from different sets of trades the conclusion would be worthless."""
    from trader.journal.vault import Trip

    def trip(pnl, days):
        t = Trip(sleeve_id="S8", symbol="X", trade_id="t", entry_ts="2026-01-01T00:00:00+00:00",
                 entry_reason="", initial_stop=None)
        t.exit_ts = (START + timedelta(days=days)).isoformat()
        t.proceeds, t.buy_cost = (pnl, 0.0) if pnl > 0 else (0.0, -pnl)
        return t

    shape = live_shape([trip(10, 5), trip(10, 5), trip(-40, 5), trip(-40, 5)])
    assert shape.n == 4 and shape.win_rate == 0.5
    assert shape.payoff == pytest.approx(0.25)
    assert 1 / (1 + shape.payoff) == pytest.approx(0.8), "needs an 80% win rate at a 0.25 payoff"

# --- check 7: is the Gate 1 being cited still worth citing? -------------------------------------------
#
# Check 6 passing only says the sleeve runs the rules SOME passing backtest chose. S8's parameters agree
# with the Gate 1 of 2026-10-03 -- a run scored before the holdout existed, on the out-of-sample window
# research_log.jsonl shows read nineteen times with every grid cell's out-of-sample Sharpe on the page.
# Checks 5 and 6 both go green on it, and the sleeve reads as certified. These four tests pin the four
# cases apart, because collapsing "demonstrably burned" into "cannot be shown" in either direction is
# how this check would become either noise or decoration.

def _rec(**kw):
    base = dict(ts="2026-12-01T00:00:00+00:00", strategy="S8", symbols=["A"],
                chosen_params={"atr_mult": 4.5}, oos_sharpe=0.85, verdict="PASS")
    return Gate1Record(**{**base, **kw})


def test_a_gate1_scored_under_the_seal_passes_the_seal_check():
    """The baseline. If this does not pass, every other case below is meaningless."""
    state, detail = _rec(data_end="2021-12-31").seal_state()
    assert state == PASS
    assert "2021-12-31" in detail and HOLDOUT_START.date().isoformat() in detail


def test_a_gate1_that_read_past_the_seal_fails_however_good_its_sharpe():
    state, detail = _rec(data_end="2026-10-01", oos_sharpe=2.5).seal_state()
    assert state == FAIL
    assert "not a held-out number" in detail


def test_a_gate1_older_than_the_seal_fails_rather_than_pleading_ignorance():
    """S8's and S9's real position. These rows carry no span because spans were not logged yet -- but
    `research_bars` did not exist either, so nothing truncated anything and the window they called
    out-of-sample IS the burned one. That is a fact about this project's history, not a guess, so the
    check states it instead of reporting the span as merely unknown.
    """
    state, detail = _rec(ts="2026-10-03T06:29:04+00:00", data_end=None).seal_state()
    assert state == FAIL
    assert "before the holdout was sealed" in detail


def test_a_gate1_run_under_the_seal_without_a_recorded_span_says_it_cannot_tell():
    """The case that must NOT be a FAIL and must NOT be a PASS.

    S1's Gate 1 of 2026-11-16 ran after the seal landed and almost certainly came through
    `research_bars`, but predates span logging, so nothing in the record proves which years it read.
    Reporting PASS would hand it a certification it has not shown; reporting FAIL would accuse it of
    something it probably did not do, and a check that cries wolf gets switched off. PENDING is the
    state this module reserves for missing evidence, and re-running Gate 1 settles it for free.
    """
    after = (SEALED_SINCE + pd.Timedelta(days=1)).date().isoformat()
    state, detail = _rec(ts=f"{after}T00:00:00+00:00", data_end=None).seal_state()
    assert state == PENDING
    assert "did not record the data span" in detail


def test_matching_the_chosen_parameters_does_not_satisfy_the_seal_check(journal, settings, tmp_path):
    """The whole point: a sleeve can agree perfectly with its Gate 1 and still be citing a dead number.
    Check 6 must pass here and check 7 must fail, on the same sleeve in the same run."""
    from trader.research.gate2 import evaluate_gate2
    import trader.research.gate2 as g2

    params = dict(settings.sleeves["S1"].params)
    log = tmp_path / "research_log.jsonl"
    log.write_text(json.dumps({
        "ts": "2026-10-03T06:29:04+00:00", "strategy": "S1",
        "symbols": sorted(settings.sleeves["S1"].symbols), "verdict": "PASS", "oos_sharpe": 0.85,
        "chosen_params": params,          # exactly what the sleeve runs
    }) + "\n")
    _equity(journal, "S1", days=10)

    original, g2.RESEARCH_LOG = g2.RESEARCH_LOG, log
    try:
        result = evaluate_gate2("S1", settings, journal)
    finally:
        g2.RESEARCH_LOG = original
    by_name = {c.name: c for c in result.checks}
    assert by_name["Running the settings Gate 1 chose"].state == PASS
    assert by_name["Gate 1 respects the seal"].state == FAIL


def test_gate1_now_records_the_span_it_was_allowed_to_see(tmp_path, monkeypatch):
    """End-to-end through `log_result`, not against a hand-built row. If the field stops being written,
    every future Gate 1 silently becomes un-auditable and check 7 degrades to PENDING forever -- which
    would look like the check working.
    """
    import trader.research.evaluate as ev

    log = tmp_path / "research_log.jsonl"
    monkeypatch.setattr(ev, "RESEARCH_LOG", log)

    class _Fake:
        strategy, symbols, trials = "S8", ["SPY"], 18
        grid_table = [{"params": {"atr_mult": 4.5}}]
        chosen_params = {"atr_mult": 4.5}
        end = pd.Timestamp("2021-12-31")
        is_metrics = oos_metrics = bench_oos = type("M", (), {"sharpe": 0.5})()
        wf_metrics = None
        checks: list = []
        passed = True

    ev.log_result(_Fake(), None, log=log)          # type: ignore[arg-type]
    row = json.loads(log.read_text().splitlines()[0])
    assert row["data_end"] == "2021-12-31"
    assert pd.Timestamp(row["data_end"]) < HOLDOUT_START
    assert latest_gate1("S8", ["SPY"], log).seal_state()[0] == PASS
