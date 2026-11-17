"""The two cards that say whether the rest of the page is worth reading.

A dashboard that shows what a strategy IS doing, and never whether it earned the right to, is how S4
nearly got switched on. These guard the other half of that: whether the sleeve is still reproducing its
backtest, and whether the machinery producing all these numbers is actually running.
"""

from __future__ import annotations

import json

import pytest

from trader.engine.dashboard import _gate2_card, _health_card, latest_gate2, latest_research


def _row(**over) -> dict:
    base = {"ts": "2026-11-16T00:00:00+00:00", "sleeve": "S8", "strategy": "S8",
            "verdict": "NOT ENOUGH EVIDENCE YET", "provisional": "would FAIL today", "days_live": 37,
            "closed_trades": 60, "live_sharpe": -4.5, "benchmark_sharpe": -4.12,
            "gate1_oos_sharpe": 0.85, "failing": ["Beats its benchmark"]}
    return {**base, **over}


def _write(settings, rows, name="gate2_log.jsonl"):
    path = settings.root / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


# --- the Gate 2 card --------------------------------------------------------------------------------

def test_a_waiting_sleeve_that_is_already_failing_says_so_on_the_card():
    """A bare WAITING on a sleeve with sixty closed trades, a 28% win rate and a 0.19 payoff would
    imply that day ninety might rescue it. It will not, and the card must not suggest otherwise."""
    card = _gate2_card(_row())
    assert "WAITING" in card and "would FAIL today" in card
    assert "Beats its benchmark" in card


def test_a_waiting_sleeve_with_nothing_failing_is_not_dressed_up_as_a_problem():
    """Colour is state here, so the words alone are not enough to test: a card reading "every check is
    passing" in alarm red is still telling the reader their sleeve is in trouble."""
    fine = _gate2_card(_row(failing=[], provisional="would PASS today", live_sharpe=0.79))
    assert "WAITING" in fine and "would FAIL today" not in fine
    assert "every check it can answer yet is passing" in fine
    assert "pill open" in fine and "pill closed" not in fine, "a healthy card must not read as an alarm"
    assert "#199e70" in fine and "#d95926" not in fine, "and its dot must be the healthy colour"

    failing = _gate2_card(_row())
    assert "pill closed" in failing and "#d95926" in failing, "a failing card must read as one"


def test_the_card_shows_the_benchmark_beside_the_live_sharpe():
    """The single most misleading number available here is a live Sharpe with nothing next to it. S8's
    -4.50 looks catastrophic until you see B0 at -4.12 over the same days -- and still loses."""
    card = _gate2_card(_row())
    assert "-4.50" in card and "-4.12" in card
    assert "over the same days" in card


def test_a_card_with_no_measurements_yet_renders_without_inventing_numbers():
    card = _gate2_card(_row(live_sharpe=None, benchmark_sharpe=None, gate1_oos_sharpe=None))
    assert "–" in card and "+0.00" not in card


def test_the_newest_verdict_per_sleeve_wins(settings):
    _write(settings, [_row(ts="2026-11-01T00:00:00+00:00", live_sharpe=-1.0),
                      _row(ts="2026-11-16T00:00:00+00:00", live_sharpe=-4.5),
                      _row(ts="2026-11-10T00:00:00+00:00", sleeve="S1", live_sharpe=0.79)])
    rows = latest_gate2(settings)
    assert len(rows) == 2, "one card per sleeve, not one per run"
    assert next(r for r in rows if r["sleeve"] == "S8")["live_sharpe"] == -4.5


def test_gate2_records_do_not_disturb_the_gate1_cards(settings):
    """The reason Gate 2 has its own file. `latest_research` keys research_log.jsonl by STRATEGY and
    keeps the newest timestamp, so a Gate 2 line written there would replace S8's Gate 1 card and
    render its out-of-sample Sharpe as a dash."""
    _write(settings, [{"ts": "2026-10-03T00:00:00+00:00", "strategy": "S8", "verdict": "PASS",
                       "oos_sharpe": 0.85, "symbols": ["SPY"]}], name="research_log.jsonl")
    _write(settings, [_row(ts="2026-11-16T00:00:00+00:00")])

    gate1 = latest_research(settings)
    assert len(gate1) == 1 and gate1[0]["oos_sharpe"] == 0.85, "the Gate 1 card must survive intact"
    assert latest_gate2(settings)[0]["sleeve"] == "S8"


def test_a_half_written_line_does_not_take_the_page_down(settings):
    path = _write(settings, [_row()])
    path.write_text(path.read_text() + '{"sleeve": "S1", "verdi')
    assert len(latest_gate2(settings)) == 1, "a torn write is not worth failing a render over"


# --- the health card --------------------------------------------------------------------------------

def test_a_discarding_alert_channel_is_flagged_not_hidden():
    """It built a null notifier under launchd for seven weeks and threw roughly five thousand messages
    away without a single error. Nothing on the page was wrong; the page was just not reaching anyone."""
    card = _health_card([{"name": "Alerts reaching your phone", "ok": False,
                          "detail": "DISCARDING TELEGRAM_BOT_TOKEN not found"}])
    assert "check" in card and "DISCARDING" in card


def test_a_healthy_row_reads_as_healthy():
    card = _health_card([{"name": "Journal backup", "ok": True, "detail": "newest is 7h old"}])
    assert "pill ok" in card and "pill closed" not in card


