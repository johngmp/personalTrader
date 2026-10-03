"""AUD reporting. The conversion direction is the whole risk: inverted, it overstates by ~45%."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.journal.db import Journal
from trader.money import STALE_AFTER, Rate, format_aud, stored_rate, store_rate

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
# AUD/USD 0.6957 means one Australian dollar buys ~70 US cents, so one USD buys ~1.4373 AUD.
RATE = Rate(aud_per_usd=1.4373, ts=NOW)


def test_one_usd_buys_more_than_one_aud_at_these_levels():
    """The direction check: AUD/USD below 1 means USD is the stronger currency."""
    assert RATE.aud_per_usd > 1.0
    assert RATE.to_aud(100.0) == pytest.approx(143.73)


def test_converting_does_not_silently_shrink_the_account():
    """Inverting the rate would report US$100,000 as A$69,570 instead of A$143,730."""
    assert RATE.to_aud(100_000) == pytest.approx(143_730)
    assert RATE.to_aud(100_000) != pytest.approx(69_570, rel=0.01)


@pytest.mark.parametrize(("usd", "signed", "expected"), [
    (100_000.0, False, "A$143,730.00"),
    (1000.0, True, "+A$1,437.30"),
    (-1000.0, True, "−A$1,437.30"),
])
def test_formatting(usd, signed, expected):
    assert format_aud(usd, RATE, signed=signed) == expected


def test_no_rate_means_no_aud_column_rather_than_a_guess():
    assert format_aud(1000.0, None) == ""


def test_a_stale_rate_is_withheld_rather_than_used():
    """A wrong number is worse than a missing one; it would look authoritative."""
    old = Rate(aud_per_usd=1.4373, ts=datetime.now(timezone.utc) - STALE_AFTER - timedelta(days=1))
    assert old.is_stale()
    assert format_aud(1000.0, old) == ""


def test_a_long_weekend_does_not_make_a_rate_stale():
    """Forex shuts every weekend; three days old is normal, not broken."""
    assert not Rate(1.4373, datetime.now(timezone.utc) - timedelta(days=3)).is_stale()


def test_the_rate_survives_a_round_trip_through_the_journal(tmp_path):
    journal = Journal(tmp_path / "j.db")
    store_rate(journal, RATE)
    back = stored_rate(journal)
    assert back is not None
    assert back.aud_per_usd == pytest.approx(RATE.aud_per_usd, abs=1e-6)
    assert back.ts == RATE.ts


def test_an_empty_journal_has_no_rate(tmp_path):
    assert stored_rate(Journal(tmp_path / "j.db")) is None


def test_a_corrupt_stored_rate_is_ignored_rather_than_crashing_a_page(tmp_path):
    journal = Journal(tmp_path / "j.db")
    journal.kv_set("aud_per_usd", "not a number")
    journal.kv_set("aud_per_usd_ts", NOW.isoformat())
    assert stored_rate(journal) is None


def test_an_implausible_quote_is_refused(monkeypatch):
    """A feed glitch returning 0.0 or 100 must not silently rewrite the account."""
    from trader import money
    from trader.models import AssetClass, Quote

    for bad in (0.0, 0.1, 5.0):
        monkeypatch.setattr("trader.data.fx_quotes.YahooFXFeed.get_quote",
                            lambda self, s, a, _b=bad: Quote(symbol="AUD/USD", price=_b, ts=NOW))
        with pytest.raises(ValueError, match="plausible"):
            money.fetch_rate()


def test_a_failed_rate_fetch_is_not_reported_as_a_trading_problem():
    """`report.problems` sends a Telegram warning. "Could not fetch a display currency" does not
    belong in the same channel as "could not place a protective stop"."""
    import inspect

    from trader.engine.live import Engine

    source = inspect.getsource(Engine._record_fx_rate)
    body = source.split('"""')[2]          # skip the docstring, which discusses report.problems
    assert "try:" in body and "except Exception" in body
    assert "report.problems" not in body, "a display currency must not escalate to a trading warning"
    assert 'level="info"' in body, "it should still be logged, just not escalated"
