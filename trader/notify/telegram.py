"""Telegram alerts and commands. Without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env it does nothing.

Commands are only accepted from the configured chat, so nobody else can /halt or /resume the engine.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 3900  # Telegram's limit is 4096 characters


class KV(Protocol):
    def kv_get(self, key: str, default: str | None = None) -> str | None: ...
    def kv_set(self, key: str, value: str) -> None: ...


class Notifier(Protocol):
    def send(self, text: str) -> None: ...
    def poll_commands(self, store: KV) -> list[str]: ...


class NullNotifier:
    """Used when Telegram isn't set up. Messages are kept so tests (and `trader run`) can show them."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def poll_commands(self, store: KV) -> list[str]:
        return []


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.client = client or httpx.Client(timeout=10)

    def send(self, text: str) -> None:
        try:
            r = self.client.post(f"{self.base}/sendMessage", json={"chat_id": self.chat_id, "text": text[:MAX_LEN]})
            r.raise_for_status()
        except httpx.HTTPError as exc:  # never let an alert failure break trading
            log.warning("telegram send failed: %s", exc)

    def poll_commands(self, store: KV) -> list[str]:
        offset = int(store.kv_get("telegram_offset", "0") or 0)
        try:
            r = self.client.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": 0})
            r.raise_for_status()
            updates = r.json().get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram poll failed: %s", exc)
            return []
        commands = []
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or {}
            if str(message.get("chat", {}).get("id")) != self.chat_id:
                continue  # ignore anyone else who finds the bot
            text = (message.get("text") or "").strip().split()
            if text and text[0].startswith("/"):
                commands.append(text[0].split("@")[0].lower())
        store.kv_set("telegram_offset", str(offset))
        return commands


def from_env() -> Notifier:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return Telegram(token, chat_id) if token and chat_id else NullNotifier()


def find_chat(token: str, client: httpx.Client | None = None) -> tuple[str, dict | None]:
    """(bot username, the most recent private chat that messaged the bot, if any)."""
    client = client or httpx.Client(timeout=10)
    base = f"https://api.telegram.org/bot{token}"
    me = client.get(f"{base}/getMe")
    if me.status_code == 401:
        raise ValueError("Telegram rejected the bot token. Copy it again from @BotFather.")
    me.raise_for_status()
    username = me.json()["result"]["username"]
    updates = client.get(f"{base}/getUpdates", params={"timeout": 0}).json().get("result", [])
    chats = [u["message"]["chat"] for u in updates if u.get("message", {}).get("chat", {}).get("type") == "private"]
    return username, (chats[-1] if chats else None)


def set_env_value(env_path, key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or adding one."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