def test_the_health_rows_cover_what_has_actually_failed_here(settings, journal):
    """Not a wishlist. Every one of these has silently broken in this project's recorded history.

    The forex watcher's own heartbeat is the newest, added 2026-11-17 after six launchd agents turned
    out not to have executed since 2026-11-13. Nothing on this page could tell: the engine row covers
    the engine, and a day with no forex setup produces exactly the same empty signal list as a day
    nothing ran. It is a separate row because it is the one output acted on by hand.
    """
    from datetime import datetime, timezone

    from trader.engine.dashboard import operational_health

    rows = operational_health(settings, journal, datetime.now(timezone.utc))
    names = {r["name"] for r in rows}
    assert names == {"Alerts reaching your phone", "Journal backup", "Engine heartbeat",
                     "Forex watcher heartbeat", "Trial ledger"}
    assert all(isinstance(r["ok"], bool) and r["detail"] for r in rows), "every row needs a verdict and a reason"


def test_a_watcher_that_has_never_run_is_not_shown_as_healthy(settings, journal):
    """The state the dashboard was in for four days while reporting nothing wrong."""
    from datetime import datetime, timedelta, timezone

    from trader.engine.dashboard import operational_health

    now = datetime.now(timezone.utc)
    row = next(r for r in operational_health(settings, journal, now)
               if r["name"] == "Forex watcher heartbeat")
    assert not row["ok"] and "never run" in row["detail"]

    journal.kv_set("irv:last_pass", (now - timedelta(days=4)).isoformat(timespec="seconds"))
    row = next(r for r in operational_health(settings, journal, now)
               if r["name"] == "Forex watcher heartbeat")
    assert not row["ok"] and "96.0h ago" in row["detail"] and "stall" in row["detail"]

    journal.kv_set("irv:last_pass", (now - timedelta(minutes=2)).isoformat(timespec="seconds"))
    row = next(r for r in operational_health(settings, journal, now)
               if r["name"] == "Forex watcher heartbeat")
    assert row["ok"], "a pass two minutes ago is healthy"


def test_an_altered_trial_ledger_shows_up_on_the_dashboard(settings, journal):
    """Gate 1 refuses to score against an edited ledger, but that refusal only appears when someone
    runs it. The page should say so without being asked."""
    from datetime import datetime, timezone

    from trader.engine.dashboard import operational_health
    from trader.research import ledger

    ledger.TRIAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ledger.TRIAL_LEDGER.write_text('{"ts":"x","strategy":"S1","trial":"t","prev":"wrong","hash":"bad"}\n')
    row = next(r for r in operational_health(settings, journal, datetime.now(timezone.utc))
               if r["name"] == "Trial ledger")
    assert not row["ok"]


def test_a_stale_engine_is_not_reported_as_healthy(settings, journal):
    """Written originally against `last_engine_run`, the same wrong key the dashboard read, so it
    passed while the row was incapable of ever going green in production. A test that shares the bug
    confirms the bug. It uses the shared constant now, which is what the engine writes."""
    from datetime import datetime, timedelta, timezone

    from trader.engine.dashboard import operational_health
    from trader.journal.db import Journal

    now = datetime.now(timezone.utc)
    journal.kv_set(Journal.ENGINE_HEARTBEAT, (now - timedelta(hours=9)).isoformat())
    row = next(r for r in operational_health(settings, journal, now) if r["name"] == "Engine heartbeat")
    assert not row["ok"] and "9" in row["detail"]

    journal.kv_set(Journal.ENGINE_HEARTBEAT, (now - timedelta(minutes=5)).isoformat())
    row = next(r for r in operational_health(settings, journal, now) if r["name"] == "Engine heartbeat")
    assert row["ok"]


def test_the_engine_heartbeat_row_reads_the_key_the_engine_actually_writes(settings, journal):
    """This row said "never run" from the day it was added, whatever the engine was doing.

    `live.py` wrote `last_run`; the health card read `last_engine_run`, a name that exists only as a
    field inside a JSON payload in `cli.py`. So on 2026-11-17, with the engine's real last run recorded
    in the journal at 2026-11-14, the dashboard reported "never run" -- and during four days of actual
    downtime it would have reported exactly the same thing. A row that cannot change is worse than no
    row, because it occupies the space where the answer should be.

    Driven through the WRITER rather than by writing the key here, so the two cannot drift apart again:
    if the engine starts writing somewhere else, this fails.
    """
    from datetime import datetime, timezone

    from trader.engine.dashboard import operational_health
    from trader.journal.db import Journal

    now = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
    before = next(r for r in operational_health(settings, journal, now) if r["name"] == "Engine heartbeat")
    assert not before["ok"] and "never run" in before["detail"]

    journal.kv_set(Journal.ENGINE_HEARTBEAT, now.isoformat(timespec="seconds"))
    after = next(r for r in operational_health(settings, journal, now) if r["name"] == "Engine heartbeat")
    assert after["ok"], f"a run just now must read as healthy, got: {after['detail']}"


def test_the_engine_writes_its_heartbeat_through_the_shared_constant():
    """The structural half. A literal in either place is how the names drifted the first time."""
    import inspect

    from trader.engine import dashboard, live

    assert 'kv_get("last_engine_run")' not in inspect.getsource(dashboard)
    assert 'kv_set("last_run"' not in inspect.getsource(live)
    assert "Journal.ENGINE_HEARTBEAT" in inspect.getsource(live)
    assert "Journal.ENGINE_HEARTBEAT" in inspect.getsource(dashboard)
