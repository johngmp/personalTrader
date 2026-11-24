"""Forex price precision and the alerts you act on by hand.

Rounding an FX price to 2 decimals is not cosmetic: EUR/USD 0.69823 -> 0.70 moves the stop 18 pips and
silently changes the risk the gate approved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.engine.live import HAND_EXECUTED, _fill_text, _stop_move_text
from trader.models import AssetClass, Fill, Side, format_price, price_decimals, round_price


@pytest.mark.parametrize(("price", "asset_class", "decimals"), [
    (0.71250, AssetClass.FOREX, 5),      # AUD/USD -- a major
    (1.14890, AssetClass.FOREX, 5),      # EUR/USD
    (156.874, AssetClass.FOREX, 3),      # USD/JPY -- hundreds, three places
    (4412.30, AssetClass.FOREX, 2),      # XAU/USD -- gold, thousands, cents
    (761.35, AssetClass.US_EQUITY, 2),
    (77454.09, AssetClass.CRYPTO, 2),
])
def test_each_market_is_quoted_to_its_own_precision(price, asset_class, decimals):
    assert price_decimals(price, asset_class) == decimals


def test_an_fx_stop_no_longer_collapses_to_two_decimals():
    """The bug: round(0.69823, 2) == 0.7, an 18 pip move in the stop, changing the approved risk."""
    assert round_price(0.69823, AssetClass.FOREX) == 0.69823
    assert round(0.69823, 2) == 0.7        # what the code used to do
    assert round_price(0.69823, AssetClass.FOREX) != round(0.69823, 2)


def test_stock_and_crypto_prices_still_round_to_cents():
    assert round_price(761.3549, AssetClass.US_EQUITY) == 761.35
    assert round_price(77454.0912, AssetClass.CRYPTO) == 77454.09


def test_gold_keeps_cents_rather_than_five_decimals():
    assert round_price(4412.2998, AssetClass.FOREX) == 4412.3


def test_formatting_matches_the_rounding():
    assert format_price(0.7125, AssetClass.FOREX) == "0.71250"
    assert format_price(4412.2998, AssetClass.FOREX) == "4,412.30"
    assert format_price(761.35, AssetClass.US_EQUITY) == "761.35"


# --- the messages you act on ----------------------------------------------------------------------

def a_fill(symbol: str, price: float, side: Side = Side.BUY) -> Fill:
    return Fill(client_order_id="c1", sleeve_id="S9", symbol=symbol, side=side, qty=3440, price=price,
                cost_haircut_usd=0.0, ts=datetime.now(timezone.utc))


def test_an_fx_alert_shows_the_full_rate_and_no_dollar_sign():
    """'@ $1.15' is both wrong and unusable: a rate is not a dollar amount and 2dp loses the trade."""
    text = _fill_text(a_fill("AUD/USD", 0.71250), asset_class=AssetClass.FOREX)
    assert "0.71250" in text and "$" not in text


def test_a_stock_alert_still_shows_dollars():
    assert "$761.35" in _fill_text(a_fill("SPY", 761.35), asset_class=AssetClass.US_EQUITY)


def test_a_stop_move_alert_names_both_prices_and_asks_you_to_act():
    text = _stop_move_text("S9", "AUD/USD", 0.69800, 0.70410, AssetClass.FOREX)
    assert "0.69800" in text and "0.70410" in text
    assert "by hand" in text


def test_the_first_stop_reads_sensibly_when_there_was_none_before():
    assert "none" in _stop_move_text("S9", "EUR/USD", None, 1.13500, AssetClass.FOREX)


# --- who gets messaged ----------------------------------------------------------------------------

@pytest.mark.parametrize(("broker", "notifies"), [
    ("alpaca", True),        # real paper account
    ("shadow", False),       # research only: stays off your phone
    ("shadow_fx", False),    # S9 failed Gate 1; it records evidence instead of interrupting you
])
def test_only_brokers_you_act_on_reach_your_phone(broker, notifies):
    from trader.engine.live import Engine

    fake = type("B", (), {"name": broker})()
    assert Engine._should_notify(type("E", (), {"broker": fake})()) is notifies


def test_the_hand_executed_set_is_all_shadow_brokers():
    """A hand-executed broker that wasn't a shadow one would mean real orders going out unsupervised."""
    assert all(name.startswith("shadow") for name in HAND_EXECUTED)


