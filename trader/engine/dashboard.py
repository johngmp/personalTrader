"""data/dashboard.html: the paper-trading dashboard, rewritten on every engine run (every 15 minutes).

Kept in data/ (git-ignored) because it shows account details. Open it in any browser; it reloads itself.
Tables are rendered here in Python; the charts are drawn in the browser so the time-range buttons and
hover read-outs work without a server.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd

from trader.config import Settings
from trader.journal.db import Journal, trading_day_start
from trader.engine.explain import GLOSSARY, headline
from trader.models import AssetClass, format_price
from trader.money import format_aud, stored_rate
from trader.journal.vault import Trip, round_trips

if TYPE_CHECKING:
    from trader.engine.live import Engine

# Categorical colours validated on the dark surface (entity keeps its colour everywhere).
SERIES_COLORS = {"alpaca": "#c98500", "oanda": "#9085e9", "S1": "#3987e5", "B0": "#d95926", "S4": "#199e70"}
SHADOW_COLOR = "#9085e9"  # shadow strategies get their own chart (a 4th colour in one chart fails the CVD check)
BROKER_LABELS = {"alpaca": "Alpaca (stocks & crypto)", "oanda": "OANDA (forex)", "shadow": "Shadow (no orders)",
                 "shadow_fx": "Shadow FX (no orders)"}


# Inline so the page stays a single self-contained file with no icon font to fetch.
def _svg(body: str) -> str:
    return (f'<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


ICON_SEARCH = _svg('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>')
ICON_HOME = _svg('<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>')
ICON_CHART = _svg('<path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 16l3.5-4.5 3 2.5L20 7"/>')
ICON_FX = _svg('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/>'
               '<path d="M12 3.5c2.4 2.3 3.6 5.2 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.2-3.6-8.5S9.6 5.8 12 3.5z"/>')
ICON_LIST = _svg('<path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/>'
                 '<circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>')
ICON_BELL = _svg('<path d="M18 16V11a6 6 0 10-12 0v5l-1.5 2.5h15L18 16z"/><path d="M10 20.5a2.2 2.2 0 004 0"/>')
ICON_PANEL = _svg('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>')
ICON_FLASK = _svg('<path d="M9 3v6.2L4.4 17A2 2 0 006.1 20h11.8a2 2 0 001.7-3L15 9.2V3"/>'
                  '<path d="M8 3h8"/><path d="M7.4 14h9.2"/>')


def is_shadow(broker: str | None) -> bool:
    """Every shadow broker, whatever prices it uses: `shadow` (Alpaca) and `shadow_fx` (Yahoo)."""
    return bool(broker) and broker.startswith("shadow")


def _money(x: float | None, signed: bool = False, cents: bool = True) -> str:
    if x is None:
        return "–"
    fmt = f"{'+' if signed else ''},.{2 if cents else 0}f"
    s = format(x, fmt)
    return s.replace("+", "+$").replace("-", "−$") if s[0] in "+-" else f"${s}"


def _pct(x: float | None, signed: bool = True, digits: int = 2) -> str:
    if x is None:
        return "–"
    return f"{x * 100:+.{digits}f}%".replace("-", "−") if signed else f"{x * 100:.{digits}f}%"


def _tone(x: float | None) -> str:
    return "" if not x else ("up" if x > 0 else "down")


def _downsample(points: list[tuple[str, float]]) -> list[list]:
    """At most one point per hour after 3 days of history, one per day after 60 days."""
    if len(points) < 2:
        return [[t, v] for t, v in points]
    s = pd.Series([v for _, v in points], index=pd.to_datetime([t for t, _ in points], utc=True))
    span = (s.index[-1] - s.index[0]).days
    if span > 60:
        s = s.resample("D").last().dropna()
    elif span > 3:
        s = s.resample("h").last().dropna()
    return [[ts.isoformat(timespec="minutes"), round(float(v), 2)] for ts, v in s.items()]


def collect(settings: Settings, journal: Journal, engines: list[Engine]) -> dict:
    """Everything the dashboard shows, across all brokers, as plain data (also handy for tests)."""
    now = datetime.now(timezone.utc)
    risk = settings.risk.account
    kill_switch = engines[0].kill_switch
    accounts, account_series = [], {}
    for engine in (e for e in engines if not is_shadow(e.broker.name)):  # shadow money isn't real money
        acct = engine.executor.account()
        snaps = journal.conn.execute("SELECT ts, equity FROM equity_snapshots WHERE broker=? ORDER BY ts",
                                     (engine.broker.name,)).fetchall()
        start = snaps[0]["equity"] if snaps else acct.equity
        peak = max([s["equity"] for s in snaps] + [acct.equity])
        day_pct = acct.equity / acct.last_equity - 1 if acct.last_equity else 0.0
        dd = acct.equity / peak - 1 if peak else 0.0
        accounts.append({"broker": engine.broker.name, "label": BROKER_LABELS.get(engine.broker.name, engine.broker.name),
                         "account": acct, "start": start, "day_pct": day_pct, "drawdown": dd,
                         "day_used": max(0.0, -day_pct * 100 / risk.max_daily_loss_pct),
                         "dd_used": max(0.0, -dd * 100 / risk.max_drawdown_halt_pct)})
        account_series[engine.broker.name] = _downsample([(s["ts"], s["equity"]) for s in snaps] +
                                                         [(now.isoformat(), acct.equity)])
    worst = max(accounts, key=lambda a: max(a["day_used"], a["dd_used"]))
    day_used, dd_used = worst["day_used"], worst["dd_used"]
    if kill_switch.active or day_used >= 1 or dd_used >= 1:
        risk_level = "Trading halted" if kill_switch.active else "Limit reached"
    else:
        risk_level = "Low risk" if max(day_used, dd_used) < 0.5 else "Elevated risk"
    total = sum(a["account"].equity for a in accounts)
    start_total = sum(a["start"] for a in accounts)
    last_total = sum(a["account"].last_equity or a["account"].equity for a in accounts)

    symbol_engine = {s: e for e in engines for sl in e._live_sleeves() for s in sl.symbols}  # noqa: SLF001
    prices: dict[str, float] = {}

    def price(symbol: str) -> float | None:
        if symbol not in prices:
            engine = symbol_engine.get(symbol, engines[0])
            try:
                prices[symbol] = engine.broker.get_quote(symbol, settings.asset_class(symbol)).price  # type: ignore[arg-type]
            except Exception:
                return None
        return prices[symbol]

    sleeves, trips = [], []
    b0_return = None
    for engine, sleeve in ((e, sl) for e in engines for sl in e._live_sleeves()):  # noqa: SLF001
        equity, invested = engine.sleeve_value(sleeve)
        haircut = journal.sleeve_haircut(sleeve.id)
        sleeve_trips = round_trips(journal, sleeve.id)
        trips += sleeve_trips
        closed = [t for t in sleeve_trips if not t.is_open]
        ret = equity / sleeve.capital_usd - 1
        if sleeve.id == "B0":
            b0_return = ret
        sleeves.append({"id": sleeve.id, "name": sleeve.name, "equity": equity, "return": ret,
                        "adjusted_return": (equity - haircut) / sleeve.capital_usd - 1,
                        "invested": invested / equity if equity else 0.0, "capital": sleeve.capital_usd,
                        "open": sum(1 for t in sleeve_trips if t.is_open), "closed": len(closed),
                        "benchmark": sleeve.id == "B0", "broker": sleeve.broker})
    for s in sleeves:
        s["vs_b0"] = None if s["benchmark"] or b0_return is None else s["return"] - b0_return

    strategy_trips = [t for t in trips if t.sleeve_id != "B0"]
    closed = [t for t in strategy_trips if not t.is_open]
    wins = [t.pnl() for t in closed if t.pnl() > 0]
    losses = [t.pnl() for t in closed if t.pnl() <= 0]
    rs = [r for r in (t.r_multiple() for t in closed) if r is not None]
    open_trips = [t for t in trips if t.is_open]
    realized = sum(t.pnl() for t in trips if not t.is_open)
    unrealized = sum(t.pnl(price(t.symbol)) for t in open_trips)
    fees = sum(t.fees for t in trips)
    fill_days = {row["ts"][:10] for row in journal.fills() if row["sleeve_id"] not in ("TEST",)}

    history = journal.sleeve_equity_history()
    sleeve_series: dict[str, list[tuple[str, float]]] = {}
    for h in history:
        cap = settings.sleeves[h["sleeve_id"]].capital_usd if h["sleeve_id"] in settings.sleeves else 10_000
        sleeve_series.setdefault(h["sleeve_id"], []).append((h["ts"], h["equity"] / cap * 10_000))

    return {
        "now": now, "accounts": accounts, "total": total, "start_equity": start_total, "total_pnl": total - start_total,
        "day_pnl": total - last_total, "day_pct": total / last_total - 1 if last_total else 0.0,
        "drawdown": worst["drawdown"], "worst_broker": worst["label"],
        "day_used": day_used, "dd_used": dd_used, "risk_level": risk_level,
        "kill_switch": kill_switch.active, "kill_reason": kill_switch.reason,
        "last_run": journal.kv_get("last_run"), "orders_today": journal.orders_submitted_since(trading_day_start(now)),
        "max_orders": risk.max_orders_per_day, "realized": realized, "unrealized": unrealized, "fees": fees,
        "kpis": {"closed": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
                 "avg_win": sum(wins) / len(wins) if wins else None,
                 "avg_loss": sum(losses) / len(losses) if losses else None,
                 "avg_r": sum(rs) / len(rs) if rs else None,
                 "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
                 "trading_days": len(fill_days), "open": len(open_trips)},
        "sleeves": sleeves, "trips": sorted(trips, key=lambda t: t.entry_ts, reverse=True), "price": price,
        "states": {(s["sleeve_id"], s["symbol"]): s for s in journal.position_states()},
        "watchlist": watchlist(settings, price),
        "orders": journal.recent_order_rows(25),
        "rate": stored_rate(journal),
        "fx": fx_snapshot(settings, journal, engines),
        "research": latest_research(settings),
        "blocked": blocked_orders(journal),
        "warnings": [e for e in journal.recent_events(60) if e["level"] in ("warning", "error", "critical")][:12],
        "charts": {
            "account": account_series,
            "sleeves": {sid: _downsample(pts) for sid, pts in sleeve_series.items() if sid in SERIES_COLORS},
            "shadow": {sid: _downsample(pts) for sid, pts in sleeve_series.items()
                       if settings.sleeves.get(sid) is not None and is_shadow(settings.sleeves[sid].broker)},
        },
    }


FX_CHART_DAYS = 400


def watchlist(settings: Settings, price) -> list[dict]:
    """Every traded symbol with its live price and the day's move, for the sidebar.

    The day's change comes from the cached daily history rather than a second quote: the previous close
    is already on disk, and asking a broker twice for the same symbol is how the page got slow before.
    """
    from trader.data.history import load_history

    rows = []
    for symbol in sorted({x for s in settings.sleeves.values() if s.enabled for x in s.symbols}):
        now_price = price(symbol)
        if now_price is None:
            continue
        change = None
        try:
            closes = load_history(symbol)["close"]
            if len(closes) >= 2:
                change = now_price / float(closes.iloc[-2]) - 1
        except Exception:
            change = None
        rows.append({"symbol": symbol, "price": now_price, "change": change,
                     "asset_class": settings.asset_class(symbol)})
    return rows


def fx_series(symbols: list[str], live: dict[str, float] | None = None) -> dict[str, list]:
    """Daily closes per pair, with today's live quote appended as the final point.

    Read from the same cached CSVs the backtests use, so drawing a chart costs no network call -- only
    the live quote does, and the page already fetches that. Daily bars, not ticks: this strategy decides
    once a day, and a minute chart would imply a precision the system does not act on.
    """
    from trader.data.history import load_history

    out: dict[str, list] = {}
    for symbol in symbols:
        try:
            closes = load_history(symbol)["close"].tail(FX_CHART_DAYS)
        except Exception:
            continue
        points = [[ts.isoformat(), round(float(v), 6)] for ts, v in closes.items()]
        now_price = (live or {}).get(symbol)
        if now_price and points:
            points.append([datetime.now(timezone.utc).isoformat(timespec="minutes"), round(now_price, 6)])
        if points:
            out[symbol] = points
    return out


def fx_snapshot(settings: Settings, journal: Journal, engines: list[Engine]) -> dict:
    """Forex: current rates, what the FX sleeve holds, and its Gate 1 verdict.

    The verdict is shown next to the rates deliberately. An FX panel that lists prices and positions
    without saying the strategy failed its own test reads as an invitation.
    """
    fx_sleeves = [s for s in settings.sleeves.values()
                  if s.enabled and any(settings.asset_class(x) == AssetClass.FOREX for x in s.symbols)]
    if not fx_sleeves:
        return {}
    engine = next((e for e in engines if e.broker.name == fx_sleeves[0].broker), None)
    symbols, rows = sorted({x for s in fx_sleeves for x in s.symbols}), []
    held = {}
    for sleeve in fx_sleeves:
        held.update(journal.sleeve_holdings(sleeve.id))
    for symbol in symbols:
        quote = None
        if engine is not None:
            try:
                quote = engine.broker.get_quote(symbol, AssetClass.FOREX)
            except Exception:
                quote = None
        state = journal.position_state(fx_sleeves[0].id, symbol)
        rows.append({"symbol": symbol, "price": quote.price if quote else None,
                     "ts": quote.ts if quote else None, "qty": held.get(symbol, 0.0),
                     "stop": (state or {})["stop"] if state else None})
    verdict = next((r for r in latest_research(settings) if r.get("strategy") in {s.strategy or s.id for s in fx_sleeves}), None)
    clock = None
    try:
        from trader.brokers.oanda import fx_clock

        clock = fx_clock(datetime.now(timezone.utc))
    except Exception:
        clock = None
    live = {r["symbol"]: r["price"] for r in rows if r["price"]}
    return {"rows": rows, "sleeves": [s.id for s in fx_sleeves], "verdict": verdict,
            "is_open": getattr(clock, "is_open", None), "series": fx_series(symbols, live)}


def latest_research(settings: Settings) -> list[dict]:
    """The most recent Gate 1 verdict per strategy, newest first.

    This is the evidence a sleeve is trading on. Without it the dashboard shows what a strategy IS doing
    and never whether it earned the right to -- which is how S4 nearly got switched on.
    """
    log = settings.root / "reports" / "research_log.jsonl"
    if not log.exists():
        return []
    newest: dict[str, dict] = {}
    for line in log.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a half-written line is not worth failing a page render over
        code = row.get("strategy")
        if code and row.get("ts", "") >= newest.get(code, {}).get("ts", ""):
            newest[code] = row
    return sorted(newest.values(), key=lambda r: r.get("ts", ""), reverse=True)


def blocked_orders(journal: Journal, limit: int = 15) -> list[dict]:
    """Orders the risk gate refused, with the reason. Invisible everywhere else on the dashboard."""
    rows = journal.conn.execute(
        "SELECT o.created_at, o.sleeve_id, o.side, o.symbol, o.qty, o.notional, r.summary "
        "FROM risk_decisions r JOIN orders o ON o.client_order_id = r.client_order_id "
        "WHERE r.approved = 0 ORDER BY r.ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _research_card(r: dict, sleeves: list[dict]) -> str:
    """One Gate 1 verdict, and -- the part that matters -- whether that sleeve is trading anyway."""
    code = r.get("strategy", "?")
    passed = r.get("verdict") == "PASS"
    live = next((s for s in sleeves if s["id"] == code), None)
    trading_for_real = live is not None and not is_shadow(live.get("broker"))
    oos, bench = r.get("oos_sharpe"), r.get("benchmark_oos_sharpe")
    failed = [c for c in (r.get("failed_checks") or [])]
    # A failing strategy that is live with real money is the one thing this page exists to shout about.
    alarm = failed and trading_for_real
    return f"""<div class="card sleeve"><div class="row">
