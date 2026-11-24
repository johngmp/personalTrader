"""Telegram alerts and commands. Without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env it does nothing.

Commands are only accepted from the configured chat, so nobody else can /halt or /resume the engine.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Protocol

import httpx
from dotenv import load_dotenv

from pathlib import Path

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

# Where `poll_commands` records a group it was shown, for `telegram-setup --fx` to read afterwards.
# See `Telegram._remember_group` for why this cannot be re-read from Telegram instead.
LAST_GROUP_KEY = "telegram_last_group"


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

    def poll_commands_full(self, store: KV) -> list[tuple[str, str]]:
        return []


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] | None = None,
                 offset_key: str = "telegram_offset") -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.client = client or httpx.Client(timeout=10)
        self.sleep = sleep or time.sleep   # injectable so the retry tests do not actually wait
        self.failures = 0                   # consecutive sends that did not get through
        self.last_error: str | None = None
        self.attempts_used = 0              # attempts the last failed send spent, for the tests
        # A second Telegram instance polling a different chat on the SAME bot token -- the forex
        # split -- must track its own `getUpdates` offset under its own key. `getUpdates` confirms
        # (and lets Telegram discard) everything before the offset you send, for every caller sharing
        # the token, not just the one chat you are filtering for. Two instances reading and writing
        # the one "telegram_offset" row in the same run would have the second one's write silently
        # undo what the first just saw, and whichever polled second would come up empty even though
        # its chat's message was sitting right there in the response the first call got.
        self.offset_key = offset_key

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

    def _poll(self, store: KV) -> list[tuple[str, str, str]]:
        """(chat id, command word, rest of the text) for every command-shaped message queued for this
        bot, from one `getUpdates` call. Shared by `poll_commands` and `poll_commands_full` so there is
        exactly one place that does the offset bookkeeping -- see `offset_key` on why that matters.
        """
        offset = int(store.kv_get(self.offset_key, "0") or 0)
        try:
            r = self.client.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": 0})
            r.raise_for_status()
            updates = r.json().get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram poll failed: %s", exc)
            return []
        out: list[tuple[str, str, str]] = []
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            self._remember_group(update, store)
            message = update.get("message") or {}
            chat_id = str(message.get("chat", {}).get("id"))
            parts = (message.get("text") or "").strip().split(maxsplit=1)
            if parts and parts[0].startswith("/"):
                out.append((chat_id, parts[0].split("@")[0].lower(), parts[1] if len(parts) > 1 else ""))
        store.kv_set(self.offset_key, str(offset))
        return out

    def poll_commands(self, store: KV) -> list[str]:
        return [cmd for chat_id, cmd, _ in self._poll(store)
                if chat_id == self.chat_id]  # ignore anyone else who finds the bot

    def poll_commands_full(self, store: KV) -> list[tuple[str, str]]:
        """Like `poll_commands`, but keeps the text after the command word -- `/taken 1.08421` needs
        the price, which the bare command list throws away."""
        return [(cmd, args) for chat_id, cmd, args in self._poll(store)
                if chat_id == self.chat_id]

    @staticmethod
    def _remember_group(update: dict, store: KV) -> None:
        """Write down any group this bot is shown, because this poll is about to destroy the evidence.

        `getUpdates` with an offset CONFIRMS everything before it, and Telegram then deletes those
        updates for good. The engine polls every fifteen minutes, so the `my_chat_member` update that
        says "the bot was added to a group" -- the one `telegram-setup --fx` needs -- gets consumed and
        discarded by a caller that has no interest in it. Whoever created the group would then be told
        "I can't see a group yet" no matter how correctly they had followed the instructions, and the
        only cure would be luck about which ran first.

        So the consumer records it. Whichever path sees the group first, the id survives: cooperation
        instead of a race over the same one-shot queue. Commands are still only accepted from the main
        chat -- being remembered here does not grant a group the right to halt the engine.
        """
        for chat in ((update.get("message") or {}).get("chat"),
                     (update.get("channel_post") or {}).get("chat"),
                     (update.get("my_chat_member") or {}).get("chat")):
            if chat and chat.get("type") in {"group", "supergroup", "channel"}:
                store.kv_set(LAST_GROUP_KEY, json.dumps(
                    {"id": chat["id"], "title": chat.get("title"), "type": chat["type"]}))


def _notifier_for(token: str, chat_id: str, missing: str, env_path: Path) -> Notifier:
    if token and chat_id:
        return Telegram(token, chat_id)
    return NullNotifier(reason=f"{missing} not found in {env_path}", misconfigured=True)


def fx_from_env() -> Notifier:
    """The notifier the FOREX messages go to: its own chat when one is configured, else the main one.

    These are the only messages the user is expected to act on by hand -- a ticket to type into
    MetaTrader on a phone -- and they were arriving in the same conversation as every paper fill, stop
    and warning the engine produces. On a busy day the one message that needs doing is buried in forty
    that do not, which is a good way to miss it.

    Falls back to the main chat rather than going silent, because a half-finished setup must not stop
    the tickets arriving; the whole point of this path is that it keeps working. The fallback is logged
    so it is visible rather than assumed -- `trader telegram-setup --fx` is what sets the split up.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    fx_chat = os.environ.get("TELEGRAM_FX_CHAT_ID", "").strip()
    main_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and fx_chat:
        return Telegram(token, fx_chat, offset_key="telegram_fx_offset")
    if token and main_chat:
        log.info("TELEGRAM_FX_CHAT_ID not set; forex messages go to the main chat. "
                 "Run `trader telegram-setup --fx` to give them their own.")
        # Still its OWN offset key even though it is the main chat: `/taken` and `/skipped` are read
        # by a separate poll from the main commands (see live.handle_fx_commands), and that poll must
        # not share a `getUpdates` offset with the main one or it comes up empty -- see `offset_key`.
        return Telegram(token, main_chat, offset_key="telegram_fx_offset")
    return from_env()


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