# --- a message that never arrives ------------------------------------------------------------------

def test_a_failed_telegram_send_is_recorded_not_swallowed():
    """`send` must never raise -- an alert failing cannot be allowed to stop the engine trading -- but
    it must not vanish either. It logged to a Python logger nobody reads, so a wrong chat id, a revoked
    token or a blocked bot would make every message disappear while the run reported itself clean.

    For a system whose whole job is to tell you things, that is indistinguishable from a quiet market:
    you would sit waiting for a signal that had already been sent into nothing.
    """
    import httpx

    from trader.notify.telegram import Telegram

    def dead(_request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(dead)))
    bot.send("hello")                                   # must not raise
    assert bot.failures == 1
    assert "Unauthorized" in (bot.last_error or "") or "401" in (bot.last_error or "")


def test_a_two_hundred_with_ok_false_still_counts_as_a_failure():
    """Telegram can answer 200 with ok:false, so the status code alone is not proof of delivery."""
    import httpx

    from trader.notify.telegram import Telegram

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"ok": False, "description": "chat not found"}))))
    bot.send("hello")
    assert bot.failures == 1 and "chat not found" in (bot.last_error or "")


def test_a_successful_send_clears_the_failure_count():
    """Otherwise one network blip would report a broken notifier for the rest of the day.

    The first send has to exhaust every attempt to count as a failure now that sends retry, so the
    transport fails SEND_ATTEMPTS times and then recovers. That is also the honest shape of the thing
    being tested: a single 500 is no longer a failure at all.
    """
    import httpx

    from trader.notify.telegram import SEND_ATTEMPTS, Telegram

    ok = {"n": 0}

    def flaky(_request):
        ok["n"] += 1
        return httpx.Response(500) if ok["n"] <= SEND_ATTEMPTS else httpx.Response(200, json={"ok": True})

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(flaky)),
                   sleep=lambda _s: None)
    assert bot.send("one") is False
    assert bot.failures == 1
    assert bot.send("two") is True
    assert bot.failures == 0 and bot.last_error is None


def test_the_run_reports_alerts_that_did_not_arrive(settings, tmp_path):
    """The count is useless unless something reads it."""
    from trader.brokers.sim import SimBroker
    from trader.engine.live import Coordinator, Engine, RunReport
    from trader.journal.db import Journal

    class Broken:
        failures = 3
        last_error = "HTTPStatusError: 401 Unauthorized"

        def send(self, text): return False
        def poll_commands(self, store): return []

    journal = Journal(tmp_path / "j.db")
    engines = [Engine(settings, SimBroker(cash=1_000, name="alpaca"), journal, {}, notifier=Broken())]
    report = RunReport(started=datetime(2026, 10, 8, tzinfo=timezone.utc))
    Coordinator(settings, journal, engines, Broken())._check_alerts_got_through(report)

    assert any("did not reach Telegram" in p for p in report.problems)
    assert any("401" in p for p in report.problems)
    assert any("telegram delivery failing" in (r["message"] or "") for r in journal.recent_events(5))
    journal.close()


def test_every_run_actually_checks_that_the_alerts_arrived():
    """The wiring, not the method.

    The test above calls _check_alerts_got_through directly and passes with the call deleted from
    run_once -- the third time today a test of a helper said nothing about whether anything calls it,
    after the forex clock the executor never handed the gate and the risk fit _enter never applied.
    """
    import inspect

    from trader.engine.live import Coordinator

    assert "_check_alerts_got_through" in inspect.getsource(Coordinator.run_once)


# --- a timed-out BUY used to be lost silently, and the state said you were in the trade ---------------
#
# Every transition claims the day's state BEFORE it sends, and has to, or two processes would message
# the same entry. The cost was that one timed-out request left the state at "in" with nobody told to
# buy -- and the next message would be a SELL for a position that was never opened, quoting a profit on
# money that was never at risk. Two halves to the fix: the send retries, and a send that still fails
# puts the state back.

def _flaky_bot(responses, **kw):
    import httpx

    from trader.notify.telegram import Telegram

    seq = list(responses)
    calls = {"n": 0}

    def transport(_request):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(transport)),
                   sleep=lambda _s: None, **kw)
    return bot, calls