<span class="dot" style="background:{'#199e70' if passed else '#d95926'}"></span><b>{html.escape(code)}</b>
<span class="pill {'open' if passed else 'closed'}">{'PASS' if passed else 'FAIL'}</span>
{'<span class="pill closed">trading anyway</span>' if alarm else ''}</div>
<div class="big {'up' if passed else 'down'}">{'–' if oos is None else f'{oos:+.2f}'}</div>
<div class="row gap"><span class="subtle">out-of-sample Sharpe vs benchmark
{'–' if bench is None else f'{bench:+.2f}'}</span></div>
<div class="subtle">walk-forward {('%+.2f' % r['wf_sharpe']) if r.get('wf_sharpe') is not None else '–'}
 · {r.get('trials', '?')} settings tried · {html.escape(str(r.get('ts', ''))[:10])}</div>
<div class="subtle">{html.escape(', '.join(r.get('symbols') or []))}</div>
{('<div class="subtle down">fails: ' + html.escape('; '.join(failed)) + '</div>') if failed else ''}
</div>"""


def _explain(label: str) -> str:
    """A `title` attribute carrying what the term means and what a good value looks like."""
    for term, (meaning, good) in GLOSSARY.items():
        if label.startswith(term):
            return html.escape(f"{meaning}\n\n{good}".strip())
    return ""


def _tile(label: str, value: str, sub: str = "", tone: str = "") -> str:
    tip = _explain(label)
    cls = "label help" if tip else "label"
    attr = f' title="{tip}"' if tip else ""
    return (f'<div class="tile"><div class="{cls}"{attr}>{html.escape(label)}</div>'
            f'<div class="value {tone}">{value}</div><div class="subtle">{sub}</div></div>')


def _trip_row(t: Trip, price, state) -> str:
    now = price(t.symbol) if t.is_open else None
    pnl = t.pnl(now)
    base = t.buy_cost or 1
    status = '<span class="pill open">Open</span>' if t.is_open else '<span class="pill closed">Closed</span>'
    stop = state["stop"] if (t.is_open and state is not None) else t.initial_stop
    close_time = "–" if t.is_open else t.exit_ts[:16].replace("T", " ")  # type: ignore[index]
    close_px = _money(now) if t.is_open else _money(t.exit_price)
    return (f"<tr><td class='mono'>{t.trade_id[-8:]}</td><td>{t.sleeve_id}</td><td class='sym'>{html.escape(t.symbol)}</td>"
            f"<td><span class='pill buy'>Long</span></td><td>{t.entry_ts[:16].replace('T', ' ')}</td>"
            f"<td class='num'>{_money(t.entry_price)}</td><td>{close_time}</td><td class='num'>{close_px}</td>"
            f"<td class='num'>{t.buy_qty:g}</td><td class='num'>{_money(stop)}</td><td>{status}</td>"
            f"<td class='num {_tone(pnl)}'>{_money(pnl, signed=True)}<span class='subtle'> {_pct(pnl / base)}</span></td>"
            f"<td class='why'>{html.escape(t.exit_reason or t.entry_reason or '')}</td></tr>")


def render(d: dict) -> str:
    k = d["kpis"]
    cash = sum(x["account"].cash for x in d["accounts"])
    tiles = "".join([
        _tile("Closed trades", str(k["closed"]), f"{k['open']} open now"),
        _tile("Win rate", _pct(k["win_rate"], signed=False, digits=1) if k["win_rate"] is not None else "–",
              "strategy trades, not the benchmark"),
        _tile("Average winning trade", _money(k["avg_win"]), "", "up" if k["avg_win"] else ""),
        _tile("Average losing trade", _money(k["avg_loss"]), "", "down" if k["avg_loss"] else ""),
        _tile("Average R", f"{k['avg_r']:.2f}" if k["avg_r"] is not None else "–", "profit ÷ planned risk"),
        _tile("Profit factor", f"{k['profit_factor']:.2f}" if k["profit_factor"] else "–", "wins ÷ losses"),
        _tile("Trading days", str(k["trading_days"]), "days with fills"),
        _tile("Fees paid", _money(d["fees"]), "charged by Alpaca"),
    ])
    sleeve_cards = "".join(
        f"""<div class="card sleeve"><div class="row"><span class="dot" style="background:{SHADOW_COLOR if is_shadow(s.get('broker')) else SERIES_COLORS.get(s['id'], '#898781')}"></span>
