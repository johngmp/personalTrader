"""Reporting in AUD as well as USD.

Everything here trades US-denominated assets, so USD is the right currency to JUDGE a strategy in: an
AUD-converted return mixes the strategy's skill with the currency's movement. But the account belongs to
someone who spends Australian dollars, and the plan starts live with about US$100, so "what did I
actually make" is an AUD question. Both are shown; neither replaces the other.

The rate is written to the journal by the engine on each run, so the dashboard and menu bar read a stored
number instead of making a network call while rendering a page. A rate that cannot be fetched is reported
as missing rather than guessed -- a wrong rate is worse than no AUD column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.journal.db import Journal

KV_RATE = "aud_per_usd"
KV_RATE_TS = "aud_per_usd_ts"
STALE_AFTER = timedelta(days=4)   # a long weekend of closed FX markets is fine; a week is not


@dataclass(frozen=True)
class Rate:
    aud_per_usd: float
    ts: datetime

    def to_aud(self, usd: float) -> float:
        return usd * self.aud_per_usd

    def is_stale(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) - self.ts > STALE_AFTER


def fetch_rate(now: datetime | None = None) -> Rate:
    """How many AUD one USD buys, from the AUD/USD quote.

    AUD/USD is quoted as USD per AUD (0.71 means one Australian dollar buys 71 US cents), so the number
    we want is its reciprocal. Getting this upside down would overstate the account by about half.
    """
    from trader.data.fx_quotes import YahooFXFeed
    from trader.models import AssetClass

    quote = YahooFXFeed().get_quote("AUD/USD", AssetClass.FOREX)
    if not 0.2 < quote.price < 2.0:
        raise ValueError(f"AUD/USD of {quote.price} is outside any plausible range; refusing to convert")
    return Rate(aud_per_usd=1.0 / quote.price, ts=quote.ts)


def store_rate(journal: Journal, rate: Rate) -> None:
    journal.kv_set(KV_RATE, f"{rate.aud_per_usd:.6f}")
    journal.kv_set(KV_RATE_TS, rate.ts.isoformat(timespec="seconds"))


def stored_rate(journal: Journal) -> Rate | None:
    """The last rate the engine recorded, or None if there isn't a usable one."""
    value, ts = journal.kv_get(KV_RATE), journal.kv_get(KV_RATE_TS)
    if not value or not ts:
        return None
    try:
        return Rate(aud_per_usd=float(value), ts=datetime.fromisoformat(ts))
    except ValueError:
        return None


def format_aud(usd: float, rate: Rate | None, signed: bool = False) -> str:
    """'A$1,234.56', or an empty string when there is no trustworthy rate to use."""
    if rate is None or rate.is_stale():
        return ""
    aud = rate.to_aud(usd)
    body = f"{aud:+,.2f}" if signed else f"{aud:,.2f}"
    return body.replace("+", "+A$").replace("-", "−A$") if body[0] in "+-" else f"A${body}"