def test_one_timed_out_request_is_retried_and_the_message_gets_through():
    """The failure this was built for. A single timeout is the common case on a flaky connection, and
    before this it silently lost whatever the message was."""
    import httpx

    from trader.notify.telegram import Telegram

    calls = {"n": 0}

    def transport(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={"ok": True})

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(transport)),
                   sleep=lambda _s: None)
    assert bot.send("BUY EUR/USD") is True
    assert calls["n"] == 2 and bot.failures == 0


def test_retries_stop_after_the_configured_number_of_attempts():
    """Unbounded retries would be worse than none: this runs on a one-minute beat, so a dead network
    has to be given up on rather than allowed to stall the next pass."""
    import httpx

    from trader.notify.telegram import SEND_ATTEMPTS, Telegram

    bot, calls = _flaky_bot([httpx.Response(503)])
    assert bot.send("x") is False
    assert calls["n"] == SEND_ATTEMPTS == 3
    assert bot.attempts_used == SEND_ATTEMPTS


def test_a_wrong_token_or_chat_id_is_not_retried():
    """A 4xx means the credentials are wrong and trying again cannot help. Retrying would spend the
    minute this command runs in, three times over, to reach the same answer."""
    import httpx

    bot, calls = _flaky_bot([httpx.Response(401, json={"description": "Unauthorized"})])
    assert bot.send("x") is False
    assert calls["n"] == 1, "a 401 must be given up on immediately"


def test_rate_limiting_waits_as_long_as_telegram_asks_within_reason():
    import httpx

    from trader.notify.telegram import MAX_RETRY_AFTER_S, Telegram

    waits: list[float] = []
    calls = {"n": 0}

    def transport(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 999}})
        return httpx.Response(200, json={"ok": True})

    bot = Telegram("token", "chat", client=httpx.Client(transport=httpx.MockTransport(transport)),
                   sleep=waits.append)
    assert bot.send("x") is True
    assert waits == [MAX_RETRY_AFTER_S], "an absurd retry_after must be capped, not obeyed"


def test_ok_false_is_still_a_failure_and_is_not_retried():
    """Telegram answers 200 with ok:false for a chat that does not exist. The status is not delivery."""
    import httpx

    bot, calls = _flaky_bot([httpx.Response(200, json={"ok": False, "description": "chat not found"})])
    assert bot.send("x") is False
    assert calls["n"] == 1 and "chat not found" in (bot.last_error or "")


def test_a_misconfigured_null_notifier_reports_failure_but_a_dry_run_does_not():
    """The two reasons to be silent are not the same. A dry run wants to discard; a notifier that
    exists only because the credentials were missing is a delivery failure, and the watcher must be
    told or it advances through a trade nobody heard about."""
    from trader.notify.telegram import NullNotifier

    assert NullNotifier().send("x") is True
    assert NullNotifier(reason="no token", misconfigured=True).send("x") is False


# --- the command's own delivery check ----------------------------------------------------------------
#
# `trader watch-fx` built a notifier, sent into it and exited 0 regardless. launchd runs it every
# minute and had no way to know a message had gone nowhere.

def _check(notifier, tmp_path):
    from trader.cli import _fx_delivery_check
    from trader.journal.db import Journal

    journal = Journal(tmp_path / "j.db")
    try:
        code = _fx_delivery_check(journal, notifier)
        errors = [r["message"] for r in journal.conn.execute(
            "SELECT message FROM events WHERE level='error'").fetchall()]
        return code, journal.kv_get("alert_channel"), errors
    finally:
        journal.close()


def test_a_working_notifier_records_that_it_is_working(tmp_path):
    """The healthy case is written down too, so "the alerts are working" is something you can check
    rather than assume. A clean pass and a pass whose every message vanished used to look identical."""
    from trader.notify.telegram import NullNotifier

    code, channel, errors = _check(NullNotifier(), tmp_path)
    assert code == 0
    assert channel and channel.startswith("NullNotifier ok")
    assert not errors


def test_missing_credentials_exit_non_zero_instead_of_discarding_quietly(tmp_path):
    """This is what happened for weeks: thousands of messages appended to a list and dropped."""
    from trader.notify.telegram import NullNotifier

    notifier = NullNotifier(reason="TELEGRAM_BOT_TOKEN not found in .env", misconfigured=True)
    notifier.send("BUY EUR/USD")
    code, channel, errors = _check(notifier, tmp_path)
    assert code == 1, "launchd has to be able to see this"
    assert channel.startswith("DISCARDING")
    assert any("1 message(s) discarded" in m for m in errors), errors


