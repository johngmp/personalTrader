"""News headlines from Alpaca's free Benzinga feed, stored in the journal.

Used by the Phase 4 AI strategies (the analyst team reads it; `trader news --json`). Every headline an AI
decision could have seen is kept, so later reviews can check what information a trade was based on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from trader.journal.db import Journal


@dataclass
class NewsItem:
    id: str
    created_at: datetime
    headline: str
    summary: str
    source: str
    symbols: list[str]    # our symbols, e.g. "BTC/USD"
    url: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat(timespec="seconds")
        return d


class NewsSource(Protocol):
    def fetch(self, symbols: list[str], since: datetime, limit: int) -> list[NewsItem]: ...


def to_alpaca_news_symbol(symbol: str) -> str:
    """Alpaca's news feed tags crypto as 'BTCUSD' rather than 'BTC/USD'."""
    return symbol.replace("/", "")


def from_alpaca_news_symbol(tag: str, ours: list[str]) -> str | None:
    lookup = {to_alpaca_news_symbol(s): s for s in ours}
    return lookup.get(tag, tag if tag in ours else None)


class AlpacaNews:
    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical.news import NewsClient

        self._client = NewsClient(api_key, secret_key)

    def fetch(self, symbols: list[str], since: datetime, limit: int = 50) -> list[NewsItem]:
        from alpaca.data.requests import NewsRequest

        request = NewsRequest(symbols=",".join(to_alpaca_news_symbol(s) for s in symbols), start=since,
                              limit=min(limit, 50), include_content=False)
        items = []
        for a in self._client.get_news(request).data.get("news", []):
            tags = [t for t in (from_alpaca_news_symbol(x, symbols) for x in (a.symbols or [])) if t]
            items.append(NewsItem(id=str(a.id), created_at=a.created_at, headline=a.headline or "",
                                  summary=a.summary or "", source=a.source or "", symbols=tags, url=a.url or ""))
        return items


NEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    headline    TEXT NOT NULL,
    summary     TEXT,
    source      TEXT,
    symbols     TEXT NOT NULL,   -- JSON list of our symbols
    url         TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at);
"""


def store(journal: Journal, items: list[NewsItem]) -> int:
    """Save headlines; returns how many were new."""
    journal.conn.executescript(NEWS_SCHEMA)
    before = journal.conn.total_changes
    with journal.conn:
        journal.conn.executemany(
            "INSERT OR IGNORE INTO news (id, created_at, headline, summary, source, symbols, url, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(i.id, i.created_at.astimezone(timezone.utc).isoformat(timespec="seconds"), i.headline, i.summary,
              i.source, json.dumps(i.symbols), i.url, datetime.now(timezone.utc).isoformat(timespec="seconds"))
             for i in items])
    return journal.conn.total_changes - before


def recent(journal: Journal, symbols: list[str] | None, hours: float, now: datetime | None = None) -> list[dict]:
    """Stored headlines from the last `hours`, newest first, optionally only those tagged with `symbols`."""
    journal.conn.executescript(NEWS_SCHEMA)
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = journal.conn.execute("SELECT * FROM news WHERE created_at >= ? ORDER BY created_at DESC", (since,)).fetchall()
    out = []
    for r in rows:
        tags = json.loads(r["symbols"])
        if symbols and not set(tags) & set(symbols):
            continue
        out.append({"id": r["id"], "created_at": r["created_at"], "headline": r["headline"], "summary": r["summary"],
                    "source": r["source"], "symbols": tags, "url": r["url"]})
    return out


def refresh(journal: Journal, source: NewsSource, symbols: list[str], hours: float = 48) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return store(journal, source.fetch(symbols, since, limit=50))