<b>{s['id']}</b><span class="subtle">{html.escape(s['name'])}</span>{'<span class="pill closed">benchmark</span>' if s['benchmark'] else ''}{'<span class="pill open">shadow · no orders</span>' if is_shadow(s.get('broker')) else ''}</div>
<div class="big">{_money(s['equity'])}</div>
<div class="row gap"><span class="{_tone(s['return'])}">{_pct(s['return'])}</span>
<span class="subtle">after est. costs {_pct(s['adjusted_return'])}</span></div>
<div class="row gap subtle">{s['invested']:.0%} invested · {s['open']} open · {s['closed']} closed
{'' if s['vs_b0'] is None else f"· <span class='{_tone(s['vs_b0'])}'>{_pct(s['vs_b0'])} vs B0</span>"}</div></div>"""
        for s in d["sleeves"])
    open_rows = "".join(_trip_row(t, d["price"], d["states"].get((t.sleeve_id, t.symbol))) for t in d["trips"] if t.is_open)
    all_rows = "".join(_trip_row(t, d["price"], d["states"].get((t.sleeve_id, t.symbol))) for t in d["trips"])
    trade_head = ("<tr><th>Ticket</th><th>Sleeve</th><th>Symbol</th><th>Side</th><th>Open time (UTC)</th><th>Open price</th>"
                  "<th>Close time</th><th>Close / now</th><th>Qty</th><th>Stop</th><th>Status</th><th>Net P/L</th>"
                  "<th>Reason</th></tr>")
    order_rows = "".join(
        f"<tr><td>{r['created_at'][:16].replace('T', ' ')}</td><td>{r['sleeve_id']}</td>"
        f"<td><span class='pill {'buy' if r['side'] == 'buy' else 'sell'}'>{r['side'].title()}</span></td>"
        f"<td class='sym'>{html.escape(r['symbol'])}</td><td>{r['order_type'].replace('_', ' ')}</td>"
        f"<td class='num'>{format(r['qty'], 'g') if r['qty'] else _money(r['notional'])}</td>"
        f"<td class='num'>{_money(r['stop_price']) if r['stop_price'] else '–'}</td><td>{html.escape(r['status'])}</td>"
        f"<td class='why'>{html.escape(r['reason'] or '')}</td></tr>" for r in d["orders"])
    warn_rows = "".join(f"<li><span class='subtle'>{w['ts'][:16].replace('T', ' ')}</span> {html.escape(w['message'])}</li>"
                        for w in d["warnings"]) or "<li class='subtle'>No warnings.</li>"
    research_cards = "".join(_research_card(r, d["sleeves"]) for r in d["research"]) or \
        "<p class='subtle'>No Gate 1 results yet. Run <code>trader gate1 all</code>.</p>"
    blocked_rows = "".join(
        f"<tr><td class='subtle'>{b['created_at'][:19].replace('T', ' ')}</td><td>{html.escape(b['sleeve_id'])}</td>"
        f"<td>{html.escape(b['side'])}</td><td>{html.escape(b['symbol'])}</td>"
        f"<td class='num'>{(format(b['qty'], 'g') if b['qty'] else _money(b['notional']))}</td>"
        f"<td class='why'>{html.escape(b['summary'] or '')}</td></tr>" for b in d["blocked"]) or \
        "<tr><td colspan=6 class='subtle'>Nothing has been blocked.</td></tr>"
    # USD is the currency these assets trade in and the one strategies are judged in; AUD is what the
    # account holder actually spends. Shown alongside, never instead.
    aud_total = format_aud(d["total"], d["rate"])
    aud_line = (f'<div class="subtle">{aud_total} at {1 / d["rate"].aud_per_usd:.4f} AUD/USD '
                f'({format_aud(d["total_pnl"], d["rate"], signed=True)} since the start)</div>'
                if aud_total else "")
    watch = d.get("watchlist") or []
    n_fx = sum(1 for w in watch if w["asset_class"] == AssetClass.FOREX)
    n_equity = len(watch) - n_fx
    n_trades = len([t for t in d["trips"] if not t.is_open])
    warn_dot = '<i class="dot-warn"></i>' if d["warnings"] else ""
    watch_items = "".join(
        f'<div class="wrow"><span class="wsym">{html.escape(w["symbol"])}</span>'
        f'<span class="wpx">{format_price(w["price"], w["asset_class"])}</span>'
        + (f'<span class="{"up" if w["change"] >= 0 else "down"}">{w["change"] * 100:+.2f}%</span>'
           if w["change"] is not None else '<span class="subtle">–</span>')
        + "</div>" for w in watch)
    head_lines = "".join(f'<li class="hl-{tone}">{html.escape(text)}</li>' for tone, text in headline(d))
    fx = d.get("fx") or {}
    if fx.get("rows"):
        v = fx.get("verdict") or {}
        shut = "" if fx.get("is_open") else '<span class="pill closed">market shut</span>'
        fx_rows = "".join(
            f"<tr><td>{html.escape(r['symbol'])}</td>"
            f"<td class='num'>{format_price(r['price'], AssetClass.FOREX) if r['price'] else '–'}</td>"
            f"<td class='num'>{(format(r['qty'], 'g') if r['qty'] else '–')}</td>"
            f"<td class='num'>{format_price(r['stop'], AssetClass.FOREX) if r.get('stop') else '–'}</td></tr>"
            for r in fx["rows"])
        verdict_note = ("" if not v else
            f'<p class="subtle">Gate 1 verdict for these rules: <b class="{"up" if v.get("verdict") == "PASS" else "down"}">'
            f'{html.escape(str(v.get("verdict")))}</b> — out-of-sample Sharpe '
            f'{v.get("oos_sharpe")} against cash. '
            + ("" if v.get("verdict") == "PASS" else
               "A negative number means these rules lost to leaving the money in T-bills, so this sleeve "
               "sends nothing to any broker and its signals are not advice to act on.") + "</p>")
        pair_items = ""
        for i, r in enumerate(fx["rows"]):
            series = (fx.get("series") or {}).get(r["symbol"]) or []
            change = ""
            if len(series) > 2:
                first, last = series[-22][1], series[-1][1]     # ~one month of trading days
                pct = (last / first - 1) * 100 if first else 0.0
                change = (f'<span class="{"up" if pct >= 0 else "down"}">{pct:+.2f}%</span>'
                          f'<span class="subtle"> 1M</span>')
            held = f'<span class="pill open">holding {r["qty"]:g}</span>' if r["qty"] else ""
            pair_items += (
                f'<button class="pair{" on" if i == 0 else ""}" data-pair="{html.escape(r["symbol"])}">'
                f'<div class="row"><b>{html.escape(r["symbol"])}</b><span class="spacer"></span>{held}</div>'
                f'<div class="row gap"><span class="px">'
                f'{format_price(r["price"], AssetClass.FOREX) if r["price"] else "–"}</span>{change}</div>'
                f'<div class="subtle">{"stop " + format_price(r["stop"], AssetClass.FOREX) if r.get("stop") else "no position"}</div>'
                f'</button>')
        v = fx.get("verdict") or {}
        verdict_note = ("" if not v else
            f'<p class="subtle">Gate 1 verdict for these rules: <b class="{"up" if v.get("verdict") == "PASS" else "down"}">'
            f'{html.escape(str(v.get("verdict")))}</b> — out-of-sample Sharpe {v.get("oos_sharpe")} against cash. '
            + ("" if v.get("verdict") == "PASS" else
               "A negative number means these rules lost to leaving the money in T-bills, so this sleeve "
               "sends nothing to any broker and its signals are not advice to act on.") + "</p>")
        fx_card = f"""<h2>Forex {shut}</h2>
