"""/today and /open, the two Telegram commands VISION.md names that `handle_commands` did not
recognise yet -- everything else it saw fell through to the generic "Commands: ..." reply.

Both reuse data that already exists rather than recomputing it: /today is the dashboard's "today's
plan" card (`intraday_today`), and /open is the per-position detail /status rolls up into one
per-sleeve figure.
"""

from __future__ import annotations

from datetime import date

from trader.brokers.sim import SimBroker
from trader.engine.live import Engine, handle_commands
from trader.journal.db import Journal
from trader.notify.telegram import NullNotifier


class FakeNotifier:
    """Answers one command then falls silent, recording whatever was sent back."""

    def __init__(self, command: str):
        self.command = command
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def poll_commands(self, store):
        command, self.command = self.command, None
        return [command] if command else []


def _fx_journal_with_plan(tmp_path, day: date, symbol: str = "EUR/USD", state: str | None = None,
                          entry: float | None = None) -> Journal:
    """Same shape as `trader.engine.intraday_watch.IntradayWatcher._set` writes, so `intraday_today`
    reads it back exactly as it would the real thing."""
    j = Journal(tmp_path / "j.db")
    key = f"irv:{day:%Y-%m-%d}:{symbol.replace('/', '')}"
    j.kv_set(f"{key}:plan", "1.11894,0.005473,1.11621,1.11456,1.11894,60897.7")
    if state:
        j.kv_set(f"{key}:state", state)
    if entry is not None:
        j.kv_set(f"{key}:entry", str(entry))
    return j


# --- /today -------------------------------------------------------------------------------------


def test_today_reuses_the_dashboards_forex_plan_instead_of_recomputing_it(settings, tmp_path):
    journal = _fx_journal_with_plan(tmp_path, date.today(), state="waiting")
    engine = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/today")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "EUR/USD" in text
    assert "1.11621" in text and "1.11456" in text and "1.11894" in text, "buy, stop and target levels"
    journal.close()


def test_today_says_plainly_when_the_watcher_is_off(settings, tmp_path):
    from trader.config import IntradayConfig

    off = settings.model_copy(update={"intraday": IntradayConfig(enabled=False)})
    journal = Journal(tmp_path / "j.db")
    engine = Engine(off, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/today")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "switched off" in text or "no symbols" in text, "must say so plainly, not fall back to a generic reply"
    assert text != "Commands: /status · /open · /today · /halt · /resume"
    journal.close()


def test_today_says_plainly_when_there_is_no_plan_yet(settings, tmp_path):
    """Enabled, but nothing has been built for today yet -- the common case most of the day."""
    journal = Journal(tmp_path / "j.db")
    engine = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/today")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "EUR/USD" in text
    assert "No plan yet" in text
    journal.close()


# --- /open ----------------------------------------------------------------------------------------


def test_open_lists_symbol_qty_entry_stop_and_unrealized_pnl(settings, tmp_path):
    journal = Journal(tmp_path / "j.db")
    journal.conn.execute(
        "INSERT INTO orders (client_order_id, created_at, updated_at, sleeve_id, symbol, side, "
        "order_type, status, qty) "
        "VALUES ('o1', '2026-09-18T15:00:00+00:00', '2026-09-18T15:00:00+00:00', 'S1', 'SPY', "
        "'buy', 'market', 'filled', 10)")
    journal.conn.execute(
        "INSERT INTO fills (client_order_id, ts, sleeve_id, symbol, side, qty, price, broker_fee_usd, "
        "fee_qty, cost_haircut_usd) VALUES ('o1', '2026-09-18T15:00:00+00:00', 'S1', 'SPY', 'buy', "
        "10, 480.0, 0, 0, 0)")
    journal.conn.commit()
    journal.upsert_position_state("S1", "SPY", entry_price=480.0, entry_date="2026-09-18",
                                  stop=470.0, initial_stop=470.0, high_since_entry=480.0, bars_held=0)
    broker = SimBroker(cash=100_000, name="alpaca")
    broker.set_price("SPY", 500.0)
    engine = Engine(settings, broker, journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/open")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "S1" in text and "SPY" in text
    assert "480.00" in text, "entry price"
    assert "470.00" in text, "current stop"
    assert "+200" in text, "qty 10 * (500 - 480) unrealized"
    journal.close()


def test_open_says_plainly_when_nothing_is_open(settings, tmp_path):
    journal = Journal(tmp_path / "j.db")
    engine = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/open")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    assert notifier.sent[0] == "No open positions."
    journal.close()


# --- /halt ----------------------------------------------------------------------------------------


def test_the_halt_message_says_the_forex_watcher_stops_too(settings, tmp_path):
    """/halt used to leave this ambiguous: nothing in the confirmation said whether the hand-traded
    forex ticket watcher (the one channel where the user is the one placing trades) also goes quiet.
    It does -- `Coordinator._intraday` refuses to run while `kill_switch.active` -- so the message has
    to say so, tying the claim to the code that actually makes it true rather than just asserting it."""
    import inspect

    from trader.engine import live

    journal = Journal(tmp_path / "j.db")
    engine = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/halt")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "forex" in text.lower(), "the confirmation must say what happens to the hand-traded signal"
    assert "no new plans" in text.lower() or "stops too" in text.lower()
    assert engine.kill_switch.active, "the command has to actually engage the switch the message describes"

    src = inspect.getsource(live.Coordinator._intraday)
    assert "kill_switch.active" in src, "the message's claim is only true while this guard exists"
    journal.close()


def test_open_includes_the_hand_traded_forex_position_with_no_broker_behind_it(settings, tmp_path):
    """State "in": bought, not yet closed. Nothing at any broker holds this -- it is executed by hand --
    so it would otherwise be invisible to a command whose whole point is to list what is open."""
    journal = _fx_journal_with_plan(tmp_path, date.today(), state="in", entry=1.11621)
    engine = Engine(settings, SimBroker(cash=100_000, name="alpaca"), journal, {}, notifier=NullNotifier())
    notifier = FakeNotifier("/open")

    handle_commands(notifier, journal, engine.kill_switch, [engine])

    text = notifier.sent[0]
    assert "EUR/USD" in text
    assert "no broker position" in text
    journal.close()