def test_a_send_that_failed_exits_non_zero_and_names_the_error(tmp_path):
    class Failing:
        failures = 1
        last_error = "ConnectTimeout: timed out"

        def send(self, text): return False
        def poll_commands(self, store): return []

    code, channel, errors = _check(Failing(), tmp_path)
    assert code == 1
    assert channel == "FAILING ConnectTimeout: timed out"
    assert any("did not reach Telegram" in m and "timed out" in m for m in errors), errors


# --- forex tickets get their own chat ----------------------------------------------------------------
#
# These are the only messages the user acts on by hand -- a ticket to type into MetaTrader 4 on a phone
# -- and they arrived in the same conversation as every paper fill, stop and warning the engine sends.
# On a busy day the one message that needs doing is one of forty that do not.

@pytest.fixture
def no_real_dotenv(monkeypatch, tmp_path):
    """Point the notifier at an empty project, so these tests cannot read the user's real .env.

    Without it, `fx_from_env` calls `load_dotenv(PROJECT_ROOT / ".env")`, which fills in anything
    `monkeypatch.delenv` just removed -- `load_dotenv` does not override what is already set, so a
    deleted key comes straight back from the real file. The fallback test below therefore passed only
    while the user had no forex chat configured, and started failing the day they set one up, asserting
    "111" against their actual group id. A test whose verdict depends on the developer's credentials
    file is not testing the code.

    The same pattern as `test_from_env_says_which_credential_is_missing` in test_alerts.py. Deliberately
    a fixture rather than autouse in conftest: one test in that file asserts that `from_env` reads the
    real .env on its own, which is the bug it was written for, and isolating it would gut it.
    """
    import trader.notify.telegram as tg

    monkeypatch.setattr(tg, "PROJECT_ROOT", tmp_path)
    assert not (tmp_path / ".env").exists()


def test_forex_messages_go_to_the_forex_chat_when_one_is_configured(monkeypatch, no_real_dotenv):
    from trader.notify.telegram import Telegram, fx_from_env

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_FX_CHAT_ID", "222")
    notifier = fx_from_env()
    assert isinstance(notifier, Telegram) and notifier.chat_id == "222"


def test_forex_messages_fall_back_to_the_main_chat_rather_than_going_silent(monkeypatch, no_real_dotenv):
    """A half-finished setup must not stop the tickets arriving. The whole point of this path is that
    it keeps working -- going quiet because an optional key is missing would be the worst outcome of
    a change meant to make the messages easier to see."""
    from trader.notify.telegram import Telegram, fx_from_env

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.delenv("TELEGRAM_FX_CHAT_ID", raising=False)
    notifier = fx_from_env()
    assert isinstance(notifier, Telegram) and notifier.chat_id == "111"


def test_the_engines_own_pass_uses_the_forex_chat_too():
    """Half a split is worse than none: the same ticket would arrive in a different place depending on
    whether the minute agent or the engine's fifteen-minute pass happened to catch the level."""
    import inspect

    from trader.engine.live import Coordinator

    src = inspect.getsource(Coordinator._intraday)
    assert "self.fx_notifier" in src, "_intraday must hand the watcher the forex notifier"
    assert "self.journal, self.notifier" not in src, "not the engine's main notifier"


def test_the_coordinator_defaults_the_forex_chat_to_the_main_one(settings, tmp_path):
    """Every existing caller and test built a Coordinator with one notifier. They must behave exactly
    as before, or this change would quietly redirect messages in code nobody updated."""
    from trader.brokers.sim import SimBroker
    from trader.engine.live import Coordinator, Engine
    from trader.journal.db import Journal
    from trader.notify.telegram import NullNotifier

    journal = Journal(tmp_path / "j.db")
    try:
        only = NullNotifier()
        engines = [Engine(settings, SimBroker(cash=1_000, name="alpaca"), journal, {}, notifier=only)]
        assert Coordinator(settings, journal, engines, only).fx_notifier is only
    finally:
        journal.close()


def test_a_dry_run_does_not_message_the_forex_chat():
    """`--dry-run` means send nothing. Reaching for the real forex chat there would post a ticket from
    a run explicitly not meant to send anything -- and unlike the engine's own alerts, a forex ticket
    is an instruction to go and place a trade."""
    import inspect

    from trader import cli

    src = inspect.getsource(cli.cmd_run)
    assert "notifier if args.dry_run else fx_from_env()" in src