<p class="subtle">{html.escape(", ".join(fx["sleeves"]))} trades these on live prices with
<b>no orders sent anywhere</b>. Daily closes with the latest quote on the end; the feed is free and lags
a few minutes.</p>
<div class="fxlayout">
<div class="pairlist">{pair_items}</div>
<div class="card chartcard fxchart"><div class="row"><div>
<div class="label" id="fx-title">&nbsp;</div><div class="subtle" id="fx-sub">&nbsp;</div></div>
<div class="spacer"></div><div class="ranges" data-for="fx">
<button data-r="1M">1M</button><button data-r="3M">3M</button><button data-r="All" class="on">All</button>
</div></div>
<div class="chart" id="fx-chart" aria-label="Exchange rate over time"></div></div>
</div>{verdict_note}"""
    else:
        fx_card = ""
    risk_class = {"Low risk": "ok", "Elevated risk": "warn"}.get(d["risk_level"], "bad")
    total_base = d["start_equity"] or 1
    chart_data = json.dumps({"account": d["charts"]["account"], "sleeves": d["charts"]["sleeves"],
                             "shadow": d["charts"]["shadow"], "colors": {**SERIES_COLORS, "shadow": SHADOW_COLOR},
                             "labels": BROKER_LABELS, "fx": (d.get("fx") or {}).get("series", {})})
    shadow_ids = sorted(d["charts"]["shadow"])
    shadow_card = "" if not shadow_ids else f"""<div class="card chartcard"><div class="row"><div>
<div class="label">Shadow tests · live prices, no orders sent</div>
<div class="subtle">{html.escape(", ".join(shadow_ids))}: value of each $10,000 strategy. Research only; not in the account totals.</div>
</div><div class="spacer"></div><div class="ranges" data-for="shadow"><button data-r="1W">1W</button><button data-r="1M">1M</button>
<button data-r="All" class="on">All</button></div></div>
<div class="chart" id="shadow-chart" aria-label="Shadow strategy values over time"></div></div>"""
    breakdown = " · ".join(f"<span><i class='sw' style='background:{SERIES_COLORS.get(x['broker'], '#898781')}'></i>"
                           f"{html.escape(x['label'])} {_money(x['account'].equity)}</span>" for x in d["accounts"])
    legend_sleeves = "".join(f"<span><i style='background:{SERIES_COLORS[s['id']]}'></i>{s['id']} {html.escape(s['name'])}</span>"
                             for s in d["sleeves"] if s["id"] in SERIES_COLORS)
    last_run = (d["last_run"] or "never").replace("T", " ")[:16]

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="pt-refresh" content="300">
<title>Trader dashboard</title><style>{CSS}</style></head><body>
<div id="searchbar"><input id="searchbox" type="search" autocomplete="off"
 placeholder="Search trades, orders, symbols or reasons&hellip;  (Esc to close)"
 aria-label="Search the tables on this page"></div>
<header><div class="brand">PT</div><div><h1>Dashboard</h1><div class="subtle">{d['now'].astimezone():%A, %d %B %Y · %H:%M}</div></div>
<div class="spacer"></div>
<div class="account"><div><b>Paper trading</b> <span class="pill {'bad' if d['kill_switch'] else 'ok'}">{'Halted' if d['kill_switch'] else 'Active'}</span>
<span class="pill closed">Auto</span></div><div class="subtle">Last engine run {html.escape(last_run)} UTC · orders today {d['orders_today']}/{d['max_orders']}</div></div>
<button id="claude-toggle" aria-expanded="false" aria-controls="claude-panel" title="Ask Claude (⌘J)">Ask Claude</button></header>
<nav class="rail" role="tablist" aria-label="Sections">
<div class="rail-top"><div class="rail-brand">PT</div><span class="rail-name">Personal Trader</span>
<span class="spacer"></span>
<button id="rail-collapse" title="Collapse sidebar" aria-label="Collapse sidebar">{ICON_PANEL}</button></div>
<button class="rail-btn" id="rail-search" title="Search (/)" aria-label="Search">{ICON_SEARCH}<span>Search</span></button>
<div class="rail-group">Main</div>
<button class="rail-btn tab active" data-tab="overview" role="tab" title="Overview" aria-label="Overview">{ICON_HOME}<span>Overview</span></button>
<button class="rail-btn tab" data-tab="stocks" role="tab" title="Stocks &amp; crypto" aria-label="Stocks and crypto">{ICON_CHART}<span>Stocks &amp; crypto</span><i class="count">{n_equity}</i></button>
<button class="rail-btn tab" data-tab="forex" role="tab" title="Forex" aria-label="Forex">{ICON_FX}<span>Forex</span><i class="count">{n_fx}</i></button>
<div class="rail-group">Activity</div>
<button class="rail-btn tab" data-tab="trades" role="tab" title="Trading history" aria-label="Trading history">{ICON_LIST}<span>History</span><i class="count">{n_trades}</i></button>
<button class="rail-btn tab" data-tab="orders" role="tab" title="Orders &amp; warnings" aria-label="Orders and warnings">{ICON_BELL}<span>Orders</span>{warn_dot}</button>
<div class="rail-group">Research</div>
<button class="rail-btn tab" data-tab="evidence" role="tab" title="Does each strategy work?" aria-label="Evidence">{ICON_FLASK}<span>Evidence</span></button>
<div class="rail-group">Watchlist</div>
<div class="watchlist">{watch_items}</div>
</nav>
<main>
<section data-panel="overview">
<ul class="headline">{head_lines}</ul>
<div class="grid top">
<div class="card chartcard"><div class="row"><div><div class="label">Account balance · all paper accounts</div>
<div class="hero">{_money(d['total'])}</div>
<div class="{_tone(d['day_pnl'])}">{_money(d['day_pnl'], signed=True)} ({_pct(d['day_pct'])}) today</div>
{aud_line}</div>
<div class="spacer"></div><div class="ranges" data-for="account">
<button data-r="1D">1D</button><button data-r="1W">1W</button><button data-r="1M">1M</button><button data-r="3M">3M</button>
<button data-r="YTD">YTD</button><button data-r="All" class="on">All</button></div></div>
<div class="breakdown">{breakdown}</div>
<div class="chart" id="account-chart" aria-label="Account balance over time"></div></div>
<div class="card side"><div class="row"><div><div class="label">Profit / loss</div>
<div class="big {_tone(d['total_pnl'])}">{_money(d['total_pnl'], signed=True)}</div>
<div class="subtle">{_pct(d['total_pnl'] / total_base)} since the start</div></div><div class="spacer"></div>
<span class="pill {risk_class}">{html.escape(d['risk_level'])}</span></div>
<div class="split"><div><div class="label">Realised</div><div class="{_tone(d['realized'])}">{_money(d['realized'], signed=True)}</div></div>
<div><div class="label">Unrealised</div><div class="{_tone(d['unrealized'])}">{_money(d['unrealized'], signed=True)}</div></div></div>
<div class="subtle">Limits are per account; meters show the closest one ({html.escape(d['worst_broker'])})</div>
<div class="meter"><div class="label">Daily loss limit used ({-d['day_used'] * float(d['_limits_day']):+.2f}% of −{d['_limits_day']}%)</div>
<div class="track"><div class="fill {'bad' if d['day_used'] >= 1 else 'warn' if d['day_used'] >= .5 else ''}" style="width:{min(d['day_used'], 1) * 100:.0f}%"></div></div></div>
<div class="meter"><div class="label">Drawdown limit used ({d['drawdown'] * 100:.2f}% of −{d['_limits_dd']}%)</div>
<div class="track"><div class="fill {'bad' if d['dd_used'] >= 1 else 'warn' if d['dd_used'] >= .5 else ''}" style="width:{min(d['dd_used'], 1) * 100:.0f}%"></div></div></div>
<dl><div><dt>Equity</dt><dd>{_money(d['total'])}</dd></div><div><dt>Cash</dt><dd>{_money(cash)}</dd></div>
<div><dt>Account size</dt><dd>{_money(d['start_equity'])}</dd></div><div><dt>Accounts</dt><dd>{len(d['accounts'])}</dd></div></dl>
{f"<p class='bad'>Kill switch: {html.escape(d['kill_reason'])}</p>" if d['kill_switch'] else ''}</div></div>
<h2>Trading objectives</h2><div class="tiles">{tiles}</div>
</section>
<section data-panel="stocks" hidden>
<h2>Strategies</h2><div class="grid sleeves">{sleeve_cards}</div>
<div class="card chartcard"><div class="row"><div><div class="label">Strategy vs buy-and-hold</div>
<div class="subtle">Value of each $10,000 strategy</div></div><div class="spacer"></div>
<div class="ranges" data-for="sleeves"><button data-r="1W">1W</button><button data-r="1M">1M</button>
<button data-r="3M">3M</button><button data-r="All" class="on">All</button></div></div>
<div class="legend">{legend_sleeves}</div>
<div class="chart" id="sleeves-chart" aria-label="Sleeve values over time"></div></div>
{shadow_card}
<h2>Open positions</h2><div class="card scroll"><table>{trade_head}{open_rows or "<tr><td colspan=13 class='subtle'>No open positions.</td></tr>"}</table></div>
</section>
<section data-panel="forex" hidden>
{fx_card or '<h2>Forex</h2><p class="subtle">No forex sleeve is switched on.</p>'}
</section>
<section data-panel="trades" hidden><h2>Trading history</h2>
<div class="card scroll"><input class="search" placeholder="Search symbol, sleeve or reason…" aria-label="Search trades">
<table class="searchable">{trade_head}{all_rows or "<tr><td colspan=13 class='subtle'>No trades yet.</td></tr>"}</table></div>
<p class="subtle">Net P/L includes Alpaca's fees. Open trades are valued at the latest price. Each trade also has a note in the Obsidian vault.</p></section>
<section data-panel="orders" hidden><h2>Recent orders</h2><div class="card scroll"><table><tr><th>Time (UTC)</th><th>Sleeve</th><th>Side</th>
<th>Symbol</th><th>Type</th><th>Size</th><th>Stop</th><th>Status</th><th>Reason</th></tr>{order_rows}</table></div>
<h2>Warnings</h2><div class="card"><ul class="warnings">{warn_rows}</ul></div></section>
<section data-panel="evidence" hidden>
<h2>Does each strategy deserve its money?</h2>
<p class="subtle">The most recent Gate 1 result per strategy: parameters chosen on the first 70% of history,
then judged on the 30% they never saw. Sharpe is measured in excess of cash, so 0 means "no better than
T-bills". A strategy that fails here has not earned real money, whatever its recent run looks like.</p>
<div class="grid sleeves">{research_cards}</div>
<h2>Blocked by the risk gate</h2>
<p class="subtle">Orders the strategies wanted to place and the gate refused. Nothing here reached a broker.</p>
<div class="card scroll"><table><tr><th>Time (UTC)</th><th>Sleeve</th><th>Side</th><th>Symbol</th>
<th>Size</th><th>Why it was blocked</th></tr>{blocked_rows}</table></div></section>
</main>
<aside id="claude-panel" hidden aria-label="Ask Claude">
<div class="ap-head"><b>Claude</b><span class="spacer"></span>
<button id="claude-clear" title="Start a new conversation">New</button>
<button id="claude-close" title="Hide (⌘J)">&times;</button></div>
<div id="chat-log" class="chatlog"></div>
<div class="ap-foot">
<div class="chips">
<button class="chip">How is S1 doing against the benchmark?</button>
<button class="chip">Why did my last trade lose money?</button>
<button class="chip">Has the engine missed any runs today?</button>
<button class="chip">Which strategies pass Gate 1, and why does S4 fail?</button></div>
<form id="chat-form"><input id="chat-input" autocomplete="off"
 placeholder="Ask about your account&hellip;" aria-label="Ask Claude">
<button type="submit" id="chat-send">Ask</button></form>
<p class="ap-note">Reads your data and runs read-only <code>trader</code> commands.
Cannot place orders, change files or read credentials. Will not tell you what to buy.</p></div></aside><div class="tip" id="tip" hidden></div>
<script id="chart-data" type="application/json">{chart_data}</script><script>{JS}</script>
<script>{RAIL_JS}</script><script>{FX_JS}</script><script>{SEARCH_JS}</script><script>{CHAT_JS}</script></body></html>"""


