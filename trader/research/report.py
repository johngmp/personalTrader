"""Self-contained HTML report for a Gate 1 run (open it in any browser; works offline)."""

from __future__ import annotations

import html
import inspect
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from trader.config import PROJECT_ROOT
from trader.research.evaluate import Gate1Result
from trader.research.metrics import Metrics
from trader.strategies import REGISTRY

REPORT_DIR = PROJECT_ROOT / "reports" / "backtests"

# Validated categorical slots 1-2 (dataviz palette): strategy = blue, benchmark = orange.
SERIES = [("--s1", "#2a78d6", "#3987e5"), ("--s2", "#eb6834", "#d95926")]

CSS = """
:root { --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
  --axis:#c3c2b7; --good:#006300; --bad:#b42323; --s1:#2a78d6; --s2:#eb6834; }
@media (prefers-color-scheme: dark) { :root { --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --good:#0ca30c; --bad:#e66767; --s1:#3987e5; --s2:#d95926; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--page); color:var(--ink); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 8px; }
.sub { color:var(--ink2); margin:0 0 16px; }
.verdict { display:inline-flex; gap:8px; align-items:center; font-weight:650; padding:6px 12px; border-radius:8px;
  border:1px solid var(--axis); background:var(--surface); }
.pass { color:var(--good); } .fail { color:var(--bad); }
.card { background:var(--surface); border:1px solid var(--grid); border-radius:10px; padding:16px; margin:12px 0; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--grid); }
th:first-child, td:first-child { text-align:left; }
th { color:var(--ink2); font-weight:600; font-size:13px; }
tr.chosen td { font-weight:650; }
.checks td:first-child { width:28px; }
.legend { display:flex; gap:16px; flex-wrap:wrap; color:var(--ink2); font-size:13px; margin-bottom:6px; }
.sw { display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }
.chart { position:relative; }
.chart svg { width:100%; height:auto; display:block; overflow:visible; }
.chart text { fill:var(--muted); font-size:11px; }
.chart .end { fill:var(--ink2); font-size:12px; font-weight:600; }
.tip { position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--axis);
  border-radius:8px; padding:6px 8px; font-size:12px; color:var(--ink); display:none; white-space:nowrap;
  box-shadow:0 2px 8px rgba(0,0,0,.12); }
.tip .d { color:var(--ink2); margin-bottom:2px; }
.scroll { overflow-x:auto; }
ul { padding-left:20px; } li { margin:4px 0; }
details summary { cursor:pointer; color:var(--ink2); }
"""

HOVER_JS = """
document.querySelectorAll('.chart').forEach(function (box) {
  var data = JSON.parse(box.dataset.series), svg = box.querySelector('svg'), tip = box.querySelector('.tip');
  var cross = svg.querySelector('.cross'), dots = svg.querySelectorAll('.dot'), fmt = box.dataset.fmt;
  function show(ev) {
    var r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal, x = (ev.clientX - r.left) * vb.width / r.width;
    var xs = data.x, i = 0, best = 1e9;
    for (var k = 0; k < xs.length; k++) { var d = Math.abs(xs[k] - x); if (d < best) { best = d; i = k; } }
    cross.setAttribute('x1', xs[i]); cross.setAttribute('x2', xs[i]); cross.style.display = '';
    var rows = '<div class="d">' + data.dates[i] + '</div>';
    data.lines.forEach(function (ln, j) {
      var v = ln.v[i]; dots[j].setAttribute('cx', xs[i]); dots[j].setAttribute('cy', ln.y[i]);
      dots[j].style.display = v === null ? 'none' : '';
      var txt = v === null ? '–' : (fmt === 'pct' ? (v * 100).toFixed(1) + '%' : '$' + Math.round(v).toLocaleString());
      rows += '<div><span class="sw" style="background:var(' + ln.c + ')"></span>' + ln.n + ': <b>' + txt + '</b></div>';
    });
    tip.innerHTML = rows; tip.style.display = 'block';
    var px = xs[i] * r.width / vb.width; tip.style.left = Math.min(px + 12, r.width - tip.offsetWidth) + 'px';
    tip.style.top = '8px';
  }
  svg.addEventListener('mousemove', show);
  svg.addEventListener('mouseleave', function () { tip.style.display = 'none'; cross.style.display = 'none';
    dots.forEach(function (d) { d.style.display = 'none'; }); });
});
"""


