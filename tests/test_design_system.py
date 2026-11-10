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
def page(live_page) -> str:
    """The real rendered page, built once for the whole session (see conftest.live_page)."""
    return live_page


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


# --- the light theme --------------------------------------------------------------------------------

def _contrast(a: str, b: str) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def lum(h: str) -> float:
        h = h.lstrip("#")
        r, g, bl = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(bl)

    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _tokens(scope: str) -> dict[str, str]:
    """The custom properties declared in one theme block."""
    body = re.search(re.escape(scope) + r"\{(.*?)\}", dash.CSS, re.S)
    assert body, f"no {scope} block in the stylesheet"
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", body.group(1)))


def test_the_light_theme_declares_every_token_the_dark_one_does():
    """A token the light block forgets silently keeps its DARK value -- black text on a white card is
    obvious, but a missed `--down-bg` is a dark red smear nobody notices until a loss renders."""
    dark = _tokens(":root")
    light = _tokens(':root[data-theme="light"]')
    colour_tokens = {k for k in dark if not k.startswith(("--t-", "--radius", "--rail", "--panel"))}
    missing = colour_tokens - set(light)
    assert not missing, f"the light theme never redefines {sorted(missing)}, so they stay dark"


def test_every_light_theme_text_colour_clears_wcag_aa():
    """This file says contrast was checked rather than eyeballed. That has to stay true of both themes,
    and light is where it is easiest to get wrong: the same green that reads on near-black is invisible
    on white."""
    light = _tokens(':root[data-theme="light"]')
    for token in ("--ink", "--ink2", "--muted", "--up", "--down", "--warn", "--accent"):
        for surface in ("--page", "--s1"):
            ratio = _contrast(light[token], light[surface])
            assert ratio >= 4.5, f"{token} on {surface} is {ratio:.2f}:1 in light mode, under AA's 4.5"


def test_the_light_theme_is_written_once_and_used_twice():
    """The OS-preference block and the explicit override need identical colours. Two hand-written
    copies of a palette is the drift this file warns about -- and a palette that drifts gives you one
    set of colours when you toggle and another when macOS does."""
    assert dash.CSS.count('--page:#f7f7f8') == 2, "the light palette should appear twice, from one source"
    media = re.search(r"@media \(prefers-color-scheme: light\)\{([^@]*?)\}\}", dash.CSS, re.S)
    assert media and ':root:not([data-theme="dark"])' in media.group(1), (
        "the OS preference must not override a reader who has explicitly chosen dark")


def test_dark_is_still_the_default():
    """Nobody who never touches the toggle should see the page change."""
    assert dash.CSS.index(":root{") < dash.CSS.index("prefers-color-scheme")
    assert _tokens(":root")["--page"] == "#0a0a0b"


def test_charts_take_their_colours_from_the_theme_not_from_literals():
    """An SVG stroke is an attribute, not a cascading property: a chart drawn with a literal keeps its
    dark gridlines on a white page. Both renderers go through tok(), and both re-draw on a change."""
    for name in ("JS", "FX_JS"):
        source = getattr(dash, name)
        for literal in ('stroke="#', 'fill="#'):
            assert literal not in source, f"{name} draws with a hardcoded colour: {literal}"
        assert "__ptRedrawCharts" in source, f"{name} never re-draws when the theme changes"
