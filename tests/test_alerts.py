"""The alert channel: that messages are actually sent, and that one problem is not ninety-six messages.

A2 of the plan. The engine generated roughly 5,000 notifications over its life and 17 arrived. These
tests cover both halves of why: a notifier that discarded everything without ever reporting a failure,
and a send loop with no idea it had said the same thing all day.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from trader.engine.live import _alert_shape, alert_counts, worth_sending
from trader.notify.telegram import NullNotifier, Telegram, from_env

DAY = date(2026, 11, 12)


# --- the root cause: a notifier built before .env was loaded --------------------------------------

def test_from_env_finds_credentials_without_the_caller_loading_dotenv_first(monkeypatch, tmp_path):
    """`trader run` called from_env() BEFORE load_secrets(), so under launchd it got a NullNotifier.

    This is the whole bug. from_env must not depend on a caller having loaded .env already.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert isinstance(from_env(), Telegram), "from_env must load .env itself, not trust the caller"


def test_from_env_says_which_credential_is_missing(monkeypatch, tmp_path):
    import trader.notify.telegram as tg

    monkeypatch.setattr(tg, "PROJECT_ROOT", tmp_path)       # an empty project: no .env to find
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = tg.from_env()
    assert isinstance(n, NullNotifier)
    assert "TELEGRAM_BOT_TOKEN" in n.reason and "TELEGRAM_CHAT_ID" in n.reason
    assert n.misconfigured, "a notifier that is null because it found no credentials is a failure"


# --- the check that would have caught it ----------------------------------------------------------

def _coordinator(settings, journal, notifier):
    from trader.brokers.sim import SimBroker
    from trader.engine.live import Coordinator, Engine

    engine = Engine(settings, SimBroker(cash=100_000), journal, None, notifier=notifier)
    return Coordinator(settings, journal, [engine], notifier,
                       clock=lambda: datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc))


def test_a_run_that_discarded_its_alerts_says_so(settings, journal):
    """The failure nothing looked at: messages 'sent' into a list and dropped, with no error anywhere."""
    from trader.engine.live import RunReport

    notifier = NullNotifier(reason="TELEGRAM_BOT_TOKEN not found in /x/.env", misconfigured=True)
    notifier.send("a fill you never saw")
    notifier.send("a stop you never saw")
    report = RunReport(started=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc))

    _coordinator(settings, journal, notifier)._check_alerts_got_through(report)

    assert any("2 alert(s) went nowhere" in p for p in report.problems), report.problems
    assert any("TELEGRAM_BOT_TOKEN" in p for p in report.problems)


def test_a_dry_run_notifier_is_not_reported_as_a_failure(settings, journal):
    """A dry run, and every test harness, discards on purpose. Only `from_env` sets the flag."""
    from trader.engine.live import RunReport

    notifier = NullNotifier(reason="dry run")
    notifier.send("pretend fill")
    report = RunReport(started=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc))

    _coordinator(settings, journal, notifier)._check_alerts_got_through(report)

    assert report.problems == [], "a dry run discards on purpose; that is not a delivery failure"


# --- the volume: dedup and cap --------------------------------------------------------------------

def test_the_same_problem_all_day_is_one_message(journal):
    """337 identical `protective stops failed: ConnectTimeout` events -> one alert."""
    msg = ("protective stops failed: ConnectTimeout: HTTPSConnectionPool(host='paper-api.alpaca.markets', "
           "port=443): Read timed out")
    first = worth_sending(journal, "alpaca", DAY, [msg])
    rest = [worth_sending(journal, "alpaca", DAY, [msg]) for _ in range(95)]

    assert first == [msg]
    assert all(r == [] for r in rest), "the same problem every 15 minutes is one thing to know, not 96"


def test_a_volatile_tail_does_not_make_it_a_new_problem(journal):
    """The storm's messages differ in the retry detail. If that counted, dedup would never fire."""
    worth_sending(journal, "alpaca", DAY, ["vault failed: ReadTimeout: HTTPSConnectionPool(host='a') after 1 try"])
    again = worth_sending(journal, "alpaca", DAY, ["vault failed: ReadTimeout: HTTPSConnectionPool(host='b') after 9 tries"])
    assert again == []


def test_different_sleeves_and_symbols_stay_separate(journal):
    """Collapsing too hard is the opposite failure: you would hear about S8 and never about S9."""
    sent = worth_sending(journal, "alpaca", DAY, [
        "S8 decisions failed: RuntimeError: no data",
        "S9 decisions failed: RuntimeError: no data",
        "could not place S8 stop for XLK: insufficient qty",
        "could not place S8 stop for XLE: insufficient qty",
    ])
    assert len(sent) == 4, sent


def test_tomorrow_the_problem_is_worth_saying_again(journal):
    msg = "reconcile failed: ConnectTimeout: nope"
    assert worth_sending(journal, "alpaca", DAY, [msg]) == [msg]
    assert worth_sending(journal, "alpaca", date(2026, 11, 13), [msg]) == [msg]


def test_one_run_cannot_send_more_than_the_cap(journal):
    problems = [f"step{i} failed: RuntimeError: boom" for i in range(12)]
    sent = worth_sending(journal, "alpaca", DAY, problems, cap=5)
    assert len(sent) == 6, sent                      # 5 problems + one line standing in for the rest
    assert sent[-1] == "and 7 more new problem(s) this run -- see the dashboard"


def test_under_the_cap_nothing_is_added(journal):
    sent = worth_sending(journal, "alpaca", DAY, ["a failed: X: y", "b failed: X: y"], cap=5)
    assert sent == ["a failed: X: y", "b failed: X: y"]


