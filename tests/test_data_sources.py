"""News storage and fundamentals parsing (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from trader.data.fundamentals import snapshot_from_ticker
from trader.data.news import NewsItem, from_alpaca_news_symbol, recent, store, to_alpaca_news_symbol

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def item(i: str, hours_ago: float, symbols: list[str]) -> NewsItem:
    return NewsItem(id=i, created_at=NOW - timedelta(hours=hours_ago), headline=f"headline {i}", summary="",
                    source="benzinga", symbols=symbols, url="")


def test_crypto_symbols_map_to_alpaca_news_tags():
    assert to_alpaca_news_symbol("BTC/USD") == "BTCUSD"
    assert from_alpaca_news_symbol("BTCUSD", ["BTC/USD", "SPY"]) == "BTC/USD"
    assert from_alpaca_news_symbol("SPY", ["BTC/USD", "SPY"]) == "SPY"
    assert from_alpaca_news_symbol("TSLA", ["BTC/USD", "SPY"]) is None


def test_news_is_stored_once_and_filtered_by_symbol_and_age(journal):
    assert store(journal, [item("1", 1, ["SPY"]), item("2", 5, ["BTC/USD"]), item("3", 100, ["SPY"])]) == 3
    assert store(journal, [item("1", 1, ["SPY"])]) == 0  # already stored
    assert [r["id"] for r in recent(journal, ["SPY"], hours=48, now=NOW)] == ["1"]
    assert [r["id"] for r in recent(journal, None, hours=48, now=NOW)] == ["1", "2"]


class FakeTicker:
    info = {"shortName": "Example Corp", "trailingPE": 20.0, "profitMargins": 0.25, "revenueGrowth": float("nan"),
            "dividendYield": 0.8, "priceToBook": 0.0}

    @property
    def quarterly_income_stmt(self):
        cols = pd.to_datetime(["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"])
        return pd.DataFrame([[120, 110, 105, 100, 100], [30, 28, 27, 25, 24]], index=["Total Revenue", "Net Income"],
                            columns=cols)


def test_fundamentals_snapshot_is_clean_and_comparable():
    snap = snapshot_from_ticker("EXM", FakeTicker())
    assert snap["name"] == "Example Corp" and snap["pe_trailing"] == 20.0
    assert snap["revenue_growth_yoy"] is None          # NaN from Yahoo becomes null, not a fake number
    assert snap["dividend_yield_pct"] == 0.8            # Yahoo already reports this one in percent
    assert snap["price_to_book"] is None                # a 0.0 ratio means "unknown", not a bargain
    assert snap["quarters"][0] == {"quarter_end": "2026-06-30", "revenue": 120, "operating_income": None,
                                   "net_income": 30, "eps_diluted": None}
    assert abs(snap["revenue_growth_same_quarter_last_year"] - 0.2) < 1e-9


def test_telegram_setup_finds_the_private_chat_and_updates_env(tmp_path):
    import httpx

    from trader.notify.telegram import find_chat, set_env_value

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_trader_bot"}})
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": -100, "type": "group"}, "text": "hi"}},
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "first_name": "Sam"}, "text": "/start"}},
        ]})

    username, chat = find_chat("TOKEN", httpx.Client(transport=httpx.MockTransport(handler)))
    assert username == "my_trader_bot" and chat["id"] == 42

    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=PKX\nTELEGRAM_CHAT_ID=\n")
    set_env_value(env, "TELEGRAM_CHAT_ID", "42")
    assert env.read_text() == "ALPACA_API_KEY=PKX\nTELEGRAM_CHAT_ID=42\n"


def test_telegram_commands_only_from_the_owner_chat():
    import httpx

    from trader.notify.telegram import Telegram

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/halt"}},      # a stranger
            {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status@my_trader_bot"}},
        ]})

    class Store(dict):
        def kv_get(self, k, default=None):
            return self.get(k, default)

        def kv_set(self, k, v):
            self[k] = v

    store = Store()
    bot = Telegram("TOKEN", "42", httpx.Client(transport=httpx.MockTransport(handler)))
    assert bot.poll_commands(store) == ["/status"]
    assert store["telegram_offset"] == "7"