class TelegramUnreachable(ValueError):
    """Telegram could not be reached. A ValueError so the setup commands, which already print one and
    exit 1, report it as a plain sentence instead of a traceback."""


def _get_json(client: httpx.Client, url: str, params: dict | None = None,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """A GET that survives one bad moment, and explains itself when it does not.

    `send` was given retries this morning because a single timeout to api.telegram.org was losing a
    forex ticket. The setup commands were left making bare calls, and the first blip after that put
    sixty lines of httpx traceback on screen in place of "the network dropped, try again" -- for
    someone whose next move was to create a Telegram group on a phone. Same three attempts, same
    reasoning: this host times out often enough here that the project's own logs show it.
    """
    last: Exception | None = None
    for attempt in range(SEND_ATTEMPTS):
        try:
            r = client.get(url, params=params)
        except httpx.RequestError as exc:
            last = exc
        else:
            if r.status_code == 401:
                raise ValueError("Telegram rejected the bot token. Copy it again from @BotFather.")
            if r.status_code < 500:
                r.raise_for_status()
                return r.json()
            last = httpx.HTTPError(f"{r.status_code} from telegram")
        if attempt < SEND_ATTEMPTS - 1:
            sleep(RETRY_WAIT_S[0])
    raise TelegramUnreachable(
        f"Could not reach Telegram after {SEND_ATTEMPTS} tries ({type(last).__name__}). "
        "Nothing was changed. Check the connection and run the same command again.")


def find_chat(token: str, client: httpx.Client | None = None) -> tuple[str, dict | None]:
    """(bot username, the most recent private chat that messaged the bot, if any)."""
    client = client or httpx.Client(timeout=10)
    base = f"https://api.telegram.org/bot{token}"
    username = _get_json(client, f"{base}/getMe")["result"]["username"]
    updates = _get_json(client, f"{base}/getUpdates", {"timeout": 0}).get("result", [])
    chats = [u["message"]["chat"] for u in updates if u.get("message", {}).get("chat", {}).get("type") == "private"]
    return username, (chats[-1] if chats else None)


def find_group_chat(token: str, client: httpx.Client | None = None) -> tuple[str, dict | None]:
    """(bot username, the most recent GROUP or CHANNEL the bot can see, if any).

    Separate from `find_chat`, which deliberately looks only at private chats: the main chat is the
    user's own conversation with the bot, and a group turning up there would be wrong. The forex split
    is the opposite case and needs the groups.

    Reads `my_chat_member` as well as messages, because that is the update Telegram sends when the bot
    is ADDED to a group -- and it arrives whether or not the bot has privacy mode on. Relying on an
    ordinary group message would fail silently for anyone whose bot has the default privacy setting,
    since the bot never receives those.
    """
    client = client or httpx.Client(timeout=10)
    base = f"https://api.telegram.org/bot{token}"
    username = _get_json(client, f"{base}/getMe")["result"]["username"]
    updates = _get_json(client, f"{base}/getUpdates", {"timeout": 0}).get("result", [])
    wanted = {"group", "supergroup", "channel"}
    found = [chat for u in updates
             for chat in (((u.get("message") or {}).get("chat")),
                          ((u.get("channel_post") or {}).get("chat")),
                          ((u.get("my_chat_member") or {}).get("chat")))
             if chat and chat.get("type") in wanted]
    return username, (found[-1] if found else None)


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
