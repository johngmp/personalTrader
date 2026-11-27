"""The one-time "connected" messages `trader telegram-setup` sends, both chats.

Both messages are the only place a non-developer first learns what the bot can do, so they need to
list the commands the engine actually answers -- `handle_commands` in `trader/engine/live.py` for the
main chat, `handle_fx_commands` for the forex one. A message that falls out of sync with the code is
the kind of gap that stayed invisible precisely because it only runs once, at setup.
"""

from __future__ import annotations

import argparse

from trader.cli import cmd_telegram_setup


class _FakeTelegram:
    """Stands in for `trader.notify.telegram.Telegram` so no real HTTP call is made."""

    sent: list[str] = []

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send(self, text):
        _FakeTelegram.sent.append(text)
        return True


def _fake_find_chat(token, client=None):
    return "my_trader_bot", {"id": 42, "first_name": "Sam"}


def _fake_find_group_chat(token, client=None):
    return "my_trader_bot", {"id": -100, "title": "Forex tickets"}


def test_connect_message_lists_every_main_chat_command(settings, monkeypatch):
    import trader.notify.telegram as telegram_mod

    _FakeTelegram.sent = []
    (settings.root / ".env").write_text("TELEGRAM_BOT_TOKEN=TOKEN123\n")
    monkeypatch.setattr(telegram_mod, "find_chat", _fake_find_chat)
    monkeypatch.setattr(telegram_mod, "Telegram", _FakeTelegram)

    rc = cmd_telegram_setup(settings, argparse.Namespace(fx=False))

    assert rc == 0
    text = _FakeTelegram.sent[0]
    for command in ("/status", "/open", "/today", "/halt", "/resume"):
        assert command in text, f"{command} missing from the connect message"


def test_fx_connect_message_tells_the_user_how_to_reply(settings, monkeypatch):
    import trader.notify.telegram as telegram_mod

    _FakeTelegram.sent = []
    (settings.root / ".env").write_text("TELEGRAM_BOT_TOKEN=TOKEN123\n")
    monkeypatch.setattr(telegram_mod, "find_group_chat", _fake_find_group_chat)
    monkeypatch.setattr(telegram_mod, "Telegram", _FakeTelegram)

    rc = cmd_telegram_setup(settings, argparse.Namespace(fx=True))

    assert rc == 0
    text = _FakeTelegram.sent[0]
    assert "/taken" in text and "/skipped" in text, "the only two replies this chat understands"