def dashboard_html(settings: Settings, journal: Journal, engines: list[Engine], refresh_s: int = 300) -> str:
    d = collect(settings, journal, engines)
    d["_limits_day"] = f"{settings.risk.account.max_daily_loss_pct:g}"
    d["_limits_dd"] = f"{settings.risk.account.max_drawdown_halt_pct:g}"
    return render(d).replace('content="300"', f'content="{refresh_s}"', 1)


def write_dashboard(settings: Settings, journal: Journal, engines: list[Engine]) -> None:
    path = settings.root / "data" / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dashboard_html(settings, journal, engines))


CSS = """
.fxlayout{display:grid;grid-template-columns:230px 1fr;gap:16px;align-items:start}
.pairlist{display:flex;flex-direction:column;gap:6px;max-height:460px;overflow-y:auto}
.pair{text-align:left;background:#161614;border:1px solid var(--line);border-radius:10px;padding:.6rem .7rem;
  cursor:pointer;color:#d6d4cf;display:flex;flex-direction:column;gap:.15rem}
.pair:hover{border-color:#5a5a55}
.pair.on{border-color:#c98500;background:#1d1b16}
.pair .px{font-size:1.05rem;font-weight:500}
.pair .subtle{font-size:.72rem}
.fxchart .chart{height:380px}
@media (max-width:900px){.fxlayout{grid-template-columns:1fr}.pairlist{max-height:none}}
.rail{position:fixed;left:0;top:0;bottom:0;width:224px;background:#111110;border-right:1px solid var(--line);
  display:flex;flex-direction:column;gap:2px;padding:10px 10px 12px;z-index:50;overflow-y:auto}
.rail-top{display:flex;align-items:center;gap:9px;padding:2px 2px 10px}
.rail-top button{background:none;border:0;color:#6e6c67;cursor:pointer;padding:3px;border-radius:6px;display:flex}
.rail-top button:hover{background:#232321;color:#e8e6e1}
.rail-name{font-size:.82rem;color:#c9c7c1;font-weight:500;white-space:nowrap}
.rail-group{font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;color:#5f5d58;
  padding:12px 8px 5px;white-space:nowrap}
.watchlist{display:flex;flex-direction:column;gap:1px;overflow-y:auto}
.wrow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:.3rem .5rem;border-radius:7px;font-size:.76rem}
.wrow:hover{background:#1b1b19}
.wsym{color:#c9c7c1}
.wpx{color:#e8e6e1;text-align:right;font-variant-numeric:tabular-nums}
.wrow .up,.wrow .down,.wrow .subtle{grid-column:2;text-align:right;font-size:.7rem}
.dot-warn{width:6px;height:6px;border-radius:50%;background:#c98500;margin-left:auto}
.rail-brand{width:30px;height:30px;border-radius:8px;background:#c98500;color:#1a1a18;font-weight:600;
  display:flex;align-items:center;justify-content:center;font-size:.76rem;flex:0 0 auto}
.rail-btn{width:100%;border:0;border-radius:8px;background:none;color:#a7a59f;cursor:pointer;
  display:flex;align-items:center;gap:10px;padding:.45rem .5rem;font:inherit;font-size:.84rem;text-align:left}
.rail-btn svg{flex:0 0 auto}
.rail-btn span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail-btn .count{margin-left:auto;font-style:normal;font-size:.7rem;color:#5f5d58;
  background:#1d1d1b;border-radius:999px;padding:.05rem .4rem}
.rail-btn:hover{background:#232321;color:#e8e6e1}
.rail-btn.active{background:#232321;color:#c98500}
body{padding-left:224px}
/* Collapsed: icons only, same rail, no second layout to keep in step. */
body.rail-min .rail{width:62px;align-items:center;padding-left:0;padding-right:0}
body.rail-min .rail-btn{width:40px;height:40px;justify-content:center;padding:0;gap:0}
body.rail-min .rail-btn span,body.rail-min .rail-btn .count,body.rail-min .rail-name,
body.rail-min .rail-group,body.rail-min .watchlist{display:none}
body.rail-min{padding-left:62px}
#searchbar{position:fixed;left:62px;right:0;top:0;z-index:45;padding:10px 28px;background:#141412;
  border-bottom:1px solid var(--line);display:none}
#searchbar.on{display:block}
#searchbar input{width:100%;max-width:560px;background:#1b1b19;border:1px solid #3a3a37;color:#e8e6e1;
  border-radius:8px;padding:.55rem .8rem;font:inherit}
#searchbar input:focus{outline:none;border-color:#c98500}
tr.hide{display:none}
ul.headline{list-style:none;margin:14px 0 22px;padding:0;display:flex;flex-direction:column;gap:.45rem}
ul.headline li{padding:.55rem .9rem;border-radius:8px;border-left:3px solid #3a3a37;background:#161614;
  font-size:.95rem;line-height:1.5;color:#d6d4cf}
ul.headline li.hl-good{border-left-color:#199e70}
ul.headline li.hl-warn{border-left-color:#c98500}
ul.headline li.hl-bad{border-left-color:#d95926;background:#241614}
.label.help{border-bottom:1px dotted #5a5a55;cursor:help;display:inline-block}
/* Claude lives in a persistent right-hand panel, not a tab: it is for asking about what you are
   looking at, which only works if it is beside the thing you are looking at. */
#claude-panel{position:fixed;top:0;right:0;width:400px;height:100vh;z-index:40;display:flex;
  flex-direction:column;background:#111110;border-left:1px solid var(--line);box-shadow:-18px 0 40px #0008}
#claude-panel[hidden]{display:none}
body.claude-open main,body.claude-open header,body.claude-open nav.tabs{margin-right:400px}
.ap-head{display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--line)}
.ap-head button{background:none;border:0;color:#898781;font-size:1rem;cursor:pointer;padding:2px 8px;border-radius:6px}
.ap-head button:hover{background:#232321;color:#e8e6e1}
.ap-foot{padding:10px 14px 14px;border-top:1px solid var(--line)}
.ap-note{font-size:.72rem;color:#6e6c67;line-height:1.45;margin:.6rem 0 0}
#claude-toggle{background:#232321;border:1px solid #3a3a37;color:#c9c7c1;border-radius:999px;
  padding:.35rem .9rem;font-size:.85rem;cursor:pointer;white-space:nowrap}
#claude-toggle:hover{border-color:#c98500;color:#e8e6e1}
body.claude-open #claude-toggle{background:#c98500;color:#1a1a18;border-color:#c98500}
@media (max-width:1100px){#claude-panel{width:100%}body.claude-open main,body.claude-open header,
  body.claude-open nav.tabs{margin-right:0}}
.chatlog{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:.5rem;padding:14px}
.msg{padding:.5rem .75rem;border-radius:10px;white-space:pre-wrap;line-height:1.5;max-width:94%;font-size:.88rem}
.msg.you{align-self:flex-end;background:#2a2a28;color:#e8e6e1}
.msg.claude{align-self:flex-start;background:#1d1d1b;border:1px solid #333;color:#e8e6e1}
.msg.tool{align-self:flex-start;font-size:.8rem;color:#898781;font-family:ui-monospace,monospace;padding:.2rem .5rem}
.msg.err{align-self:flex-start;background:#3a1f1f;color:#f0a0a0;font-size:.85rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:.5rem}
.chip{background:#232321;border:1px solid #3a3a37;color:#c9c7c1;border-radius:999px;padding:.25rem .6rem;
      font-size:.75rem;cursor:pointer;text-align:left}
.chip:hover{border-color:#5a5a55}
#chat-form{display:flex;gap:.5rem}
#chat-input{flex:1;margin:0;background:#1b1b19;border:1px solid #3a3a37;color:#e8e6e1;
  border-radius:var(--radius,8px);padding:.5rem .7rem;font:inherit;font-size:.88rem}
#chat-input:focus{outline:none;border-color:#c98500}
#chat-send{background:#c98500;color:#1a1a18;border:0;border-radius:var(--radius,8px);padding:.5rem .9rem;
           font-weight:500;cursor:pointer}
#chat-send:disabled{opacity:.5;cursor:default}
:root { --page:#0b0b0a; --surface:#141413; --surface2:#1b1b19; --line:#2c2c2a; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --up:#2fbf71; --down:#e66767; --warn:#fab219; --accent:#c98500; }
* { box-sizing:border-box; }
body { margin:0; background:radial-gradient(1200px 500px at 70% -10%, #1d1a12 0%, var(--page) 60%); color:var(--ink);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif; min-height:100vh; }
header { display:flex; align-items:center; gap:16px; padding:20px 28px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.brand { width:36px; height:36px; border-radius:10px; background:var(--surface2); border:1px solid var(--line);
  display:grid; place-items:center; font-weight:700; color:var(--accent); }
h1 { font-size:22px; margin:0; font-weight:650; } h2 { font-size:15px; font-weight:600; margin:28px 0 10px; }
.spacer { flex:1; } .subtle { color:var(--muted); font-size:12px; } .label { color:var(--ink2); font-size:12px; margin-bottom:4px; }
.account { text-align:right; } .account b { font-weight:600; }
.tabs { display:flex; gap:22px; padding:0 28px; border-bottom:1px solid var(--line); }
.tab { background:none; border:0; color:var(--muted); padding:14px 0; cursor:pointer; font:inherit; border-bottom:2px solid transparent; }
.tab.active { color:var(--ink); border-bottom-color:var(--accent); }
main { padding:8px 28px 48px; max-width:1400px; margin:0 auto; }
.card { background:linear-gradient(180deg,var(--surface2),var(--surface)); border:1px solid var(--line); border-radius:14px; padding:18px; }
.grid { display:grid; gap:14px; } .grid.top { grid-template-columns:minmax(0,2fr) minmax(280px,1fr); margin-top:18px; }
.grid.sleeves { grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); margin-bottom:14px; }
@media (max-width:900px) { .grid.top { grid-template-columns:1fr; } header, main, .tabs { padding-left:16px; padding-right:16px; } }
.row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; } .row.gap { margin-top:6px; gap:14px; }
.hero { font-size:44px; font-weight:650; letter-spacing:-.02em; line-height:1.1; margin:2px 0 4px; }
.big { font-size:26px; font-weight:650; margin:6px 0 2px; }
.up { color:var(--up); } .down { color:var(--down); }
.ranges { display:flex; gap:4px; background:var(--page); border:1px solid var(--line); border-radius:9px; padding:3px; }
.ranges button { background:none; border:0; color:var(--muted); font:inherit; font-size:12px; padding:4px 9px; border-radius:6px; cursor:pointer; }
.ranges button.on { background:var(--surface2); color:var(--ink); }
.chart { position:relative; height:260px; margin-top:12px; } .chart svg { width:100%; height:100%; display:block; overflow:visible; }
.chart text { fill:var(--muted); font-size:11px; } .chart .end { fill:var(--ink2); font-weight:600; font-size:12px; }
.side .split { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:16px 0; padding:12px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
.meter { margin:12px 0; } .track { height:6px; border-radius:4px; background:#2a2a1f; overflow:hidden; }
.fill { height:100%; background:var(--up); border-radius:4px; } .fill.warn { background:var(--warn); } .fill.bad { background:var(--down); }
dl { display:grid; grid-template-columns:1fr 1fr; gap:10px 16px; margin:16px 0 0; }
dt { color:var(--muted); font-size:12px; } dd { margin:2px 0 0; font-weight:600; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); background:var(--surface); border:1px solid var(--line); border-radius:14px; overflow:hidden; }
.tile { padding:14px 16px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); }
.tile .value { font-size:22px; font-weight:650; margin:2px 0; }
.sleeve .dot { width:10px; height:10px; border-radius:50%; } .sleeve b { font-size:15px; }
.pill { display:inline-block; font-size:11px; padding:2px 8px; border-radius:20px; border:1px solid var(--line); color:var(--ink2); }
.pill.ok { color:var(--up); border-color:#1f4d33; background:#11261b; } .pill.warn { color:var(--warn); border-color:#5a4410; }
.pill.bad { color:var(--down); border-color:#5a2525; background:#2a1414; }
.pill.buy { color:var(--up); background:#11261b; border-color:#1f4d33; } .pill.sell { color:var(--down); background:#2a1414; border-color:#5a2525; }
.pill.open { color:var(--accent); border-color:#4d3a10; } .pill.closed { color:var(--muted); }
.breakdown { display:flex; gap:16px; flex-wrap:wrap; color:var(--ink2); font-size:12px; margin-top:8px; }
.sw { display:inline-block; width:14px; height:3px; border-radius:2px; margin-right:6px; vertical-align:middle; }
.legend { display:flex; gap:18px; color:var(--ink2); font-size:12px; margin-top:10px; }
.legend i { display:inline-block; width:14px; height:3px; border-radius:2px; margin-right:6px; vertical-align:middle; }
.scroll { overflow-x:auto; } table { width:100%; border-collapse:collapse; font-size:13px; white-space:nowrap; }
th { text-align:left; color:var(--muted); font-weight:500; font-size:12px; padding:8px 10px; border-bottom:1px solid var(--line); }
td { padding:10px; border-bottom:1px solid #222220; } tr:last-child td { border-bottom:0; }
td.num { text-align:right; font-variant-numeric:tabular-nums; } th:nth-child(n+6) { text-align:left; }
.mono { font-family:ui-monospace,Menlo,monospace; color:var(--ink2); } .sym { font-weight:600; }
.why { color:var(--muted); max-width:260px; overflow:hidden; text-overflow:ellipsis; }
.search { width:100%; max-width:320px; background:var(--page); border:1px solid var(--line); color:var(--ink); border-radius:8px;
  padding:8px 10px; font:inherit; margin-bottom:10px; }
.warnings { margin:0; padding-left:18px; } .warnings li { margin:6px 0; }
.tip { position:fixed; pointer-events:none; background:#1f1f1d; border:1px solid var(--line); border-radius:10px; padding:8px 10px;
  font-size:12px; z-index:10; box-shadow:0 8px 24px rgba(0,0,0,.4); }
.tip i { display:inline-block; width:10px; height:3px; border-radius:2px; margin-right:6px; vertical-align:middle; }
p.bad { color:var(--down); }
"""

