"""What the dashboard says about a sleeve's condition, as opposed to its numbers.

A sleeve that is winding down, or that wanted a market and could not take it, shows an equity figure and
a return like any other. Both of those are states you should not have to read the config or a Telegram
history to discover.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trader.engine.dashboard import _blocked_today, _retire_pill


class FakeJournal:
    def __init__(self, kv=None):
        self._kv = kv or {}

    def kv_get(self, key, default=None):
        return self._kv.get(key, default)


def test_a_winding_down_sleeve_says_so():
    pill = _retire_pill({"retiring": True, "retired": False})
    assert "winding down" in pill
    assert "no new trades" in pill, "say what it means, not just that it is happening"


def test_a_finished_retirement_says_that_instead():
    pill = _retire_pill({"retiring": True, "retired": True})
    assert "retired" in pill and "no longer trading" in pill
    assert "winding down" not in pill, "it is past that"


def test_an_ordinary_sleeve_gets_no_pill():
    assert _retire_pill({"retiring": False, "retired": False}) == ""
    assert _retire_pill({}) == ""


def test_a_market_blocked_today_stays_visible_all_day():
    """The alert fires once a day on purpose; the warnings list only renders the last hour.

    Without this the page would quietly stop mentioning a market the sleeve still cannot buy -- the
    notification would have come and gone while the condition was still true.
    """
    now = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
    j = FakeJournal({
        "unaffordable:alpaca:2026-10-08": "SPY ($544 < 1 share at $777.22)",
        "washblocked:alpaca:2026-10-08": "QQQ (held by S1)",
    })
    blocked = _blocked_today(j, now)
    kinds = {b["kind"] for b in blocked}
    assert kinds == {"unaffordable", "washblocked"}
    assert any("SPY" in b["detail"] for b in blocked)
    assert any("another sleeve holds it" in b["why"] for b in blocked)


def test_yesterdays_block_is_not_shown_today():
    now = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
    j = FakeJournal({"unaffordable:alpaca:2026-10-07": "SPY (yesterday)"})
    assert _blocked_today(j, now) == [], "a stale condition must not look current"


def test_the_old_marker_format_is_not_rendered_as_a_symbol():
    """These keys used to store "1" just to mark that an alert had been sent. Rendering that as a
    blocked market would read as a symbol called 1."""
    now = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
    assert _blocked_today(FakeJournal({"unaffordable:alpaca:2026-10-08": "1"}), now) == []
