"""Telegram alerts and commands. Without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env it does nothing.

Commands are only accepted from the configured chat, so nobody else can /halt or /resume the engine.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Protocol

import httpx
from dotenv import load_dotenv

from trader.config import PROJECT_ROOT

log = logging.getLogger(__name__)
MAX_LEN = 3900  # Telegram's limit is 4096 characters

# A send gets three attempts, roughly a second apart. The numbers come from what this is for: the
# forex watcher runs on a one-minute beat, so the whole retry sequence has to finish well inside a
# minute or it delays the next pass. Three attempts over ~4 seconds covers the failure this is aimed at
# -- a single timed-out request on a flaky connection -- without turning a dead network into a stall.
SEND_ATTEMPTS = 3
RETRY_WAIT_S = (1.0, 3.0)
MAX_RETRY_AFTER_S = 10.0   # cap on Telegram's own 429 retry_after, so rate limiting cannot hang a pass


class KV(Protocol):
    def kv_get(self, key: str, default: str | None = None) -> str | None: ...
    def kv_set(self, key: str, value: str) -> None: ...


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...
    def poll_commands(self, store: KV) -> list[str]: ...


class NullNotifier:
    """Used when Telegram isn't set up. Messages are kept so tests (and `trader run`) can show them.

    `misconfigured` is the difference between the two reasons to be silent. A dry run or a test wants a
    notifier that discards, and that is fine. An engine that discards because it could not find the
    credentials is a silent failure and must be reported -- that is what happened for weeks. The flag
    defaults to False because `from_env` is the only caller that can produce the accidental kind, and
    it sets the flag itself; everyone else building one by hand is doing it on purpose.
    `Coordinator._check_alerts_got_through` reads it.
    """

    def __init__(self, reason: str = "Telegram is not set up", misconfigured: bool = False) -> None:
        self.sent: list[str] = []
        self.reason = reason
        self.misconfigured = misconfigured

    def send(self, text: str) -> bool:
        """True when discarding is what the caller asked for, False when it is an accident.

        A dry run or a test wants messages to go nowhere and should see success. A notifier that exists
        only because the credentials were missing is a delivery failure, and callers that check -- the
        forex watcher, which rolls its state back on a failed send -- must be told so rather than
        advancing through a trade nobody was told about.
        """
        self.sent.append(text)
        return not self.misconfigured

    def poll_commands(self, store: KV) -> list[str]:
        return []


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.client = client or httpx.Client(timeout=10)
        self.sleep = sleep or time.sleep   # injectable so the retry tests do not actually wait
        self.failures = 0                   # consecutive sends that did not get through
        self.last_error: str | None = None
        self.attempts_used = 0              # attempts the last failed send spent, for the tests

    def send(self, text: str) -> bool:
        """True if Telegram confirmed delivery. Never raises: an alert must not stop the engine trading.

        But it must not vanish either. This logged to a Python logger nobody reads, so a wrong chat id,
        a revoked token or a blocked bot would have made every message disappear while the engine
        reported a clean run -- and for a system whose whole job is to tell you things, a silent delivery
        failure is indistinguishable from a quiet market. You would be waiting for a signal that had
        already been sent into nothing. The count is read after each run and recorded where you can see
        it; see Coordinator._check_alerts_got_through.

        It now retries, and returns whether it worked, because of what sits downstream. The forex
        watcher claims the day's state BEFORE it sends -- it has to, or two processes would message you
        the same entry -- so a send that failed silently left the state saying you were in a trade you
        had never been told to open, and the next message you would get was a SELL for a position you
        did not hold. One timed-out request is the likely cause and is exactly what a retry fixes; the
        return value is what lets the watcher put the state back when it does not.

        Retried: timeouts, dropped connections, Telegram's 5xx, and 429 rate limiting. Not retried: a
        4xx, which means the token or the chat id is wrong and trying again cannot help -- it would
        just spend the minute the watcher is running in.
        """
        tried, failure = 0, None
        while tried < SEND_ATTEMPTS:
            delivered, retry_after, exc = self._attempt(text)
            tried += 1
            if delivered:
                self.failures = 0
                self.last_error = None
                return True
            failure = exc
            if retry_after is None or tried == SEND_ATTEMPTS:
                break                       # nothing a retry would fix, or out of attempts
            self.sleep(retry_after)
        self.failures += 1
        self.last_error = f"{type(failure).__name__}: {failure}" if failure else "unknown"
        self.attempts_used = tried
        log.warning("telegram send failed after %d attempt(s): %s", tried, self.last_error)
        return False

    def _attempt(self, text: str) -> tuple[bool, float | None, Exception | None]:
        """(delivered, seconds to wait before retrying or None if retrying is pointless, what failed)."""
        try:
            r = self.client.post(f"{self.base}/sendMessage",
                                 json={"chat_id": self.chat_id, "text": text[:MAX_LEN]})
        except httpx.RequestError as exc:                      # timeout, DNS, refused, reset
            return False, RETRY_WAIT_S[0], exc
        if r.status_code == 429:
            try:
                wait = float(r.json().get("parameters", {}).get("retry_after", RETRY_WAIT_S[0]))
            except ValueError:
                wait = RETRY_WAIT_S[0]
            return False, min(wait, MAX_RETRY_AFTER_S), httpx.HTTPError("429 rate limited")
        if r.status_code >= 500:
            return False, RETRY_WAIT_S[1], httpx.HTTPError(f"{r.status_code} from telegram")
        try:
            r.raise_for_status()
            # Telegram can answer 200 with ok:false, so the status alone is not proof of delivery.
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return False, None, exc                            # a 4xx or unreadable body: retrying cannot help
        if not body.get("ok"):
            return False, None, httpx.HTTPError(str(body.get("description") or "telegram returned ok:false"))
        return True, None, None

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
    """The notifier the credentials in .env describe -- loading .env here, not trusting a caller to.

    This function used to read os.environ and nothing more, which made it correct only for a caller
    that had already loaded .env. `trader run` had not: it called this on line one and `load_secrets()`
    (the only thing that loads .env) several lines later, inside the broker setup. Under launchd the
    environment is bare, so every engine run for weeks built a NullNotifier and then "sent" thousands
    of fills, stops and warnings into a list that was thrown away at the end of the run. No failure was
    logged, because from the engine's point of view nothing failed. `trader watch-fx` -- the forex
    messages you asked for -- had the same defect. The seventeen messages that did arrive came from
    `trader notify`, the one path that happened to load .env on the line before.

    Loading it here means the order cannot be got wrong again, by this caller or the next one.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return Telegram(token, chat_id)
    missing = " / ".join(n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)) if not v)
    return NullNotifier(reason=f"{missing} not found in {PROJECT_ROOT / '.env'}", misconfigured=True)


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