FX_JS = r"""
(function () {
  var data = JSON.parse(document.getElementById('chart-data').textContent);
  var chart = document.getElementById('fx-chart'), list = document.querySelector('.pairlist');
  if (!chart || !list) return;
  var title = document.getElementById('fx-title'), sub = document.getElementById('fx-sub');
  var range = 'All', current = null;
  function rate(v) {
    // A rate is not a dollar amount, and gold needs 2 places where the majors need 5. The `axis`
    // argument is ignored on purpose: rounding a gridline to a whole number makes every one read "1".
    var dp = v >= 1000 ? 2 : v >= 100 ? 3 : 5;
    return v.toLocaleString(undefined, {minimumFractionDigits: dp, maximumFractionDigits: dp});
  }
  function show(pair) {
    var pts = (data.fx || {})[pair];
    current = pair;
    list.querySelectorAll('.pair').forEach(function (b) { b.classList.toggle('on', b.dataset.pair === pair); });
    title.textContent = pair;
    if (!pts || pts.length < 2) { chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">No price history for ' + pair + '.</p>'; sub.textContent = ''; return; }
    var first = pts[0][1], last = pts[pts.length - 1][1], pct = first ? (last / first - 1) * 100 : 0;
    sub.textContent = rate(last) + '  ·  ' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '% over the shown period';
    window.__ptDraw(chart, [{name: pair, color: '#c98500', pts: pts}], range, true, rate);
  }
  list.addEventListener('click', function (e) {
    var b = e.target.closest('.pair'); if (b) show(b.dataset.pair);
  });
  var ranges = document.querySelector('.ranges[data-for="fx"]');
  if (ranges) ranges.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    ranges.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
    range = b.dataset.r; if (current) show(current);
  });
  var firstBtn = list.querySelector('.pair');
  function drawWhenVisible() {
    // A hidden element has no width, so drawing into it makes a chart one pixel wide.
    setTimeout(function () {
      if (chart.offsetParent !== null) show(current || (firstBtn && firstBtn.dataset.pair));
    }, 60);
  }
  var railBtn = document.querySelector('.rail-btn[data-tab="forex"]');
  if (railBtn) railBtn.addEventListener('click', drawWhenVisible);
  // ...and on load, because the open tab is restored from last visit: without this, arriving straight
  // on Forex showed an empty box.
  drawWhenVisible();
  var redraw; window.addEventListener('resize', function () {
    clearTimeout(redraw); redraw = setTimeout(function () { if (chart.offsetParent !== null && current) show(current); }, 150);
  });
})();
"""