def test_the_group_finder_sees_groups_and_ignores_the_private_chat():
    """`find_chat` deliberately only looks at private chats, so the forex split needs its own finder.
    It reads `my_chat_member` as well as messages: that is the update Telegram sends when the bot is
    ADDED to a group, and it arrives even with privacy mode on. Relying on an ordinary group message
    would fail silently for any bot left on the default privacy setting, which is most of them.
    """
    import httpx

    from trader.notify.telegram import find_group_chat

    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 111, "type": "private", "first_name": "Jo"}}},
        {"my_chat_member": {"chat": {"id": -500, "type": "group", "title": "Forex tickets"}}},
    ]}

    def transport(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}})
        return httpx.Response(200, json=updates)

    username, chat = find_group_chat("tok", client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert username == "mybot"
    assert chat["id"] == -500 and chat["title"] == "Forex tickets"


def test_the_group_finder_returns_nothing_when_only_a_private_chat_exists():
    """So setup can print the three steps instead of silently wiring the forex chat to the main one."""
    import httpx

    from trader.notify.telegram import find_group_chat

    def transport(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}})
        return httpx.Response(200, json={"ok": True, "result": [
            {"message": {"chat": {"id": 111, "type": "private"}}}]})

    _, chat = find_group_chat("tok", client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert chat is None


# --- the setup commands must not answer a network blip with a traceback ------------------------------
#
# `send` was given retries this morning because one timeout to api.telegram.org was losing a forex
# ticket. The setup commands were left making bare calls, and the first blip after that printed sixty
# lines of httpx traceback -- to someone whose next step was creating a Telegram group on a phone.

def _flaky_client(responses, calls=None):
    import httpx

    seq, n = list(responses), calls if calls is not None else {"n": 0}

    def transport(request):
        n["n"] += 1
        item = seq[min(n["n"] - 1, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}})
        return item

    return httpx.Client(transport=httpx.MockTransport(transport)), n


def test_one_timed_out_setup_request_is_retried_rather_than_crashing(monkeypatch):
    import httpx

    from trader.notify import telegram
    from trader.notify.telegram import find_group_chat

    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    client, calls = _flaky_client([
        httpx.ConnectTimeout("handshake timed out"),
        httpx.Response(200, json={"ok": True, "result": [
            {"my_chat_member": {"chat": {"id": -500, "type": "group", "title": "Forex tickets"}}}]}),
    ])
    username, chat = find_group_chat("tok", client=client)
    assert username == "mybot" and chat["id"] == -500
    assert calls["n"] > 1, "it must actually have retried"


def test_a_network_failure_is_a_sentence_not_a_traceback(monkeypatch):
    """What the user saw: `httpx.ConnectTimeout: _ssl.c:1015: The handshake operation timed out` under
    forty lines of frames. `TelegramUnreachable` subclasses ValueError because both setup commands
    already print one and exit 1, so the readable path was there and simply unused."""
    import httpx
    import pytest

    from trader.notify import telegram
    from trader.notify.telegram import TelegramUnreachable, find_group_chat

    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    client, _ = _flaky_client([httpx.ConnectTimeout("handshake timed out")])
    with pytest.raises(TelegramUnreachable, match="Nothing was changed"):
        find_group_chat("tok", client=client)
    assert issubclass(TelegramUnreachable, ValueError), "the CLI's except ValueError must catch it"


def test_a_bad_token_is_still_reported_immediately_and_not_retried(monkeypatch):
    """A 401 means the token is wrong. Three attempts cannot fix that and the message must stay the
    one that tells you where to get a new one."""
    import httpx
    import pytest

    from trader.notify import telegram
    from trader.notify.telegram import find_chat

    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def transport(_request):
        calls["n"] += 1
        return httpx.Response(401, json={"description": "Unauthorized"})

    with pytest.raises(ValueError, match="@BotFather"):
        find_chat("tok", client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert calls["n"] == 1, "a 401 must not be retried"


# --- the engine must not eat the update the forex setup needs ----------------------------------------
#
# `getUpdates` with an offset CONFIRMS everything before it and Telegram then deletes those updates for
# good. The engine polls every fifteen minutes, so the `my_chat_member` update saying "the bot was added
# to a group" is consumed and discarded by a caller with no interest in it. Whoever made the group would
# be told "I can't see a group yet" however correctly they followed the steps, and the only cure would
# be luck about which ran first.

class _Store(dict):
    def kv_get(self, key, default=None):
        return self.get(key, default)

    def kv_set(self, key, value):
        self[key] = value


def _poll_once(updates):
    import httpx

    from trader.notify.telegram import Telegram

    bot = Telegram("tok", "111", client=httpx.Client(transport=httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"ok": True, "result": updates}))))
    store = _Store()
    commands = bot.poll_commands(store)
    return commands, store


