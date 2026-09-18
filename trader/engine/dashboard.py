"""data/dashboard.html: a one-page view of the paper account, rewritten on every engine run.

Kept in data/ (git-ignored) because it shows account details. Open it in any browser.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd

from trader.config import Settings
from trader.journal.db import Journal
from trader.research.report import CSS, HOVER_JS, line_chart

if TYPE_CHECKING:
    from trader.engine.live import Engine


def _money(x: float | None) -> str:
    return "–" if x is None else f"${x:,.2f}"


def write_dashboard(settings: Settings, journal: Journal, engine: Engine) -> None:
    account = engine.broker.get_account()
    sleeves = engine._live_sleeves()  # noqa: SLF001 - same package

    rows = []
    for sleeve in sleeves:
        equity, invested = engine.sleeve_value(sleeve)
        adjusted = equity - journal.sleeve_haircut(sleeve.id)
        rows.append(f"<tr><td>{sleeve.id}</td><td style='text-align:left'>{html.escape(sleeve.name)}</td>"
                    f"<td>{_money(equity)}</td><td>{equity / sleeve.capital_usd - 1:+.2%}</td>"
                    f"<td>{adjusted / sleeve.capital_usd - 1:+.2%}</td><td>{invested / equity:.0%}</td></tr>")

    history = journal.sleeve_equity_history()
    chart = "<p class='sub'>The chart appears after a few engine runs.</p>"
    if len({h["ts"] for h in history}) >= 2:
        frame = pd.DataFrame([dict(h) for h in history])
        frame["ts"] = pd.to_datetime(frame["ts"])
        pivot = frame.pivot_table(index="ts", columns="sleeve_id", values="equity")
        pivot.index = pivot.index.tz_convert(None)  # chart axis uses plain UTC timestamps
        span_days = (pivot.index[-1] - pivot.index[0]).days
        series = [(sid, pivot[sid] / settings.sleeves[sid].capital_usd * 10_000) for sid in ("S1", "B0") if sid in pivot]
        if series:
            chart = line_chart(series, log=False, fmt="usd", split=None, title="Sleeve value per $10,000",
                               resample=None if span_days < 3 else "h" if span_days < 30 else "D")

    positions = []
    for s in journal.position_states():
        qty = journal.sleeve_holdings(s["sleeve_id"]).get(s["symbol"], 0.0)
        try:
            now = engine.broker.get_quote(s["symbol"], settings.asset_class(s["symbol"])).price  # type: ignore[arg-type]
        except Exception:
            now = None
        pnl = (now - s["entry_price"]) * qty if now else None
        positions.append(f"<tr><td>{s['sleeve_id']}</td><td style='text-align:left'>{html.escape(s['symbol'])}</td>"
                         f"<td>{qty:g}</td><td>{_money(s['entry_price'])}</td><td>{_money(now)}</td>"
                         f"<td>{_money(s['stop'])}</td><td>{_money(pnl)}</td><td>{s['bars_held']}</td></tr>")

    orders = [f"<tr><td style='text-align:left'>{r['created_at'][:16].replace('T', ' ')}</td><td>{r['sleeve_id']}</td>"
              f"<td>{r['side']}</td><td>{html.escape(r['symbol'])}</td><td>{r['order_type']}</td>"
              f"<td>{r['status']}</td><td style='text-align:left'>{html.escape(r['reason'] or '')}</td></tr>"
              for r in journal.recent_order_rows(15)]
    problems = [f"<li>{e['ts'][:16].replace('T', ' ')} · {html.escape(e['message'])}</li>"
                for e in journal.recent_events(40) if e["level"] in ("warning", "error", "critical")][:10]

    ks = engine.kill_switch
    body = f"""<main>
<h1>Personal Trader · paper</h1>
<p class="sub">Account {_money(account.equity)} · today {account.equity - account.last_equity:+,.2f} ·
kill switch <b class="{'fail' if ks.active else 'pass'}">{'ON' if ks.active else 'off'}</b> ·
last engine run {html.escape(journal.kv_get('last_run', 'never') or 'never')} · page written {datetime.now():%Y-%m-%d %H:%M}</p>
<h2>Sleeves</h2>
<div class="card scroll"><table><tr><th>Sleeve</th><th style="text-align:left">Strategy</th><th>Value</th><th>Return</th>
<th>After est. costs</th><th>Invested</th></tr>{''.join(rows)}</table></div>
<h2>Strategy vs buy-and-hold</h2>
<div class="card">{chart}</div>
<h2>Open positions</h2>
<div class="card scroll"><table><tr><th>Sleeve</th><th style="text-align:left">Symbol</th><th>Qty</th><th>Entry</th>
<th>Now</th><th>Stop</th><th>P&amp;L</th><th>Days held</th></tr>{''.join(positions) or '<tr><td colspan=8>none</td></tr>'}</table></div>
<h2>Recent orders</h2>
<div class="card scroll"><table><tr><th style="text-align:left">Time (UTC)</th><th>Sleeve</th><th>Side</th><th>Symbol</th>
<th>Type</th><th>Status</th><th style="text-align:left">Reason</th></tr>{''.join(orders)}</table></div>
<h2>Warnings</h2>
<div class="card"><ul>{''.join(problems) or '<li>none</li>'}</ul></div>
</main>"""
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">'
            f"<title>Trader dashboard</title><style>{CSS}</style></head><body>{body}<script>{HOVER_JS}</script>"
            f"</body></html>")
    path = settings.root / "data" / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