RAIL_JS = r"""
(function () {
  var btn = document.getElementById('rail-collapse');
  if (!btn) return;
  function set(min) {
    document.body.classList.toggle('rail-min', min);
    btn.title = min ? 'Expand sidebar' : 'Collapse sidebar';
    try { localStorage.setItem('railMin', min ? '1' : '0'); } catch (e) {}
  }
  btn.addEventListener('click', function () { set(!document.body.classList.contains('rail-min')); });
  var saved = '0'; try { saved = localStorage.getItem('railMin') || '0'; } catch (e) {}
  set(saved === '1');
})();
"""

SEARCH_JS = r"""
(function () {
  // Filters every table on the page rather than one, so a symbol is findable wherever it appears.
  var bar = document.getElementById('searchbar'), box = document.getElementById('searchbox'),
      btn = document.getElementById('rail-search');
  if (!bar || !box || !btn) return;
  function apply() {
    var q = box.value.trim().toLowerCase();
    document.querySelectorAll('main table tr').forEach(function (tr) {
      if (tr.querySelector('th')) return;                 // never hide a header
      tr.classList.toggle('hide', q !== '' && tr.textContent.toLowerCase().indexOf(q) === -1);
    });
  }
  function open(on) {
    bar.classList.toggle('on', on);
    btn.classList.toggle('active', on);
    if (on) { box.focus(); } else { box.value = ''; apply(); }
  }
  btn.addEventListener('click', function () { open(!bar.classList.contains('on')); });
  box.addEventListener('input', apply);
  document.addEventListener('keydown', function (e) {
    var typing = /^(INPUT|TEXTAREA)$/.test((e.target || {}).tagName || '');
    if (e.key === '/' && !typing) { e.preventDefault(); open(true); }
    if (e.key === 'Escape' && bar.classList.contains('on')) open(false);
  });
})();
"""

CHAT_JS = r"""
(function () {
  // The page used to reload itself with a meta refresh, which wiped the conversation mid-answer.
  // Reload from here instead, and never while the Ask tab is open or an answer is arriving.
  var secs = parseInt((document.querySelector('meta[name=pt-refresh]') || {}).content || '300', 10);
  window.__ptBusy = false;
  setInterval(function () {
    var onAsk = document.querySelector('.tab[data-tab="ask"].active');
    if (!onAsk && !window.__ptBusy) location.reload();
  }, Math.max(secs, 15) * 1000);
})();
(function () {
  var panel = document.getElementById('claude-panel'), toggle = document.getElementById('claude-toggle');
  if (!panel || !toggle) return;
  function setOpen(open) {
    panel.hidden = !open;
    document.body.classList.toggle('claude-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    try { localStorage.setItem('claudePanel', open ? '1' : '0'); } catch (e) {}
    if (open) { var i = document.getElementById('chat-input'); if (i) i.focus(); }
  }
  toggle.addEventListener('click', function () { setOpen(panel.hidden); });
  var close = document.getElementById('claude-close');
  if (close) close.addEventListener('click', function () { setOpen(false); });
  var fresh = document.getElementById('claude-clear');
  if (fresh) fresh.addEventListener('click', function () {
    document.getElementById('chat-log').innerHTML = ''; window.__ptSession = null;
    document.getElementById('chat-input').focus();
  });
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); setOpen(panel.hidden); }
    if (e.key === 'Escape' && !panel.hidden) setOpen(false);
  });
  var saved = '0'; try { saved = localStorage.getItem('claudePanel') || '0'; } catch (e) {}
  setOpen(saved === '1');
})();
(function () {
  var form = document.getElementById('chat-form'), input = document.getElementById('chat-input'),
      log = document.getElementById('chat-log'), send = document.getElementById('chat-send');
  window.__ptSession = window.__ptSession || null;
  if (!form) return;
  function add(cls, text) {
    var el = document.createElement('div'); el.className = 'msg ' + cls; el.textContent = text;
    log.appendChild(el); log.scrollTop = log.scrollHeight; return el;
  }
  document.querySelectorAll('.chip').forEach(function (c) {
    c.addEventListener('click', function () { input.value = c.textContent; form.requestSubmit(); });
  });
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var q = input.value.trim(); if (!q) return;
    input.value = ''; send.disabled = true; window.__ptBusy = true; add('you', q);
    var reply = null, thinking = add('tool', 'Thinking\u2026');
    fetch('/ask', {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({question: q, session_id: window.__ptSession})})
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
        (function pump() {
          return reader.read().then(function (res) {
            if (res.done) { send.disabled = false; return; }
            buf += dec.decode(res.value, {stream: true});
            var parts = buf.split('\n\n'); buf = parts.pop();
            parts.forEach(function (chunk) {
              if (chunk.indexOf('data: ') !== 0) return;
              var ev; try { ev = JSON.parse(chunk.slice(6)); } catch (err) { return; }
              if (ev.kind === 'tool') { if (thinking) { thinking.remove(); thinking = null; } add('tool', ev.payload); }
              else if (ev.kind === 'text') {
                if (thinking) { thinking.remove(); thinking = null; }
                if (!reply) reply = add('claude', ''); reply.textContent += ev.payload;
                log.scrollTop = log.scrollHeight;
              } else if (ev.kind === 'done') {
                if (thinking) { thinking.remove(); thinking = null; }
                window.__ptSession = (ev.payload && ev.payload.session_id) || window.__ptSession; reply = null; send.disabled = false; window.__ptBusy = false;
              } else if (ev.kind === 'error') {
                if (thinking) { thinking.remove(); thinking = null; }
                add('err', ev.payload); send.disabled = false; window.__ptBusy = false;
              }
            });
            return pump();
          });
        })();
      })
      .catch(function (err) { if (thinking) thinking.remove(); add('err', String(err)); send.disabled = false; window.__ptBusy = false; });
  });
})();
"""