def test_polling_for_commands_writes_down_any_group_it_is_shown():
    """The whole fix: the consumer records what it is about to destroy."""
    import json

    from trader.notify.telegram import LAST_GROUP_KEY

    _, store = _poll_once([
        {"update_id": 1,
         "my_chat_member": {"chat": {"id": -500, "type": "supergroup", "title": "Forex tickets"}}},
    ])
    noted = json.loads(store[LAST_GROUP_KEY])
    assert noted["id"] == -500 and noted["title"] == "Forex tickets"


def test_a_remembered_group_is_still_there_after_the_queue_is_gone():
    """End to end on the real failure: the engine polls first, Telegram then returns nothing, and the
    setup must still be able to answer. Without the record it would say "I can't see a group yet"
    forever."""
    import json

    import httpx

    from trader.notify.telegram import LAST_GROUP_KEY, find_group_chat

    _, store = _poll_once([
        {"update_id": 7,
         "my_chat_member": {"chat": {"id": -900, "type": "group", "title": "FX"}}},
    ])

    # Telegram now has nothing left to give: the poll above confirmed the queue.
    def empty(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}})
        return httpx.Response(200, json={"ok": True, "result": []})

    _, live = find_group_chat("tok", client=httpx.Client(transport=httpx.MockTransport(empty)))
    assert live is None, "the live look must genuinely come up empty, or this proves nothing"
    assert json.loads(store[LAST_GROUP_KEY])["id"] == -900


def test_being_remembered_does_not_let_a_group_halt_the_engine():
    """The group is recorded, not trusted. Commands stay restricted to the configured chat, or anyone
    who found the bot could add it to a group and send /halt."""
    commands, store = _poll_once([
        {"update_id": 1, "message": {"chat": {"id": -500, "type": "group", "title": "FX"},
                                     "text": "/halt"}},
    ])
    assert commands == [], "a command from a group must be ignored"
    from trader.notify.telegram import LAST_GROUP_KEY
    assert LAST_GROUP_KEY in store, "but the group is still noted"


def test_a_command_from_the_real_chat_still_works():
    """The guard on the guard: if this broke, /halt would stop working and nothing above would notice."""
    commands, _ = _poll_once([
        {"update_id": 1, "message": {"chat": {"id": 111, "type": "private"}, "text": "/halt"}},
    ])
    assert commands == ["/halt"]


def test_the_main_private_chat_is_never_recorded_as_the_forex_group():
    """The failure this guards is silent and total: if private chats were eligible, your own chat with
    the bot would be written down as 'the group', `--fx` would set TELEGRAM_FX_CHAT_ID to the chat it
    was meant to move messages OUT of, and the split would read as configured while changing nothing.

    Ordered with the private message LAST, which is the realistic sequence -- message the bot, add it
    to a group, message it again -- and the one where a too-loose filter overwrites the right answer.
    """
    import json

    from trader.notify.telegram import LAST_GROUP_KEY

    _, store = _poll_once([
        {"update_id": 1, "my_chat_member": {"chat": {"id": -500, "type": "group", "title": "FX"}}},
        {"update_id": 2, "message": {"chat": {"id": 111, "type": "private"}, "text": "hi"}},
    ])
    assert json.loads(store[LAST_GROUP_KEY])["id"] == -500, "the group must survive a later private message"


def test_nothing_is_recorded_when_only_a_private_chat_is_seen():
    """So setup prints the three steps rather than wiring forex to the main chat."""
    from trader.notify.telegram import LAST_GROUP_KEY

    _, store = _poll_once([
        {"update_id": 1, "message": {"chat": {"id": 111, "type": "private"}, "text": "/status"}},
    ])
    assert LAST_GROUP_KEY not in store