def _pct(x: float, digits: int = 1) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{digits}f}%"


def _num(x: float, digits: int = 2) -> str:
    return "–" if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else f"{x:.{digits}f}"


def _nice_log_ticks(lo: float, hi: float) -> list[float]:
    ticks = []
    for exp in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in (1, 2, 5):
            v = m * 10 ** exp
            if lo <= v <= hi:
                ticks.append(v)
    return ticks if len(ticks) >= 2 else [lo, hi]


def line_chart(series: list[tuple[str, pd.Series]], *, log: bool, fmt: str, split: pd.Timestamp | None,
               title: str, resample: str | None = "W-FRI") -> str:
    """Inline SVG line chart (one y-axis) with a hover crosshair and tooltip. Weekly points keep it light."""
    frame = pd.concat({name: s for name, s in series}, axis=1)
    if resample:
        frame = frame.resample(resample).last()
    frame = frame.dropna(how="all")
    W, H, L, R, T, B = 800, 310, 64, 150, 26, 28
    x0, x1 = frame.index[0], frame.index[-1]
    span = (x1 - x0).total_seconds() or 1.0
    xs = [L + (d - x0).total_seconds() / span * (W - L - R) for d in frame.index]
    vals = frame.to_numpy(dtype=float)
    finite = vals[~pd.isna(vals)]
    lo, hi = float(finite.min()), float(finite.max())
    if log:
        lo, hi = max(lo, 1e-9) * 0.95, hi * 1.05
        f = lambda v: math.log10(v)  # noqa: E731
        ticks = _nice_log_ticks(lo, hi)
    else:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
        f = lambda v: v  # noqa: E731
        step = 0.1 if hi - lo <= 0.8 else 0.2
        ticks = [round(-i * step, 2) for i in range(0, int(abs(lo) / step) + 1)]
    flo, fhi = f(lo), f(hi)
    y = lambda v: T + (fhi - f(v)) / ((fhi - flo) or 1) * (H - T - B)  # noqa: E731

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">']
    for tv in ticks:
        ty = y(tv)
        label = (f"${tv:,.0f}" if tv >= 1 else f"${tv:,.2f}") if fmt == "usd" else f"{tv * 100:.0f}%"
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{L - 8}" y="{ty + 4:.1f}" text-anchor="end">{label}</text>')
    years = sorted({d.year for d in frame.index})
    every = max(1, math.ceil(len(years) / 8))
    for yr in years[::every]:
        d = pd.Timestamp(year=yr, month=1, day=1)
        if x0 <= d <= x1:
            tx = L + (d - x0).total_seconds() / span * (W - L - R)
            parts.append(f'<text x="{tx:.1f}" y="{H - 8}" text-anchor="middle">{yr}</text>')
    parts.append(f'<line x1="{L}" x2="{W - R}" y1="{H - B}" y2="{H - B}" stroke="var(--axis)" stroke-width="1"/>')
    if split is not None and x0 <= split <= x1:
        sx = L + (split - x0).total_seconds() / span * (W - L - R)
        parts.append(f'<line x1="{sx:.1f}" x2="{sx:.1f}" y1="{T}" y2="{H - B}" stroke="var(--muted)" '
                     f'stroke-dasharray="4 4" stroke-width="1"/>')
        parts.append(f'<text x="{sx + 6:.1f}" y="{T - 10}">out-of-sample →</text>')

    lines_js, ends = [], []
    for j, (name, _) in enumerate(series):
        var = SERIES[j][0]
        col = frame[name]
        pts = [(xs[i], y(v)) for i, v in enumerate(col) if not pd.isna(v) and (not log or v > 0)]
        if not pts:
            continue
        d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        parts.append(f'<path d="{d}" fill="none" stroke="var({var})" stroke-width="2" stroke-linejoin="round" '
                     f'stroke-linecap="round"/>')
        last = col.dropna().iloc[-1]
        ends.append((pts[-1][1], name, last, var))
        lines_js.append({"n": name, "c": var,
                         "v": [None if pd.isna(v) else round(float(v), 4) for v in col],
                         "y": [round(y(v), 1) if not pd.isna(v) and (not log or v > 0) else None for v in col]})
    ends.sort()
    prev = -1e9
    for ey, name, last, var in ends:  # direct end labels, nudged apart so they never overlap
        ey = max(ey, prev + 14)
        prev = ey
        val = f"${last:,.0f}" if fmt == "usd" else f"{last * 100:.1f}%"
        parts.append(f'<text class="end" x="{W - R + 8}" y="{ey + 4:.1f}">{html.escape(name)} {val}</text>')
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="var(--muted)" stroke-width="1" '
                 f'style="display:none"/>')
    for j in range(len(lines_js)):
        parts.append(f'<circle class="dot" r="4" fill="var({lines_js[j]["c"]})" stroke="var(--surface)" '
                     f'stroke-width="2" style="display:none"/>')
    parts.append(f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="transparent"/></svg>')
    stamp = (lambda d: str(d.date())) if resample else (lambda d: d.strftime("%Y-%m-%d %H:%M"))
    data = {"x": [round(v, 1) for v in xs], "dates": [stamp(d) for d in frame.index], "lines": lines_js}
    legend = "".join(f'<span><span class="sw" style="background:var({SERIES[j][0]})"></span>{html.escape(n)}</span>'
                     for j, (n, _) in enumerate(series))
    return (f'<div class="legend">{legend}</div><div class="chart" data-fmt="{fmt}" '
            f"data-series='{html.escape(json.dumps(data), quote=True)}'>{''.join(parts)}<div class=\"tip\"></div></div>")


def _metrics_rows(r: Gate1Result) -> str:
    cols: list[tuple[str, Metrics | None]] = [
        ("In-sample", r.is_metrics), ("Bench", r.bench_is), ("Out-of-sample", r.oos_metrics), ("Bench", r.bench_oos),
        ("Walk-forward", r.wf_metrics), ("Bench", r.bench_wf)]
    rows = [
        ("Period", lambda m: f"{m.start[:4]}–{m.end[:4]}"),
        ("Yearly return (CAGR)", lambda m: _pct(m.cagr)),
        ("Sharpe (risk-adjusted)", lambda m: _num(m.sharpe)),
        ("Worst drop (max drawdown)", lambda m: _pct(m.max_drawdown)),
        ("Volatility", lambda m: _pct(m.volatility)),
        ("Time invested", lambda m: _pct(m.exposure, 0)),
    ]
    head = "<tr><th></th>" + "".join(f"<th>{c}</th>" for c, _ in cols) + "</tr>"
    body = "".join("<tr><td>" + label + "</td>" + "".join(f"<td>{fn(m) if m else '–'}</td>" for _, m in cols)
                   + "</tr>" for label, fn in rows)
    trades = [("Completed trades", lambda m: str(m.n_trades)), ("Win rate", lambda m: _pct(m.win_rate, 0)),
              ("Profit factor", lambda m: _num(m.profit_factor)), ("Average R", lambda m: _num(m.avg_r)),
              ("Average days held", lambda m: _num(m.avg_bars_held, 1))]
    body += "".join("<tr><td>" + label + "</td>" + "".join(
        f"<td>{fn(m) if m and i in (0, 2) else '–'}</td>" for i, (_, m) in enumerate(cols)) + "</tr>"
        for label, fn in trades)
    return f'<div class="scroll"><table>{head}{body}</table></div>'


def _yearly_table(r: Gate1Result) -> str:
    s = r.equity.resample("YE").last()
    b = r.bench_equity.resample("YE").last()
    s_ret = s.pct_change()
    s_ret.iloc[0] = s.iloc[0] / r.equity.iloc[0] - 1
    b_ret = b.pct_change()
    b_ret.iloc[0] = b.iloc[0] / r.bench_equity.iloc[0] - 1
    rows = "".join(f"<tr><td>{d.year}</td><td>{_pct(sv)}</td><td>{_pct(bv)}</td></tr>"
                   for d, sv, bv in zip(s.index, s_ret, b_ret))
    return (f"<details><summary>Year-by-year returns (table view)</summary><div class='scroll'><table>"
            f"<tr><th>Year</th><th>Strategy</th><th>{html.escape(r.benchmark)}</th></tr>{rows}</table></div></details>")


def _oos_symbol_summary(r: Gate1Result) -> str:
    """How wide the out-of-sample profit was -- without naming which symbols made it.

    This section used to be a table of held-out profit per symbol, all forty rows of it. That is a
    worse peeking surface than the parameter grid was, and for a sharper reason: a grid tells you which
    settings to prefer, but this tells you which MARKETS to drop, and dropping the losers is a single
    edit to `symbols` in sleeves.yaml that no gate would notice. Doing it would manufacture a basket
    selected on the held-out window and then report that window's Sharpe as out-of-sample evidence for
    it. The universe caveat already on every report -- symbols were picked today, knowing which assets
    did well -- would become the whole result rather than a footnote.

    What the gate actually uses is the count, so the count is what is shown. The concentration figure
    stays because it is the one thing the table was legitimately good for: a strategy whose entire
    held-out profit came from one market is fragile in a way the count alone hides. It cannot be acted
    on the way the table could -- it says that something is concentrated, not what to keep.
    """
    traded = {s: v for s, v in r.oos_symbol_pnl.items() if v != 0}
    if not traded:
        return '<p style="margin:0;color:var(--ink2)">No symbol traded out-of-sample.</p>'
    winners = [v for v in traded.values() if v > 0]
    total_win = sum(winners)
    biggest = max(traded.values())
    share = biggest / total_win if total_win > 0 else float("nan")
    return (f'<p style="margin-top:0"><b>{len(winners)} of {len(traded)}</b> traded symbols were '
            f'profitable out-of-sample'
            + (f', and the single best contributed {_pct(share, 0)} of the gross profit.'
               if total_win > 0 else ', and none made money.')
            + f'</p><p style="margin-bottom:0;color:var(--ink2);font-size:13px">Held-out profit per '
            f'symbol is deliberately not listed. It is the one number on this page that could be acted '
            f'on with a single edit &mdash; dropping the markets that lost would build a universe '
            f'chosen on the window that is supposed to test it, and no gate here would catch that. '
            f'Breadth is what the checklist uses; concentration is shown because a strategy carried by '
            f'one market is fragile in a way a count hides.</p>')


def write_report(r: Gate1Result, out_dir: Path = REPORT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cls = REGISTRY[r.strategy]
    doc = inspect.getdoc(inspect.getmodule(cls)) or ""
    verdict = ('<span class="verdict pass">✓ PASSES Gate 1: eligible for paper trading</span>' if r.passed else
               '<span class="verdict fail">✕ FAILS Gate 1: not traded on paper</span>')
    checks = "".join(
        f'<tr><td class="{"pass" if c.passed else "fail"}">{"✓" if c.passed else "✕"}</td>'
        f'<td style="text-align:left">{html.escape(c.name)}</td><td style="text-align:left">{html.escape(c.detail)}</td></tr>'
        for c in r.checks)
    dd = lambda e: e / e.cummax() - 1  # noqa: E731
    equity_chart = line_chart([("Strategy", r.equity), ("Benchmark", r.bench_equity)], log=True, fmt="usd",
                              split=r.split_date, title="Growth of the sleeve's capital")
    dd_chart = line_chart([("Strategy", dd(r.equity)), ("Benchmark", dd(r.bench_equity))], log=False, fmt="pct",
                          split=r.split_date, title="Drop from previous high")
    # In-sample Sharpe only, one column, deliberately.
    #
    # This table used to carry an out-of-sample Sharpe per cell: eighteen held-out numbers laid out on
    # one page, in front of whoever is about to decide what to change next. PLAN.md line 418 records
    # what that cost in this project's own words -- "that was measured by peeking at out-of-sample and
    # cannot be acted on" -- and research_log.jsonl shows the same window read nineteen times. A window
    # read like that is not a test any more; it is a slow-motion training set.
    #
    # Selection is allowed to use in-sample results, so those stay: seeing a flat in-sample surface is
    # how you notice that which cell "wins" is noise, which is exactly what the PBO check reports on.
    # The chosen configuration's out-of-sample Sharpe is still on the page, in Results and in the
    # verdict, because it IS the verdict. What is gone is the surface -- the ability to read off which
    # OTHER settings would have scored better on held-out data, which is the one thing that cannot be
    # known without spending the window.
    #
    # Honest limit: this does not make peeking impossible, because re-running with different settings
    # still reveals one cell at a time. It makes it cost a trial in the ledger each time instead of
    # being free and invisible, which is the whole difference between one look and eighteen.
    grid_rows = "".join(
        f'<tr class="{"chosen" if row["params"] == r.chosen_params else ""}"><td>'
        f'{html.escape(", ".join(f"{k}={v}" for k, v in row["params"].items()))}'
        f'{" ← chosen" if row["params"] == r.chosen_params else ""}</td>'
        f'<td>{_num(row["is_sharpe"])}</td></tr>'
        for row in sorted(r.grid_table, key=lambda x: -x["is_sharpe"]))
    sym_summary = _oos_symbol_summary(r)
    caveats = [
        f"{r.trials} parameter combinations were tried in this run ({r.total_trials_ever} for {r.strategy} in total). "
        "The more variants are tried, the more likely a good result is luck.",
        "Symbols were picked today, knowing which assets did well (e.g. Bitcoin, the Nasdaq). "
        "That hindsight flatters results, especially before each asset became popular.",
        "Prices are daily bars from Yahoo Finance, dividend-adjusted. Live fills use Alpaca's prices.",
        "A backtest is not a forecast. Gate 2 (90+ days of paper trading) decides what survives.",
    ]
    if r.strategy == "S4":
        caveats.append("Gold history uses gold futures (GC=F) as a stand-in for spot gold. Forex P&L is approximated "
                       "in the quote currency.")
    body = f"""
<main>
<h1>{r.strategy}: {html.escape(cls.title or r.strategy)}</h1>
<p class="sub">{html.escape(', '.join(r.symbols))} · ${r.capital:,.0f} sleeve · benchmark: {html.escape(r.benchmark)}
 · generated {datetime.now():%Y-%m-%d %H:%M}</p>
{verdict}
<div class="card"><p style="margin-top:0">{html.escape(' '.join(doc.split(chr(10)+chr(10), 1)[-1].split()))}</p>
<p style="margin-bottom:0;color:var(--ink2)">Settings chosen on {r.eval_start.date()} – {r.split_date.date()} only
 ({html.escape(', '.join(f'{k}={v}' for k, v in r.chosen_params.items()))}), then tested on
 {r.split_date.date()} – {r.end.date()}, which they never saw.</p></div>
<h2>Gate 1 checklist</h2>
<div class="card"><table class="checks">{checks}</table></div>
<h2>Growth of ${r.capital:,.0f}</h2>
<div class="card">{equity_chart}</div>
<h2>Drop from previous high</h2>
<div class="card">{dd_chart}</div>
<h2>Results</h2>
<div class="card">{_metrics_rows(r)}<p style="color:var(--ink2);font-size:13px;margin-bottom:0">
Sharpe measures return per unit of risk (higher is better; above 1 is good). Bench = {html.escape(r.benchmark)}.
Walk-forward re-picks settings each year using only the previous {4} years.</p>{_yearly_table(r)}</div>
<h2>Every setting tried</h2>
<div class="card"><table><tr><th>Settings</th><th>In-sample Sharpe</th></tr>
{grid_rows}</table>
<p style="color:var(--ink2);font-size:13px;margin-bottom:0">In-sample only, on purpose. The
out-of-sample Sharpe of each setting used to be listed here, which put the whole held-out surface on
the page before the next decision was made &mdash; and this project&rsquo;s log shows that window read
nineteen times. Settings are chosen on the {r.eval_start.date()} &ndash; {r.split_date.date()} window
alone; the chosen configuration&rsquo;s out-of-sample result is in Results above. Reading another
cell&rsquo;s out-of-sample Sharpe means re-running Gate 1, which costs a trial in the ledger and
raises the bar for every future result.</p></div>
<h2>Out-of-sample breadth</h2>
<div class="card">{sym_summary}</div>
<h2>Read this before trusting the numbers</h2>
<div class="card"><ul>{''.join(f'<li>{html.escape(c)}</li>' for c in caveats)}</ul></div>
</main>"""
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{r.strategy} Gate 1 report</title>'
            f"<style>{CSS}</style></head><body>{body}<script>{HOVER_JS}</script></body></html>")
    path = out_dir / f"{r.strategy}-gate1-{datetime.now():%Y%m%d-%H%M}.html"
    path.write_text(page)
    return path
