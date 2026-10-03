"""The interface rules that are easy to undo by accident.

These are not assertions that the CSS says what the CSS says. Each one pins a rule that something
outside the stylesheet depends on, and that a plausible future edit would quietly break.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trader.engine import dashboard as dash
from trader.engine.explain import headline


@pytest.fixture(scope="module")
def page() -> str:
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


def test_figures_are_tabular_across_the_whole_page():
    """Rule 1. Proportional digits make a live price jitter sideways on every tick.

    This is set once on `body` and inherited, which is what makes it easy to lose: rewriting the `font`
    shorthand or the body rule drops it silently, and nothing looks broken in a screenshot -- the
    numbers just stop lining up as they update.
    """
    body = re.search(r"\bbody\{(.*?)\}", dash.CSS, re.S)
    assert body, "no body rule in the stylesheet"
    assert "tabular-nums" in body.group(1), "body lost font-variant-numeric:tabular-nums"


def test_no_gradients_behind_data():
    """Rule 3. A gradient behind a figure changes that figure's contrast across its own width.

    Cards used to be `linear-gradient(180deg,...)`. Depth here comes from flat surface steps instead.
    """
    card = re.search(r"\n\.card\{(.*?)\}", dash.CSS, re.S)
    assert card, "no .card rule"
    assert "gradient" not in card.group(1), f"gradient is back on .card: {card.group(1)}"


def test_every_headline_tone_has_a_style_and_a_glyph(page):
    """Rule 2, and a real contract between two modules.

    `explain.headline()` emits a tone string; the stylesheet turns it into `li.hl-<tone>`. Add a tone in
    explain.py with no matching CSS and it renders as an unstyled line -- no colour, no glyph, no
    indication of severity. Measured, the green and the red are only 1.52:1 against each other, so the
    glyph is not decoration: for a red-green colourblind reader it IS the signal.
    """
    tones = {"good", "bad", "warn", "plain"}
    emitted = {tone for tone, _text in headline({"kpis": {"closed": 0}, "sleeves": [], "total_pnl": 0.0,
                                                 "start_equity": 1000.0})}
    assert emitted <= tones, f"explain.py emitted an unknown tone: {emitted - tones}"
    for tone in tones:
        assert f"hl-{tone}" in dash.CSS, f"tone {tone!r} has no style"
        rule = re.search(r"li\.hl-" + tone + r"::before\{([^}]*)\}", dash.CSS)
        assert rule and "content:" in rule.group(1), f"tone {tone!r} has colour but no glyph"


def test_the_page_states_its_colour_contract_for_whoever_edits_it_next(page):
    """Green and red mean profit and loss, and nothing else, so a colour is never ambiguous."""
    for token in ("--up", "--down", "--warn", "--accent"):
        assert token in dash.CSS, f"{token} is gone"
    # The brand accent must not be either state colour, or a brand flourish would read as a result.
    up = re.search(r"--up:(#[0-9a-f]{6})", dash.CSS).group(1)
    down = re.search(r"--down:(#[0-9a-f]{6})", dash.CSS).group(1)
    accent = re.search(r"--accent:(#[0-9a-f]{6})", dash.CSS).group(1)
    assert accent not in (up, down), "the brand accent is a state colour; a result could be mistaken for chrome"


def test_chart_gridlines_stay_inside_the_plot():
    """A measured bug, not a hypothetical one.

    `nice()` rounds its first tick DOWN below the low and ran past the high, and `.chart svg` is
    overflow:visible, so the outermost labels escaped the box: on a 400px FX chart they were drawn at
    y=-24 (landing on the chart's own subtitle) and y=460. Both chart renderers clamp now.
    """
    clamps = dash.FX_JS.count("if (v >= lo && v <= hi)") + dash.JS.count("if (v >= lo && v <= hi)")
    assert clamps == 2, f"expected both chart renderers to clamp their ticks, found {clamps}"
    assert "overflow:visible" in dash.CSS, "clamping matters because the SVG is not clipped"