JS = r"""
(function () {
  var data = JSON.parse(document.getElementById('chart-data').textContent);
  var tip = document.getElementById('tip');
  window.__ptDraw = function () { return draw.apply(null, arguments); };
  var RANGE = { '1D': 1, '1W': 7, '1M': 30, '3M': 91 };
  function cutoff(r, last) {
    if (r === 'All') return -Infinity;
    if (r === 'YTD') return Date.UTC(new Date(last).getUTCFullYear(), 0, 1);
    return last - RANGE[r] * 864e5;
  }
  function money(v, axis) {
    if (axis) return '$' + Math.round(v).toLocaleString();
    return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function nice(lo, hi, n) {
    var span = hi - lo || Math.abs(hi) || 1, step = Math.pow(10, Math.floor(Math.log10(span / n))), err = span / n / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var t = [], v = Math.floor(lo / step) * step;
    for (; v <= hi + step * 0.5; v += step) t.push(v);
    return t;
  }
  function draw(el, series, range, area, fmt) {
    fmt = fmt || money;
    var W = el.clientWidth || 800, H = el.clientHeight || 260, L = 70, R = area ? 12 : 120, T = 10, B = 24;
    var last = -Infinity;
    series.forEach(function (s) { if (s.pts.length) last = Math.max(last, Date.parse(s.pts[s.pts.length - 1][0])); });
    var from = cutoff(range, last);
    var ser = series.map(function (s) {
      return { name: s.name, color: s.color, pts: s.pts.map(function (p) { return [Date.parse(p[0]), p[1]]; })
        .filter(function (p) { return p[0] >= from; }) };
    }).filter(function (s) { return s.pts.length; });
    if (!ser.length || ser.every(function (s) { return s.pts.length < 2; })) {
      el.innerHTML = '<p class="subtle" style="padding-top:90px;text-align:center">The chart fills in as the engine runs (every 15 minutes).</p>';
      return;
    }
    var x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    ser.forEach(function (s) { s.pts.forEach(function (p) {
      x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]); y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]); }); });
    var pad = (y1 - y0) * 0.08 || y1 * 0.002 || 1; y0 -= pad; y1 += pad;
    var X = function (t) { return L + (t - x0) / ((x1 - x0) || 1) * (W - L - R); };
    var Y = function (v) { return T + (y1 - v) / ((y1 - y0) || 1) * (H - T - B); };
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img">';
    nice(y0, y1, 4).forEach(function (v) {
      if (v < y0 || v > y1) return;
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="#2c2c2a"/>' +
        '<text x="' + (L - 8) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + fmt(v, true) + '</text>';
    });
    var days = (x1 - x0) / 864e5, ticks = 5;
    for (var i = 0; i <= ticks; i++) {
      var t = x0 + (x1 - x0) * i / ticks, d = new Date(t);
      var lab = days < 2 ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) :
        d.toLocaleDateString([], { day: 'numeric', month: 'short' });
      svg += '<text x="' + X(t) + '" y="' + (H - 6) + '" text-anchor="middle">' + lab + '</text>';
    }
    ser.forEach(function (s, j) {
      var d = s.pts.map(function (p, k) { return (k ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); }).join('');
      if (area) {
        svg += '<defs><linearGradient id="g' + j + '" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="' + s.color +
          '" stop-opacity=".28"/><stop offset="1" stop-color="' + s.color + '" stop-opacity="0"/></linearGradient></defs>' +
          '<path d="' + d + 'L' + X(s.pts[s.pts.length - 1][0]) + ',' + (H - B) + 'L' + X(s.pts[0][0]) + ',' + (H - B) +
          'Z" fill="url(#g' + j + ')"/>';
      }
      svg += '<path d="' + d + '" fill="none" stroke="' + s.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    });
    if (!area) {  // direct end labels, nudged apart so they never overlap
      var ends = ser.map(function (s) { var lp = s.pts[s.pts.length - 1]; return { y: Y(lp[1]), s: s, v: lp[1] }; })
        .sort(function (a, b) { return a.y - b.y; });
      var prev = -1e9;
      ends.forEach(function (e) {
        var y = Math.max(e.y, prev + 15); prev = y;
        svg += '<text class="end" x="' + (W - R + 8) + '" y="' + (y + 4) + '">' + e.s.name + ' ' + fmt(e.v) + '</text>';
      });
    }
    svg += '<line class="x" y1="' + T + '" y2="' + (H - B) + '" stroke="#898781" stroke-dasharray="3 3" style="display:none"/>';
    ser.forEach(function (s) { svg += '<circle class="d" r="4" fill="' + s.color + '" stroke="#141413" stroke-width="2" style="display:none"/>'; });
    svg += '<rect x="' + L + '" y="' + T + '" width="' + (W - L - R) + '" height="' + (H - T - B) + '" fill="transparent"/></svg>';
    el.innerHTML = svg;
    var node = el.querySelector('svg'), cross = node.querySelector('.x'), dots = node.querySelectorAll('.d');
    node.addEventListener('mousemove', function (ev) {
      var r = node.getBoundingClientRect(), mx = (ev.clientX - r.left) * W / r.width, t = x0 + (mx - L) / (W - L - R) * (x1 - x0);
      var html = '', shownT = null;
      ser.forEach(function (s, j) {
        var best = s.pts[0];
        s.pts.forEach(function (p) { if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; });
        shownT = shownT || best[0];
        dots[j].setAttribute('cx', X(best[0])); dots[j].setAttribute('cy', Y(best[1])); dots[j].style.display = '';
        html += '<div><i style="background:' + s.color + '"></i>' + s.name + ': <b>' + fmt(best[1]) + '</b></div>';
      });
      cross.setAttribute('x1', X(shownT)); cross.setAttribute('x2', X(shownT)); cross.style.display = '';
      tip.innerHTML = '<div class="subtle">' + new Date(shownT).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) + '</div>' + html;
      tip.hidden = false; tip.style.left = (ev.clientX + 14) + 'px'; tip.style.top = (ev.clientY + 14) + 'px';
    });
    node.addEventListener('mouseleave', function () {
      tip.hidden = true; cross.style.display = 'none'; dots.forEach(function (d) { d.style.display = 'none'; });
    });
  }
  var charts = {
    account: function (r) { var ks = Object.keys(data.account); draw(document.getElementById('account-chart'),
      ks.map(function (k) { return { name: ks.length > 1 ? (data.labels[k] || k).split(' (')[0] : 'Account',
        color: data.colors[k], pts: data.account[k] }; }), r, ks.length === 1); },
    sleeves: function (r) { draw(document.getElementById('sleeves-chart'), Object.keys(data.sleeves).sort().map(function (k) {
      return { name: k, color: data.colors[k], pts: data.sleeves[k] }; }), r, false); },
    shadow: function (r) { var el = document.getElementById('shadow-chart'); if (!el) return;
      var ks = Object.keys(data.shadow).sort();
      draw(el, ks.map(function (k) { return { name: k, color: data.colors.shadow, pts: data.shadow[k] }; }), r, ks.length === 1); }
  };
  var current = { account: 'All', sleeves: 'All', shadow: 'All' };
  document.querySelectorAll('.ranges').forEach(function (box) {
    box.addEventListener('click', function (ev) {
      var b = ev.target.closest('button'); if (!b) return;
      box.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
      current[box.dataset.for] = b.dataset.r; charts[box.dataset.for](b.dataset.r);
    });
  });
  function redraw() { charts.account(current.account); charts.sleeves(current.sleeves); charts.shadow(current.shadow); }
  window.addEventListener('resize', redraw); redraw();
  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (t) { t.classList.toggle('active', t === tab); });
      document.querySelectorAll('[data-panel]').forEach(function (p) { p.hidden = p.dataset.panel !== tab.dataset.tab; });
      try { localStorage.setItem('tab', tab.dataset.tab); } catch (e) {}
      redraw();
    });
  });
  try { var saved = localStorage.getItem('tab'); if (saved) document.querySelector('.tab[data-tab="' + saved + '"]').click(); } catch (e) {}
  var search = document.querySelector('.search');
  if (search) search.addEventListener('input', function () {
    var q = search.value.toLowerCase();
    document.querySelectorAll('table.searchable tr').forEach(function (tr, i) {
      if (i) tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    });
  });
})();
"""