def test_the_count_is_kept_so_it_can_be_shown(journal):
    """Suppressing a message is only honest if the number survives somewhere you can see it."""
    msg = "protective stops failed: ConnectTimeout: whatever"
    for _ in range(337):
        worth_sending(journal, "alpaca", DAY, [msg])
    assert alert_counts(journal, [DAY]) == [("protective stops failed: ConnectTimeout", 337)]
    assert alert_counts(journal, [date(2026, 11, 13)]) == []


def test_two_brokers_are_counted_apart(journal):
    msg = "reconcile failed: ConnectTimeout: nope"
    assert worth_sending(journal, "alpaca", DAY, [msg]) == [msg]
    assert worth_sending(journal, "oanda", DAY, [msg]) == [msg], "each broker's failure is its own news"


def test_alert_shape_keeps_what_identifies_the_problem():
    assert _alert_shape("vault failed: ReadTimeout: HTTPSConnectionPool(host='a')") == "vault failed: ReadTimeout"
    assert _alert_shape("kill switch on") == "kill switch on"


# --- that it is actually wired in ------------------------------------------------------------------

def test_a_real_run_sends_a_recurring_failure_once(settings, tmp_path):
    """Through Engine._run, not by calling the helper.

    Tests that call worth_sending directly prove the helper works and nothing about whether the send
    loop uses it. This runs the engine four times over one day with a broker that always fails, the
    way a flapping Alpaca connection did 337 times, and counts the messages.

    One dead connection breaks three separate steps, so three distinct problems is right and three
    messages is right -- they are not the same problem, and collapsing across steps would hide a
    genuine single-step failure. What must not happen is the same step reporting on every run.
    """
    from trader.brokers.sim import SimBroker
    from trader.engine.live import Engine
    from trader.journal.db import Journal

    class Boom(SimBroker):
        def get_positions(self):                   # breaks `reconcile`, as a read timeout did
            raise ConnectionError("HTTPSConnectionPool(host='paper-api.alpaca.markets'): timed out")

    for sid in ("S8", "S1"):
        settings.sleeves[sid] = settings.sleeves[sid].model_copy(update={"enabled": False})
    journal = Journal(tmp_path / "j.db")
    notifier = NullNotifier()
    clock = lambda: datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)  # noqa: E731
    engine = Engine(settings, Boom(cash=100_000, name="alpaca"), journal, None,
                    notifier=notifier, clock=clock, handle_commands=False, shared_outputs=False)
    try:
        reports = [engine.run_once() for _ in range(4)]
    finally:
        journal.close()

    timeouts = [m for m in notifier.sent if "timed out" in m]
    recorded = [p for r in reports for p in r.problems if "timed out" in p]
    # 16, not 12: `protective stops` is in the step list twice (deliberately -- it runs again after
    # decisions so a position filled this run gets its stop in the same run), so it reports twice.
    assert len(recorded) == 16, recorded
    assert len(set(recorded)) == 3, "three distinct problems, from three steps that need the broker"
    assert len(timeouts) == 3, f"12 problems should be 3 messages, got {len(timeouts)}: {timeouts}"
    assert len(set(timeouts)) == 3, "and the three must be the three different steps, not one repeated"
    assert all(any("timed out" in p for p in r.problems) for r in reports), \
        "every run must still RECORD the problem -- only the messaging is rationed"


def test_the_digest_reports_what_it_said_only_once(settings, tmp_path):
    """Rationing messages is only honest if the count reaches you. The digest is where it does."""
    from trader.brokers.sim import SimBroker
    from trader.engine.live import DIGEST_HOUR, Engine, send_digest
    from trader.journal.db import Journal

    journal = Journal(tmp_path / "j.db")
    engines = [Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, None,
                      notifier=NullNotifier(), handle_commands=False, shared_outputs=False)]
    notifier = NullNotifier()
    now = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)   # 08:00 Sydney, just past DIGEST_HOUR
    for _ in range(337):
        worth_sending(journal, "alpaca", now.date(), ["protective stops failed: ConnectTimeout: x"])
    worth_sending(journal, "alpaca", now.date(), ["kill switch is on"])      # happened once
    try:
        send_digest(notifier, journal, engines, now)
    finally:
        journal.close()

    assert notifier.sent, f"digest did not fire (DIGEST_HOUR={DIGEST_HOUR}, local hour was 08)"
    body = notifier.sent[0]
    assert "337x protective stops failed: ConnectTimeout" in body, body
    assert "kill switch" not in body, "a problem that happened once was already sent; do not repeat it"


def test_the_journal_records_which_channel_the_run_used(settings, tmp_path):
    """A clean run and a run whose every message vanished used to leave identical journals."""
    from trader.brokers.sim import SimBroker
    from trader.engine.live import Coordinator, Engine, RunReport
    from trader.journal.db import Journal

    journal = Journal(tmp_path / "j.db")
    report = RunReport(started=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc))
    try:
        healthy = NullNotifier()                                   # a test harness: null on purpose
        engine = Engine(settings, SimBroker(cash=100, name="alpaca"), journal, None, notifier=healthy)
        Coordinator(settings, journal, [engine], healthy)._check_alerts_got_through(report)
        assert (journal.kv_get("alert_channel") or "").startswith("NullNotifier ok")

        broken = NullNotifier(reason="TELEGRAM_CHAT_ID not found in /x/.env", misconfigured=True)
        broken.send("vanished")
        engine2 = Engine(settings, SimBroker(cash=100, name="alpaca"), journal, None, notifier=broken)
        Coordinator(settings, journal, [engine2], broken)._check_alerts_got_through(report)
        assert (journal.kv_get("alert_channel") or "").startswith("DISCARDING"), journal.kv_get("alert_channel")
    finally:
        journal.close()
