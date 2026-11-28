"""The Claude chat panel's JS: the auto-refresh guard, cancelling a stale reply, and showing a cut-off.

Three bugs a factory UX review found, all invisible from the Python side because nothing here renders
the page in a browser:

  * the auto-refresh timer's "don't reload while chatting" guard checked a `.tab[data-tab="ask"]`
    selector that was never in the markup -- the panel is `#claude-panel`, not a rail tab -- so the
    guard always failed and refresh silently wiped the conversation every interval.
  * clicking "New" while a reply was still streaming cleared the log but left the old fetch running;
    its 'done' event could still land afterwards and resurrect the abandoned conversation.
  * a reply cut short by the turn/budget cap rendered identically to a complete one.

These assertions can only check the JS source text, not that a browser behaves correctly -- that part
needs manual verification (or the browser-automation skill) against a running `trader serve`.
"""

from __future__ import annotations

from trader.engine import dashboard as dash


def test_the_refresh_guard_no_longer_checks_the_nonexistent_ask_tab():
    assert 'data-tab="ask"' not in dash.CHAT_JS


def test_the_refresh_guard_checks_whether_the_chat_panel_is_open():
    assert "document.body.classList.contains('claude-open')" in dash.CHAT_JS


def test_new_aborts_any_reply_still_streaming():
    assert "AbortController" in dash.CHAT_JS
    assert "window.__ptAbortChat" in dash.CHAT_JS
    fresh = dash.CHAT_JS[dash.CHAT_JS.index("var fresh = document.getElementById('claude-clear')"):]
    fresh = fresh[: fresh.index("});") + 3]
    assert "window.__ptAbortChat()" in fresh, "New must cancel the in-flight request, not just clear the log"


def test_an_aborted_reply_does_not_resurrect_itself_in_the_log():
    """The pump loop must stop touching the DOM once its own controller has been aborted."""
    assert dash.CHAT_JS.count("mine.signal.aborted") >= 2


def test_a_truncated_reply_gets_a_visible_marker():
    assert "ev.payload.truncated" in dash.CHAT_JS
    assert "add('cut'" in dash.CHAT_JS


def test_the_truncation_marker_has_its_own_style_distinct_from_an_error():
    assert ".msg.cut{" in dash.CSS
    assert ".msg.err{" in dash.CSS
