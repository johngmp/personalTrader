"""The left icon rail, its sections, and the search.

A nav button pointing at a section that does not exist is a dead click, and a section with no button is
unreachable. Both are invisible in a screenshot and obvious in a test.
"""

from __future__ import annotations

import re

import pytest

from trader.engine import dashboard as dash


@pytest.fixture(scope="module")
def page() -> str:
    """The real rendered page, from the running project's own data."""
    from pathlib import Path

    from trader.config import load_settings
    from trader.engine.brokers_setup import configured_brokers
    from trader.engine.live import Engine
    from trader.journal.db import Journal

    settings = load_settings()
    journal = Journal(Path("data/journal.db"))
    engines = [Engine(settings, b, journal, bars) for b, bars in configured_brokers(settings, journal)]
    try:
        return dash.dashboard_html(settings, journal, engines)
    finally:
        journal.close()


def buttons(page: str) -> set[str]:
    return set(re.findall(r'class="rail-btn tab[^"]*" data-tab="([a-z]+)"', page))


def sections(page: str) -> set[str]:
    return set(re.findall(r'<section data-panel="([a-z]+)"', page))


def test_every_nav_button_leads_somewhere(page):
    assert buttons(page) - sections(page) == set(), "a button with no section is a dead click"


def test_every_section_is_reachable(page):
    assert sections(page) - buttons(page) == set(), "a section with no button cannot be opened"


def test_the_sections_a_beginner_needs_are_all_there(page):
    assert {"overview", "stocks", "forex", "trades", "orders", "evidence"} <= sections(page)


def test_exactly_one_section_starts_open(page):
    """Counted from the section tags themselves: `hidden` also appears on the Claude panel."""
    tags = re.findall(r'<section data-panel="([a-z]+)"([^>]*)>', page)
    visible = [name for name, attrs in tags if "hidden" not in attrs]
    assert visible == ["overview"], f"expected only overview open, got {visible}"
    assert len(re.findall(r'class="rail-btn tab active"', page)) == 1


def test_every_button_has_a_label_for_screen_readers(page):
    for match in re.finditer(r'<button class="rail-btn[^>]*>', page):
        assert "aria-label=" in match.group(0)
        assert "title=" in match.group(0)


def test_icons_are_inline_so_nothing_is_fetched(page):
    """One self-contained file: no icon font, no CDN, works with no network."""
    assert page.count("<svg viewBox") >= 7
    for remote in ("http://", "https://", "//cdn", "<link"):
        assert remote not in page.split("<style>")[0], "the shell must not fetch anything"


# --- search ----------------------------------------------------------------------------------------

def test_the_search_box_exists_and_is_labelled(page):
    assert 'id="searchbox"' in page and 'aria-label="Search' in page


def test_search_never_hides_a_table_header():
    """Hiding the header would leave unlabelled columns, which is worse than showing everything."""
    assert "if (tr.querySelector('th')) return;" in dash.SEARCH_JS


def test_search_covers_every_table_not_just_one():
    assert "main table tr" in dash.SEARCH_JS


def test_clearing_the_search_restores_every_row():
    assert "box.value = ''; apply();" in dash.SEARCH_JS


def test_slash_opens_search_but_not_while_typing():
    """Otherwise a "/" in a question to Claude would hijack the page."""
    assert "INPUT|TEXTAREA" in dash.SEARCH_JS


# --- the sidebar: labels, counts and the watchlist ---------------------------------------------------

def test_the_sidebar_is_grouped_rather_than_a_row_of_anonymous_icons(page):
    """Seven unlabelled icons is a memory test. Groups and words are not."""
    for group in ("Main", "Activity", "Research", "Watchlist"):
        assert f'class="rail-group">{group}' in page


def test_every_nav_item_has_a_visible_word_not_just_an_icon(page):
    for label in ("Overview", "Stocks &amp; crypto", "Forex", "History", "Orders", "Evidence"):
        assert f"<span>{label}</span>" in page


def test_the_watchlist_shows_every_traded_symbol_with_a_price(page):
    """Every symbol an enabled sleeve can trade, and nothing else.

    This used to assert a bare `>= 10` rows, which quietly tracked however many sleeves happened to be
    switched on: disabling S8 took the watchlist to 8 and failed a test about the dashboard for a reason
    that had nothing to do with it. The invariant worth holding is that the list matches the enabled
    sleeves, so it now fails when a symbol is genuinely missing rather than when the count moves.
    """
    import re

    from trader.config import load_settings

    settings = load_settings()
    expected = {sym for s in settings.sleeves.values() if s.enabled for sym in s.symbols}
    shown = set(re.findall(r'<span class="wsym">([^<]+)</span>', page))
    assert shown == expected, f"missing {sorted(expected - shown)}, unexpected {sorted(shown - expected)}"
    assert 'class="wpx"' in page


def test_collapsing_hides_labels_without_a_second_layout(page):
    """One rail with a modifier class; two layouts would drift apart."""
    assert "body.rail-min .rail-btn span" in page and "display:none" in page
    assert "body.rail-min{padding-left:62px}" in page


def test_the_collapsed_state_is_remembered(page):
    from trader.engine import dashboard as d

    assert "localStorage.setItem('railMin'" in d.RAIL_JS


def test_a_warning_shows_a_marker_on_the_orders_item(page):
    from trader.engine import dashboard as d
    import inspect

    assert 'dot-warn' in inspect.getsource(d)


def test_the_forex_chart_draws_on_load_not_only_on_click():
    """The open tab is restored from last visit, so arriving straight on Forex must not show a blank box."""
    from trader.engine import dashboard as d

    assert "drawWhenVisible();" in d.FX_JS
    assert "chart.offsetParent !== null" in d.FX_JS, "must not draw into a hidden, zero-width element"
