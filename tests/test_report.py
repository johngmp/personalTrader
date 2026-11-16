"""The Gate 1 report, read as a surface rather than as a page.

A report is not a passive rendering of a result. It decides what the researcher sees before making the
next decision, which makes it part of the evaluation. This file guards the one place that was actively
harmful: "Every setting tried" listed an out-of-sample Sharpe for all eighteen grid cells, so the whole
held-out surface sat on the page. PLAN.md line 418 records the consequence in the project's own words --
"that was measured by peeking at out-of-sample and cannot be acted on" -- and research_log.jsonl shows
the same window read nineteen times.

These tests are written against the rendered HTML, not against a helper, because what matters is what
reaches the page.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from trader.research.evaluate import evaluate
from trader.research.report import _num, write_report


def _trending_bars(seed: int, n: int = 3000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    idx = pd.bdate_range("2005-01-03", periods=n)
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": 1.0}, index=idx)


def _report(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("trader.research.evaluate.RESEARCH_LOG", tmp_path / "log.jsonl")
    bars = {s: _trending_bars(i) for i, s in enumerate(["BTC/USD", "ETH/USD", "QQQ", "SPY"])}
    result = evaluate("S1", settings, bars)
    return result, (write_report(result, tmp_path)).read_text()


def _grid_card(page: str) -> str:
    """Just the 'Every setting tried' card, so a match elsewhere on the page proves nothing here."""
    start = page.index("<h2>Every setting tried</h2>")
    return page[start:page.index("<h2>", start + 10)]


def test_the_grid_lists_every_setting_with_its_in_sample_sharpe(settings, monkeypatch, tmp_path):
    """The guard on the guard.

    Deleting the table outright would satisfy the peeking test below while destroying the one thing the
    table is for: seeing whether the in-sample surface is flat, which is how you notice that which cell
    "wins" is noise. So the table must still be complete on the window selection is allowed to use.
    """
    result, page = _report(settings, monkeypatch, tmp_path)
    card = _grid_card(page)

    assert len(re.findall(r"<tr", card)) == len(result.grid_table) + 1, "one row per cell, plus a header"
    for row in result.grid_table:
        assert _num(row["is_sharpe"]) in card, f"missing in-sample Sharpe for {row['params']}"
    assert "← chosen" in card


def test_no_cell_s_out_of_sample_sharpe_is_printed_in_the_grid(settings, monkeypatch, tmp_path):
    """The actual defect. Eighteen held-out numbers on one page is eighteen free looks.

    The chosen cell is included in the sweep, not exempted. Its out-of-sample Sharpe belongs on the page
    -- it is the verdict -- but it belongs in Results, with the benchmark and the drawdown beside it,
    not as a column that invites reading down it.
    """
    result, page = _report(settings, monkeypatch, tmp_path)
    card = _grid_card(page)

    # Structurally first, which no coincidence can satisfy: two cells per row, the settings and one
    # number. A second numeric column is the defect, whatever it happens to contain.
    rows = re.findall(r"<tr class=[^>]*>(.*?)</tr>", card)
    assert len(rows) == len(result.grid_table)
    for row_html in rows:
        assert row_html.count("<td>") == 2, f"a grid row carries more than one number: {row_html}"

    # Then by value, which also catches a number smuggled into a title or tooltip rather than a cell.
    # Skipping any out-of-sample figure that happens to render identically to SOME cell's in-sample
    # figure: at two decimals those collide on real grids, and such a match would prove nothing.
    allowed = {_num(row["is_sharpe"]) for row in result.grid_table} | {"–"}
    checked = 0
    for row in result.grid_table:
        oos = _num(row["oos_sharpe"])
        if oos in allowed:
            continue
        assert oos not in card, f"out-of-sample Sharpe {oos} for {row['params']} is on the page"
        checked += 1
    assert checked >= len(result.grid_table) // 2, \
        f"only {checked} of {len(result.grid_table)} cells were distinguishable enough to check by value"


def test_the_grid_says_why_the_column_is_missing(settings, monkeypatch, tmp_path):
    """A removed column with no explanation gets added back by the next person with a good reason."""
    _, page = _report(settings, monkeypatch, tmp_path)
    card = _grid_card(page)
    assert "In-sample only, on purpose." in card
    assert "nineteen times" in card and "costs a trial in the ledger" in card


def test_the_chosen_configuration_s_out_of_sample_sharpe_is_still_in_results(settings, monkeypatch, tmp_path):
    """Hiding the verdict would be the opposite failure: the number the gate turns on must be on the page.

    Pinned positionally, in the Sharpe row of the Results table, because the obvious version of this
    test is worthless. Asserting that a four-character string like "0.42" appears somewhere in a page of
    HTML passes almost always -- verified: a mutation that deleted both the verdict block and the whole
    Results table left it green, because the number turns up in a chart label or the yearly table by
    coincidence. So this reads the specific cell, which also pins the column order (In-sample, Bench,
    Out-of-sample, Bench, Walk-forward, Bench) that has to be known to read the report at all.
    """
    result, page = _report(settings, monkeypatch, tmp_path)

    row = re.search(r"<tr><td>Sharpe \(risk-adjusted\)</td>(.*?)</tr>", page)
    assert row, "the Results table has no Sharpe row"
    cells = re.findall(r"<td>(.*?)</td>", row.group(1))
    assert len(cells) == 6, f"expected six metric columns, got {cells}"
    assert cells[0] == _num(result.is_metrics.sharpe), "column 1 is not in-sample"
    assert cells[2] == _num(result.oos_metrics.sharpe), "column 3 is not the chosen config's out-of-sample"
    assert cells[3] == _num(result.bench_oos.sharpe), "column 4 is not the out-of-sample benchmark"
