"""data/dashboard.html: the paper-trading dashboard, rewritten on every engine run (every 15 minutes).

Kept in data/ (git-ignored) because it shows account details. Open it in any browser; it reloads itself.
Tables are rendered here in Python; the charts are drawn in the browser so the time-range buttons and
hover read-outs work without a server.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from trader.config import Settings
from trader.journal.db import Journal, trading_day_start
from trader.engine.explain import GLOSSARY, headline
from trader.models import AssetClass, format_price, price_decimals
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


def _blocked_today(journal: Journal, now: datetime) -> list[dict]:
    """Markets a sleeve wanted today and could not take, with the reason.

    These alert once a day on purpose -- the engine runs every fifteen minutes and the same symbols stay
    blocked all day, so alerting each time trains you to ignore it. But the dashboard only renders
    events from the last hour, so without this the page would quietly stop mentioning a market the
    sleeve still cannot buy. The notification is the interruption; this is the state.
    """
    out: list[dict] = []
    for kind, why in (("unaffordable", "not enough capital for one share"),
                      ("washblocked", "another sleeve holds it with a stop")):
        for broker in ("alpaca", "oanda", "shadow", "shadow_fx"):
            detail = journal.kv_get(f"{kind}:{broker}:{now.date()}")
            if detail and detail != "1":
                out.append({"kind": kind, "broker": broker, "why": why, "detail": detail})
    return out


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
                        "benchmark": sleeve.id == "B0", "broker": sleeve.broker,
                        "retiring": sleeve.retiring,
                        "retired": journal.kv_get(f"retired:{sleeve.id}") is not None})
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
        "equities": fx_series(sorted({x for sl in settings.sleeves.values() if sl.enabled
                                      for x in sl.symbols
                                      if settings.asset_class(x) != AssetClass.FOREX}),
                              {w["symbol"]: w["price"] for w in watchlist_rows}
                              if (watchlist_rows := watchlist(settings, price)) else {}),
        "orders": journal.recent_order_rows(25),
        "rate": stored_rate(journal),
        "fx": fx_snapshot(settings, journal, engines),
        # Rendered here rather than in render(), which only ever sees this dict.
        "intraday": intraday_card(settings, journal, now),
        "research": latest_research(settings),
        "gate2": latest_gate2(settings),
        "health": operational_health(settings, journal, now),
        "blocked": blocked_orders(journal),
        "warnings": [e for e in journal.recent_events(60) if e["level"] in ("warning", "error", "critical")][:12],
        "blocked_today": _blocked_today(journal, now),
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
    """Daily candles per symbol: [iso, open, high, low, close, volume].

    Read from the same cached CSVs the backtests use, so drawing costs no network call -- only the live
    quote does, and the page already fetches that. Daily bars, not ticks: this strategy decides once a
    day, and a minute chart would imply a precision it never acts on.

    The high and low are the point. A stop is hit on the day's LOW, and every loss this system has taken
    came from a stop, so a line of closes hides exactly the thing that decided the trade.

    Volume is 0 for spot FX majors -- it is traded over the counter with no central exchange, so there is
    no such figure to report. Gold (a futures contract), stocks and crypto do have it.
    """
    from trader.data.history import load_history

    out: dict[str, list] = {}
    for symbol in symbols:
        try:
            frame = load_history(symbol).tail(FX_CHART_DAYS)
        except Exception:
            continue
        candles = [[ts.isoformat(), round(float(r.open), 6), round(float(r.high), 6),
                    round(float(r.low), 6), round(float(r.close), 6), float(r.volume or 0)]
                   for ts, r in zip(frame.index, frame.itertuples())]
        now_price = (live or {}).get(symbol)
        if now_price and candles:
            # Today, still forming: open at yesterday's close, high/low stretched to include it.
            prev = candles[-1][4]
            candles.append([datetime.now(timezone.utc).isoformat(timespec="minutes"), prev,
                            max(prev, now_price), min(prev, now_price), round(now_price, 6), 0.0])
        if candles:
            out[symbol] = candles
    return out


def _fx_levels(sleeve, symbols: list[str]) -> dict[str, dict]:
    """The strategy's OWN latest indicator values, per symbol.

    Built by running the real strategy object over the same daily history the engine uses, rather than
    recomputing moving averages here. A second implementation would be free to drift from the first, and
    the panel's whole job is to say why the engine is or is not buying -- being confidently wrong about
    that is worse than saying nothing.
    """
    from trader.data.history import load_history
    from trader.strategies import LIVE_REGISTRY

    try:
        cls = LIVE_REGISTRY[sleeve.strategy or sleeve.id]
        strat = cls(params={**cls.default_params, **sleeve.params}, symbols=list(symbols))
        bars = {}
        for symbol in symbols:
            try:
                bars[symbol] = load_history(symbol)
            except Exception:
                return {}
        strat.setup(bars)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for symbol in symbols:
        frame = strat.indicators.get(symbol)
        if frame is None or frame.empty:
            continue
        last = frame.iloc[-1]
        out[symbol] = {"fast": float(last["fast"]), "slow": float(last["slow"]),
                       "atr": float(last["atr"]), "bar_close": float(last["close"]),
                       "fast_n": int(strat.params["fast"]), "slow_n": int(strat.params["slow"]),
                       "atr_mult": float(strat.params["atr_mult"]),
                       "risk_pct": 1.0, "capital": sleeve.capital_usd}
    return out


def _pip(price: float) -> float:
    """One pip, derived from how the market quotes the price rather than hardcoded per symbol.

    The majors quote to 5 places and move in 0.0001; JPY pairs quote to 3 and move in 0.01; gold quotes
    to 2 and moves in 0.10. That is the same magnitude rule `price_decimals` already encodes, so deriving
    it here keeps one source of truth instead of a second table to forget to update.
    """
    return 10 ** -(price_decimals(price, AssetClass.FOREX) - 1)


def _units(qty: float) -> str:
    """Quantity at a precision that does not lie about the size.

    Gold is the reason. A $100 risk budget against a $347 stop distance buys 0.29 ounces, and "{:,.0f}"
    renders that as "0 units" -- which reads as "it cannot trade this", not as a fractional position.
    """
    if qty >= 100:
        return f"{qty:,.0f}"
    return f"{qty:,.2f}" if qty >= 1 else f"{qty:,.3f}"


def _distance(gap: float, price: float) -> str:
    """How far away a level is, in the unit that market actually uses.

    Pips are right for the majors and for JPY pairs. Gold quotes in dollars and cents, and "3,474 pips"
    is both technically defensible and useless -- nobody trading gold thinks in pips.
    """
    if price_decimals(price, AssetClass.FOREX) <= 2:
        return _money(gap)
    return f"{gap / _pip(price):,.0f} pips"


def _fx_history(trips: list, price) -> str:
    """Every trade this sleeve has made in one pair, newest first.

    The plan above says what the rules WILL do; this says what they have actually done. Without it the
    page can describe a strategy at length and still never answer "so what has it traded here".
    """
    if not trips:
        return '<p class="subtle">No trades in this pair yet.</p>'
    fx = AssetClass.FOREX
    rows = ""
    for t in sorted(trips, key=lambda x: x.entry_ts, reverse=True):
        now = price(t.symbol) if t.is_open else None
        pnl = t.pnl(now)
        out_px = now if t.is_open else t.exit_price
        rows += (
            f"<tr><td>{t.entry_ts[:10]}</td><td class='num'>{format_price(t.entry_price, fx)}</td>"
            f"<td>{'open' if t.is_open else t.exit_ts[:10]}</td>"
            f"<td class='num'>{format_price(out_px, fx) if out_px else '–'}</td>"
            f"<td class='num'>{_units(t.buy_qty)}</td>"
            f"<td class='num {_tone(pnl)}'>{_money(pnl, signed=True)}</td>"
            f"<td class='why'>{'<span class=\'subtle\'>still open</span>' if t.is_open else html.escape(t.exit_reason or '')}</td></tr>")
    return (f"<table class='histtable'><tr><th>Opened</th><th>Bought at</th><th>Closed</th>"
            f"<th>Sold at</th><th>Size</th><th>Net P/L</th><th>Why it ended</th></tr>{rows}</table>")


def _ladder(stop: float, entry: float, now: float, high: float | None, fx) -> str:
    """Where the rate sits between the level that ends the trade and the best it has seen.

    Taken from the loss-limit bars in prop-firm dashboards (E8 Markets): a track whose left end is the
    level that stops you out, so "how much room is left" is a distance you can see rather than a
    subtraction you have to do. The numbers are already stated above it; this is for the glance.
    """
    top = max(high or 0.0, now, entry)
    span = top - stop
    if span <= 0:
        return ""
    pos = lambda v: max(0.0, min(100.0, (v - stop) / span * 100))   # noqa: E731
    now_pct, entry_pct = pos(now), pos(entry)
    tone = "up" if now >= entry else "down"
    return (
        f'<div class="ladder">'
        f'<div class="ladder-track">'
        f'<div class="ladder-fill {tone}" style="width:{now_pct:.1f}%"></div>'
        f'<div class="ladder-mark entry" style="left:{entry_pct:.1f}%" title="bought here"></div>'
        f'<div class="ladder-mark now {tone}" style="left:{now_pct:.1f}%" title="now"></div>'
        f'</div>'
        f'<div class="ladder-ends"><span class="down">{format_price(stop, fx)} &middot; sells here</span>'
        f'<span class="spacer"></span><span class="subtle">bought {format_price(entry, fx)}</span>'
        f'<span class="spacer"></span><span class="subtle">best {format_price(top, fx)}</span>'
        f'</div></div>')


def _rule(name: str, need: str, have: str, ok: bool) -> str:
    """One entry condition as a card: what the rule wants, what it currently is, and whether it is met.

    The Goal Overview cards in E8's dashboard do this well -- the requirement and the live value get
    equal weight, and a pill states the verdict rather than leaving you to compare two numbers.
    """
    pill = ('<span class="pill ok">met</span>' if ok else '<span class="pill closed">not yet</span>')
    return (f'<div class="rule"><div class="rule-head"><span class="label">{name}</span>{pill}</div>'
            f'<div class="rule-vals"><div><dt>Needs</dt><dd>{need}</dd></div>'
            f'<div><dt>Now</dt><dd class="{"up" if ok else "down"}">{have}</dd></div></div></div>')


def fx_plan(r: dict, trips: list | None = None, price=None) -> str:
    """One pair, in plain English: what it bought at, and what would make it sell.

    Written because "what price does it buy at and when does it plan to sell" has a specific answer for a
    trend strategy, and the honest part of that answer is counter-intuitive: there is NO planned sell
    price. There is a stop that only ever moves up, and a trend-ended rule. A panel that invented a
    target would be describing a different strategy from the one actually running.
    """
    sym, px = r["symbol"], r.get("price")
    fx = AssetClass.FOREX
    if px is None or r.get("slow") is None:
        return (f'<div class="plan"><div class="plan-head"><b>{html.escape(sym)}</b>'
                f'<span class="pill closed">no data</span></div>'
                f'<p class="subtle">No rate or price history for this pair right now.</p></div>')
    f = lambda v: format_price(v, fx)          # noqa: E731 - a local shorthand, used a dozen times below
    stop_gap = r["atr_mult"] * r["atr"]
    qty_held = r.get("qty") or 0.0

    if qty_held > 0 and r.get("entry_price"):
        entry, stop = r["entry_price"], r.get("stop") or r.get("initial_stop")
        pnl = (px - entry) * qty_held
        to_stop = (px - stop) if stop else None
        at_risk = to_stop * qty_held if to_stop is not None else None
        when = (r.get("entry_date") or "")[:10]
        moved = (stop and r.get("initial_stop") and stop > r["initial_stop"])
        body = (
            f'<dl class="plan-rows">'
            f'<div><dt>Bought at</dt><dd>{f(entry)}<span class="subtle"> on {html.escape(when)}</span></dd></div>'
            f'<div><dt>Holding</dt><dd>{_units(qty_held)} units</dd></div>'
            f'<div><dt>Rate now</dt><dd>{f(px)} <span class="{_tone(pnl)}">{_money(pnl, signed=True)}</span></dd></div>'
            f'<div><dt>Sells if it falls to</dt><dd>{f(stop) if stop else "–"}'
            + (f'<span class="subtle"> · {_distance(abs(to_stop), px)} away, {_money(at_risk)} more at risk</span>'
               if to_stop is not None else "") + '</dd></div></dl>'
            + (_ladder(stop, entry, px, r.get("high_since"), fx) if stop else "")
            + f'<p class="plan-note">That stop{" has already moved up from " + f(r["initial_stop"]) + " and" if moved else ""}'
            f' only ever moves up, never down, so the worst case keeps improving as the trade works.</p>'
            f'<p class="plan-note">It also sells straight away if the trend ends &mdash; the {r["fast_n"]}-day '
            f'average ({f(r["fast"])}) falling back below the {r["slow_n"]}-day ({f(r["slow"])}).</p>'
            f'<p class="plan-note"><b>There is no target price.</b> This kind of strategy has no planned '
            f'sell level: it stays in the move until the stop is hit or the trend ends, because cutting a '
            f'winner at a fixed target is what stops trend following working.</p>')
        pill = '<span class="pill open">holding</span>'
    else:
        above_slow, fast_above = px > r["slow"], r["fast"] > r["slow"]
        risk_budget = r["capital"] * r["risk_pct"] / 100
        would_stop = px - stop_gap
        would_qty = risk_budget / stop_gap if stop_gap > 0 else 0
        body = (
            f'<p class="plan-note">Not holding. It buys only when <b>both</b> of these are true:</p>'
            f'<div class="rules">'
            + _rule(f'Rate above its {r["slow_n"]}-day average', f'above {f(r["slow"])}',
                    f'{f(px)} ({(px / r["slow"] - 1):+.2%})', above_slow)
            + _rule(f'{r["fast_n"]}-day average above the {r["slow_n"]}-day', f'above {f(r["slow"])}',
                    f(r["fast"]), fast_above)
            + '</div>'
            f'<div class="label" style="margin-top:16px">If it bought at today\'s rate</div>'
            f'<div class="would">'
            f'<div><dt>Buys</dt><dd>{_units(would_qty)}<span class="subtle"> units</span></dd></div>'
            f'<div><dt>At</dt><dd>{f(px)}</dd></div>'
            f'<div><dt>Stop goes at</dt><dd>{f(would_stop)}</dd></div>'
            f'<div><dt>Most it can lose</dt><dd class="down">{_money(risk_budget)}</dd></div></div>'
            f'<p class="plan-note">The stop sits {_distance(stop_gap, px)} below, which is '
            f'{r["atr_mult"]:g}x how far this pair typically moves in a day. The size is worked backwards '
            f'from it, so being stopped out costs {r["risk_pct"]:g}% of the sleeve and no more.</p>')
        pill = ('<span class="pill closed">waiting</span>' if not (above_slow and fast_above)
                else '<span class="pill ok">conditions met</span>')
    history = ("" if trips is None else
               f'<div class="plan-hist"><div class="label">Trades so far in {html.escape(sym)}</div>'
               f'{_fx_history(trips, price)}</div>')
    # Hidden by default; the pair list reveals the selected one, and show() runs on load.
    return (f'<div class="plan" data-plan="{html.escape(sym)}" hidden><div class="plan-head">'
            f'<b>{html.escape(sym)}</b>{pill}</div>{body}{history}</div>')


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
    levels = _fx_levels(fx_sleeves[0], symbols)
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
        row = {"symbol": symbol, "price": quote.price if quote else None,
               "ts": quote.ts if quote else None, "qty": held.get(symbol, 0.0),
               "stop": (state or {})["stop"] if state else None}
        if state is not None:
            row.update(entry_price=state["entry_price"], entry_date=state["entry_date"],
                       initial_stop=state["initial_stop"], high_since=state["high_since_entry"])
        row.update(levels.get(symbol) or {})
        rows.append(row)
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


PIP = 1e-4

# What each state of the day means in a sentence, and how loudly to say it. The tone drives the pill
# and the card's own emphasis: only a live trade is loud, because only a live trade is something you
# are in the middle of and could still get wrong.
_IRV_STATE = {
    "none":    ("pending",  "closed", "No plan yet"),
    "waiting": ("waiting",  "closed", "Waiting for the level"),
    "in":      ("HOLDING",  "warn",   "You should be in this trade"),
    "done":    ("finished", "ok",     "Finished for today"),
    "missed":  ("skipped",  "closed", "No plan today — the open was missed"),
    "gapped":  ("skipped",  "closed", "Skipped — it went through both levels at once"),
}


def _irv_pair(pair: dict, close_utc: str, in_session: bool) -> str:
    """One pair's card. See intraday_today for why this exists at all."""
    fx = AssetClass.FOREX
    f = lambda v: format_price(v, fx)          # noqa: E731
    sym, plan, state, rate = pair["symbol"], pair["plan"], pair["state"], pair["rate"]
    word, tone, headline = _IRV_STATE.get(state, _IRV_STATE["none"])

    if plan is None:
        when = "The levels are measured from the session open and set just after it."
        if state == "missed":
            when = "The open was missed, so the levels cannot be measured from it. Skipped, by design."
        return (f'<div class="plan irv"><div class="plan-head"><b>{html.escape(sym)}</b>'
                f'<span class="pill {tone}">{word}</span></div>'
                f'<p class="subtle">{html.escape(headline)}. {html.escape(when)}</p></div>')

    rows = (
        f'<div><dt>Buy at</dt><dd>{f(plan.trigger)}</dd></div>'
        f'<div><dt>Stop</dt><dd>{f(plan.stop)}'
        f'<span class="subtle"> {plan.risk_pips:.0f} pips · ${plan.risk_usd:,.0f}</span></dd></div>'
        f'<div><dt>Target</dt><dd>{f(plan.target)}'
        f'<span class="subtle"> {plan.reward_pips:.0f} pips</span></dd></div>'
        f'<div><dt>Size</dt><dd>{plan.qty:,.0f}<span class="subtle"> units</span></dd></div>')

    live = ""
    if rate is not None:
        if state == "waiting":
            away = (rate - plan.trigger) / PIP
            # Signed, not just coloured: green and red are 1.05:1 apart in the light theme.
            live = (f'<p class="irv-now">Now <b>{f(rate)}</b> · '
                    f'<span class="{"up" if away > 0 else "down"}">{away:+.1f} pips</span> from the buy level</p>')
        elif state == "in" and pair["entry"]:
            pips = (rate - pair["entry"]) / PIP
            live = (f'<p class="irv-now">Now <b>{f(rate)}</b> · bought {f(pair["entry"])} · '
                    f'<span class="{"up" if pips >= 0 else "down"}">{pips:+.1f} pips</span>, about '
                    f'<span class="{"up" if pips >= 0 else "down"}">{pips * PIP * plan.qty:+,.0f} USD</span></p>')
        else:
            live = f'<p class="irv-now">Now <b>{f(rate)}</b></p>'

    # Your report, next to the system's own quote -- not instead of it. The system only ever knows the
    # rate it quoted; it has no broker connection to this pair at all, so whether you actually took the
    # trade, and at what price, is otherwise invisible here and lives only in a Telegram chat.
    report = pair.get("report")
    if report is not None:
        kind, price = report
        reported = (f'<p class="irv-reported">You reported: <b>skipped</b></p>' if kind == "skipped" else
                    f'<p class="irv-reported">You reported: <b>taken at {f(price)}</b></p>')
    elif state in ("in", "done") and plan is not None:
        reported = '<p class="irv-reported subtle">No /taken or /skipped reply yet</p>'
    else:
        reported = ""

    note = (f"Flat by {close_utc} UTC either way." if state in ("waiting", "in")
            else "The session is over for today." if not in_session else "")
    evidence = pair.get("evidence")
    foot = (f'<p class="ap-note">{html.escape(note)} '
            f'{("Shadow test — " + html.escape(evidence) + ". Not advice.") if evidence else ""}</p>')
    return (f'<div class="plan irv irv-{html.escape(state)}"><div class="plan-head">'
            f'<b>{html.escape(sym)}</b><span class="pill {tone}">{word}</span></div>'
            f'<p class="irv-head">{html.escape(headline)}</p>'
            f'<dl class="plan-rows">{rows}</dl>{live}{reported}{foot}</div>')


def intraday_card(settings: Settings, journal: Journal, now: datetime | None = None) -> str:
    """The hand-executed forex plan, rendered for the Forex page. Empty string when switched off."""
    d = intraday_today(settings, journal, now)
    if not d:
        return ""
    body = "".join(_irv_pair(p, d["close_utc"], d["in_session"]) for p in d["pairs"])
    opens_at = f"{d['open_local']:%-I%p}".lower()
    closes_at = f"{d['close_local']:%-I%p}".lower()
    if d["weekend"]:
        when = "The forex market is shut at the weekend. The next plan goes out on Monday."
    elif d["in_session"]:
        when = (f"The London session is open. Checked every minute until {d['close_utc']} UTC "
                f"({closes_at} your time).")
    else:
        when = (f"Outside the session. The next plan goes out just after {d['open_utc']} UTC, "
                f"which is {opens_at} where you are.")

    # When today has nothing yet, the last session is more use than an empty card.
    if all(p["plan"] is None for p in d["pairs"]) and d.get("previous"):
        last = "".join(
            f'<div class="plan irv"><div class="plan-head"><b>{html.escape(p["symbol"])}</b>'
            f'<span class="pill closed">{p["day"]:%a %-d %b}</span></div>'
            f'<p class="irv-head">{html.escape(_IRV_STATE.get(p["state"], _IRV_STATE["none"])[2])}</p>'
            f'<dl class="plan-rows">'
            f'<div><dt>Buy was</dt><dd>{format_price(p["plan"].trigger, AssetClass.FOREX)}</dd></div>'
            f'<div><dt>Stop</dt><dd>{format_price(p["plan"].stop, AssetClass.FOREX)}</dd></div>'
            f'<div><dt>Target</dt><dd>{format_price(p["plan"].target, AssetClass.FOREX)}</dd></div>'
            f'</dl></div>' for p in d["previous"])
        body += last
    return (f'<h2>Today\'s plan <span class="pill closed">you place these</span></h2>'
            f'<p class="subtle">These are the messages sent to your phone, shown here so the answer to '
            f'&ldquo;what am I meant to be doing&rdquo; is not only in a notification. '
            f'{html.escape(when)} Nothing here is sent to any broker.</p>'
            f'<div class="irv-grid">{body}</div>')


def intraday_today(settings: Settings, journal: Journal, now: datetime | None = None) -> dict:
    """Today's hand-executed forex plan, as the watcher currently sees it.

    This is the one thing on the whole dashboard that is meant to be ACTED ON, and until now it was the
    only part of the system with no screen at all -- the plan went to Telegram and nowhere else, so the
    answer to "what am I supposed to be doing right now" lived in a phone notification you might have
    missed. The Forex page showed S9 instead, which is the sleeve that failed Gate 1 and sends nothing.

    Returns {} when the watcher is switched off, so the card simply does not render.
    """
    from trader.engine.intraday_watch import EVIDENCE, IntradayWatcher, _parse

    cfg = settings.intraday
    if not cfg.enabled or not cfg.symbols:
        return {}
    now = now or datetime.now(timezone.utc)
    watcher = IntradayWatcher(settings, journal, notifier=None, clock=lambda: now)
    day = now.date()
    opens, closes = _parse(cfg.open_utc), _parse(cfg.close_utc)
    pairs = []
    for symbol in cfg.symbols:
        plan = watcher._stored_plan(day, symbol)
        state = watcher._get(day, "state", symbol) or ("waiting" if plan else "none")
        rate = None
        try:
            rate = watcher._rate(symbol)
        except Exception:
            rate = None
        entry = watcher._entry(day, symbol)
        pairs.append({"symbol": symbol, "plan": plan, "state": state, "rate": rate, "entry": entry,
                      "report": watcher.report(day, symbol), "evidence": EVIDENCE.get(symbol)})
    # The session is quoted in UTC everywhere else because that is what the watcher works in, but the
    # person reading this is in Sydney, where 07:00 UTC is 6pm the same evening. A reader should not
    # have to do that arithmetic to know when the next message is due.
    local_open = datetime.combine(day, opens, tzinfo=timezone.utc).astimezone()
    local_close = datetime.combine(day, closes, tzinfo=timezone.utc).astimezone()

    # Most of the day there is no plan: before 07:00 UTC it has not been built, and the setup only
    # appears on about one day in three. An empty card at those times says nothing useful, so it falls
    # back to the last session that did something.
    previous = []
    for symbol in cfg.symbols:
        for back in range(1, 8):
            earlier = day - timedelta(days=back)
            plan = watcher._stored_plan(earlier, symbol)
            if plan is None:
                continue
            previous.append({"symbol": symbol, "day": earlier, "plan": plan,
                             "state": watcher._get(earlier, "state", symbol) or "waiting",
                             "entry": watcher._entry(earlier, symbol)})
            break
    return {"pairs": pairs, "open_utc": cfg.open_utc, "close_utc": cfg.close_utc,
            "open_local": local_open, "close_local": local_close, "previous": previous,
            "weekend": now.weekday() >= 5,
            "in_session": now.weekday() < 5 and opens <= now.time() < closes, "now": now}


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


def operational_health(settings: Settings, journal: Journal, now: datetime) -> list[dict]:
    """The handful of things that fail silently, and whose failure invalidates everything above.

    Each one has already happened here. The alert channel built a null notifier under launchd for seven
    weeks and threw roughly five thousand messages away without a single error. The newest journal
    backup was 34 days old, on the same disk, with Time Machine off. The Mac slept through two entire
    US sessions. None of these announce themselves, and every number on this page is worth less if any
    of them is true right now.
    """
    def age_hours(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds() / 3600
        except ValueError:
            return None

    rows: list[dict] = []

    channel = journal.kv_get("alert_channel") or ""
    broken = channel.startswith(("DISCARDING", "FAILING"))
    rows.append({"name": "Alerts reaching your phone", "ok": bool(channel) and not broken,
                 "detail": channel or "never checked -- the engine has not run since this was added"})

    backups = Path.home() / "Library" / "Application Support" / "PersonalTrader" / "backups"
    newest = max((p.stat().st_mtime for p in backups.glob("*")), default=None) if backups.exists() else None
    if newest is None:
        rows.append({"name": "Journal backup", "ok": False, "detail": "no backup found"})
    else:
        hours = (now.timestamp() - newest) / 3600
        rows.append({"name": "Journal backup", "ok": hours < 48,
                     "detail": f"newest is {hours:.0f}h old" + ("" if hours < 48 else " -- the 90-day "
                               "record Gate 2 needs lives in one file")})

    last_run = age_hours(journal.kv_get(Journal.ENGINE_HEARTBEAT))
    rows.append({"name": "Engine heartbeat", "ok": last_run is not None and last_run < 1.0,
                 "detail": "never run" if last_run is None else f"last run {last_run:.1f}h ago"})

    # The forex watcher gets its own row because it is the one output the user acts on by hand, and
    # because its failure is the least visible on this page: a day with no setup and a day when nothing
    # ran produce exactly the same empty signal list. `irv:last_pass` has no market or network
    # dependency, so its age is the only honest answer to "is the watcher alive".
    last_fx = age_hours(journal.kv_get("irv:last_pass"))
    rows.append({"name": "Forex watcher heartbeat",
                 "ok": last_fx is not None and last_fx < 1.0,
                 "detail": "never run -- no forex message can be arriving" if last_fx is None else
                           f"last pass {last_fx:.1f}h ago" +
                           ("" if last_fx < 1.0 else " -- it runs every minute, so this is a stall")})

    ok, why = (True, "ok")
    try:
        from trader.research.ledger import trial_count, verify_chain
        ok, why = verify_chain()
        counted = sum(trial_count(c) for c in ("S1", "S2", "S3", "S4", "S8"))
        detail = f"{counted} distinct looks at the data, chain intact" if ok else why
    except Exception as exc:                                    # pragma: no cover - defensive
        ok, detail = False, str(exc)
    rows.append({"name": "Trial ledger", "ok": ok, "detail": detail})
    return rows


def _health_card(rows: list[dict]) -> str:
    items = "".join(
        f'<div class="row gap"><span class="pill {"ok" if r["ok"] else "closed"}">'
        f'{"ok" if r["ok"] else "check"}</span><b>{html.escape(r["name"])}</b>'
        f'<span class="spacer"></span><span class="subtle">{html.escape(r["detail"])}</span></div>'
        for r in rows)
    return f'<div class="card">{items}</div>'


def latest_gate2(settings: Settings) -> list[dict]:
    """The most recent Gate 2 verdict per sleeve, newest first.

    Read from `reports/gate2_log.jsonl`, never computed here: Gate 2 re-runs a backtest over the live
    window, which is seconds of work and has no business happening inside a page render. Its own file,
    not the research log -- `latest_research` keys that one by STRATEGY and keeps the newest timestamp,
    so a Gate 2 line written there would quietly replace S8's Gate 1 card.
    """
    log = settings.root / "reports" / "gate2_log.jsonl"
    if not log.exists():
        return []
    newest: dict[str, dict] = {}
    for line in log.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        sleeve = row.get("sleeve")
        if sleeve and row.get("ts", "") >= newest.get(sleeve, {}).get("ts", ""):
            newest[sleeve] = row
    return sorted(newest.values(), key=lambda r: r.get("ts", ""), reverse=True)


def _gate2_card(r: dict) -> str:
    """One forward verdict: is the sleeve that is RUNNING reproducing the one that was tested?

    Gate 1 asks whether a rule worked on history. This asks whether this sleeve, as configured, with
    this broker, on this feed, is doing what that history promised -- which for S8 it is not.
    """
    verdict = r.get("verdict", "?")
    failing = r.get("failing") or []
    waiting = verdict == "NOT ENOUGH EVIDENCE YET"
    good = verdict == "PASS" or (waiting and not failing)
    live, bench = r.get("live_sharpe"), r.get("benchmark_sharpe")
    label = {"PASS": "PASS", "FAIL": "FAIL"}.get(verdict, "WAITING")
    return f"""<div class="card sleeve"><div class="row">
<span class="dot" style="background:{'#199e70' if good else '#d95926'}"></span><b>{html.escape(r.get('sleeve', '?'))}</b>
<span class="pill {'open' if good else 'closed'}">{label}</span>
{f'<span class="pill closed">{html.escape(r["provisional"])}</span>' if waiting and failing else ''}</div>
<div class="big {'up' if good else 'down'}">{'–' if live is None else f'{live:+.2f}'}</div>
<div class="row gap"><span class="subtle">live Sharpe vs benchmark
{'–' if bench is None else f'{bench:+.2f}'}, over the same days</span></div>
<div class="subtle">{r.get('days_live', 0)} of 90 days · {r.get('closed_trades', 0)} of 30 closed trades
{f" · backtest said {r['gate1_oos_sharpe']:+.2f}" if r.get('gate1_oos_sharpe') is not None else ''}</div>
{('<div class="subtle down">fails: ' + html.escape('; '.join(failing)) + '</div>') if failing else
 '<div class="subtle">every check it can answer yet is passing</div>'}
</div>"""


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


def _retire_pill(s: dict) -> str:
    """Say when a sleeve is winding down, because otherwise it looks like it is still working.

    A retiring sleeve shows an equity figure and a return like any other, and nothing on the card would
    tell you it sold everything and will never enter again. That is exactly the sort of thing you should
    not have to read the config to discover.
    """
    if s.get("retired"):
        return '<span class="pill closed">retired · no longer trading</span>'
    if s.get("retiring"):
        return '<span class="pill warn">winding down · selling, no new trades</span>'
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
            f"<td class='why'>{'<span class=\'subtle\'>still open</span>' if t.is_open else html.escape(t.exit_reason or '')}</td></tr>")


def _holding_row(t: Trip, price) -> str:
    """One open position as a dense row: what it is on the left, what it is worth on the right.

    Reuses the instrument-list row rather than inventing a third list style -- the same shape should
    mean the same kind of thing everywhere on the page.
    """
    now = price(t.symbol)
    pnl = t.pnl(now)
    value = (now or 0) * t.buy_qty
    base = t.buy_cost or 1
    return (f'<div class="pair holding"><span class="p-l"><b>{html.escape(t.symbol)}</b>'
            f'<span class="p-sub">{t.sleeve_id} · {t.buy_qty:g} @ {_money(t.entry_price)}</span></span>'
            f'<span class="p-r"><span class="px">{_money(value)}</span>'
            f'<span class="p-chg"><span class="{_tone(pnl)}">{_money(pnl, signed=True)}</span>'
            f'<span class="subtle">{_pct(pnl / base)} since entry</span></span></span></div>')


# A stock list reads better when the ticker has a word under it. We do not carry company names, and
# inventing them would be worse than saying plainly what kind of thing each symbol is.
ASSET_WORDS = {AssetClass.US_EQUITY: "US listed", AssetClass.CRYPTO: "Crypto", AssetClass.FOREX: "Spot FX"}


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
<b>{s['id']}</b><span class="subtle">{html.escape(s['name'])}</span>{'<span class="pill closed">benchmark</span>' if s['benchmark'] else ''}{'<span class="pill open">shadow · no orders</span>' if is_shadow(s.get('broker')) else ''}{_retire_pill(s)}</div>
<div class="big">{_money(s['equity'])}</div>
<div class="row gap"><span class="{_tone(s['return'])}">{_pct(s['return'])}</span>
<span class="subtle">after est. costs {_pct(s['adjusted_return'])}</span></div>
<div class="row gap subtle">{s['invested']:.0%} invested · {s['open']} open · {s['closed']} closed
{'' if s['vs_b0'] is None else f"· <span class='{_tone(s['vs_b0'])}'>{_pct(s['vs_b0'])} vs B0</span>"}</div></div>"""
        for s in d["sleeves"])
    sleeve_rows = "".join(
        f'<div class="pair holding"><span class="p-l">'
        f'<b><i class="sw" style="background:{SHADOW_COLOR if is_shadow(s.get("broker")) else SERIES_COLORS.get(s["id"], "#898781")}"></i>'
        f'{s["id"]}</b><span class="p-sub">{html.escape(s["name"])}</span></span>'
        f'<span class="p-r"><span class="px">{_money(s["equity"])}</span>'
        f'<span class="p-chg"><span class="{_tone(s["return"])}">{_pct(s["return"])}</span>'
        + (f'<span class="subtle">since start</span>' if s["vs_b0"] is None
           else f'<span class="subtle">{_pct(s["vs_b0"])} vs B0</span>')
        + "</span></span></div>" for s in d["sleeves"])
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
    gate2_cards = "".join(_gate2_card(r) for r in d["gate2"]) or \
        "<p class='subtle'>No forward test has been run yet. Run <code>trader gate2 all</code>.</p>"
    health_card = _health_card(d["health"])
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
        f'<div class="wrow" data-sym="{html.escape(w["symbol"])}">'
        f'<span class="wsym">{html.escape(w["symbol"])}</span>'
        f'<span class="wspark" aria-hidden="true"></span>'
        f'<span class="wpx">{format_price(w["price"], w["asset_class"])}</span>'
        + (f'<span class="{"up" if w["change"] >= 0 else "down"}">{w["change"] * 100:+.2f}%</span>'
           if w["change"] is not None else '<span class="subtle">–</span>')
        + "</div>" for w in watch)
    sym_items = ""
    for i, w in enumerate([x for x in watch if x["asset_class"] != AssetClass.FOREX]):
        change = (f'<span class="p-chg"><span class="{"up" if w["change"] >= 0 else "down"}">'
                  f'{w["change"] * 100:+.2f}%</span><span class="subtle">1D</span></span>'
                  ) if w["change"] is not None else ""
        sym_items += (
            f'<button class="pair sym{" on" if i == 0 else ""}" data-pair="{html.escape(w["symbol"])}">'
            f'<span class="p-l"><b>{html.escape(w["symbol"])}</b>'
            f'<span class="p-sub">{html.escape(ASSET_WORDS.get(w["asset_class"], ""))}</span></span>'
            f'<span class="p-r"><span class="px">{_money(w["price"])}</span>{change}</span></button>')
    head_lines = "".join(f'<li class="hl-{tone}">{html.escape(text)}</li>' for tone, text in headline(d))
    blocked = d.get("blocked_today") or []
    blocked_panel = "" if not blocked else (
        '<h2>Markets it wanted and could not take today</h2>'
        '<p class="subtle">These are not errors. They are the difference between the strategy that was '
        'tested and the one actually running, which is worth knowing about.</p>'
        '<div class="card"><ul class="warnings">'
        + "".join(f'<li><b>{html.escape(b["broker"])}</b> &mdash; {html.escape(b["why"])}: '
                  f'{html.escape(b["detail"])}</li>' for b in blocked)
        + '</ul></div>')
    open_trips = [t for t in d["trips"] if t.is_open]
    holdings_rows = "".join(_holding_row(t, d["price"]) for t in open_trips) or \
        '<p class="subtle" style="padding:.5rem .55rem">Nothing is open. The strategies are in cash.</p>'
    fx = d.get("fx") or {}
    if fx.get("rows"):
        shut = "" if fx.get("is_open") else '<span class="pill closed">market shut</span>'
        fx_rows = "".join(
            f"<tr><td>{html.escape(r['symbol'])}</td>"
            f"<td class='num'>{format_price(r['price'], AssetClass.FOREX) if r['price'] else '–'}</td>"
            f"<td class='num'>{(format(r['qty'], 'g') if r['qty'] else '–')}</td>"
            f"<td class='num'>{format_price(r['stop'], AssetClass.FOREX) if r.get('stop') else '–'}</td></tr>"
            for r in fx["rows"])
        pair_items = ""
        for i, r in enumerate(fx["rows"]):
            series = (fx.get("series") or {}).get(r["symbol"]) or []
            change = ""
            if len(series) > 2:
                first, last = series[-22][1], series[-1][1]     # ~one month of trading days
                pct = (last / first - 1) * 100 if first else 0.0
                change = (f'<span class="p-chg"><span class="{"up" if pct >= 0 else "down"}">'
                          f'{pct:+.2f}%</span><span class="subtle">1M</span></span>')
            held = f'<span class="pill open">holding {r["qty"]:g}</span>' if r["qty"] else ""
            note = ("stop " + format_price(r["stop"], AssetClass.FOREX)) if r.get("stop") else "no position"
            pair_items += (
                f'<button class="pair{" on" if i == 0 else ""}" data-pair="{html.escape(r["symbol"])}">'
                f'<span class="p-l"><b>{html.escape(r["symbol"])}</b>'
                f'<span class="p-sub">{html.escape(note)}</span></span>'
                f'<span class="p-r"><span class="px">'
                f'{format_price(r["price"], AssetClass.FOREX) if r["price"] else "–"}</span>{change}{held}</span>'
                f'</button>')
        fx_ids = set(fx["sleeves"])
        plans = "".join(
            fx_plan(r, [t for t in d["trips"] if t.symbol == r["symbol"] and t.sleeve_id in fx_ids],
                    d["price"])
            for r in fx["rows"])
        v = fx.get("verdict") or {}
        verdict_note = ("" if not v else
            f'<p class="fxverdict">Gate 1 verdict for these rules: <b class="{"up" if v.get("verdict") == "PASS" else "down"}">'
            f'{html.escape(str(v.get("verdict")))}</b> — out-of-sample Sharpe {v.get("oos_sharpe")} against cash. '
            + ("" if v.get("verdict") == "PASS" else
               "A negative number means these rules lost to leaving the money in T-bills, so this sleeve "
               "sends nothing to any broker and its signals are not advice to act on.") + "</p>")
        fx_card = f"""{d.get('intraday', '')}
<h2>Forex {shut}</h2>
<p class="subtle">{html.escape(", ".join(fx["sleeves"]))} trades these on live prices with
<b>no orders sent anywhere</b>. Daily closes with the latest quote on the end; the feed is free and lags
a few minutes.</p>
{verdict_note}
<div class="fxlayout">
<div class="pairlist">{pair_items}</div>
<div class="fxright">
<div class="card chartcard fxchart"><div class="row"><div>
<div class="label" id="fx-title">&nbsp;</div><div class="subtle" id="fx-sub">&nbsp;</div></div>
<div class="spacer"></div><div class="ranges" data-for="forex">
<button data-r="1D">1D</button><button data-r="5D">5D</button><button data-r="1W">1W</button>
<button data-r="1M">1M</button><button data-r="3M" class="on">3M</button>
<button data-r="1Y">1Y</button><button data-r="All">All</button>
</div></div>
<div class="chart" id="fx-chart" aria-label="Exchange rate over time"></div></div>
<div id="fx-plans">{plans}</div>
</div>"""
    else:
        fx_card = ""
    risk_class = {"Low risk": "ok", "Elevated risk": "warn"}.get(d["risk_level"], "bad")
    total_base = d["start_equity"] or 1
    chart_data = json.dumps({"account": d["charts"]["account"], "sleeves": d["charts"]["sleeves"],
                             "shadow": d["charts"]["shadow"], "colors": {**SERIES_COLORS, "shadow": SHADOW_COLOR},
                             "labels": BROKER_LABELS, "fx": (d.get("fx") or {}).get("series", {}),
                             "equities": d.get("equities", {})})
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
<button id="theme-toggle" class="icon-btn" type="button" title="Switch between light and dark"
 aria-label="Switch between light and dark"><span class="t-sun" aria-hidden="true">☀</span><span class="t-moon" aria-hidden="true">☾</span></button>
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
<div class="grid holdings-row">
<div class="card"><div class="row"><div class="label">Open positions</div><div class="spacer"></div>
<span class="subtle">{len(open_trips)} held · valued at the latest price</span></div>
<div class="holdlist">{holdings_rows}</div></div>
<div class="card"><div class="row"><div class="label">Strategies</div><div class="spacer"></div>
<span class="subtle">each starts at $10,000</span></div>
<div class="holdlist">{sleeve_rows}</div></div></div>
{blocked_panel}
<h2>Trading objectives</h2><div class="tiles">{tiles}</div>
</section>
<section data-panel="stocks" hidden>
<h2>Price charts</h2>
<p class="subtle">Everything the enabled strategies trade. Daily candles: the wick is the day's high and
low, which is what a stop is measured against.</p>
<div class="fxlayout">
<div class="symlist">{sym_items}</div>
<div class="card chartcard fxchart"><div class="row"><div>
<div class="label" id="eq-title">&nbsp;</div><div class="subtle" id="eq-sub">&nbsp;</div></div>
<div class="spacer"></div><div class="ranges" data-for="stocks">
<button data-r="1D">1D</button><button data-r="5D">5D</button><button data-r="1W">1W</button>
<button data-r="1M">1M</button><button data-r="3M" class="on">3M</button>
<button data-r="1Y">1Y</button><button data-r="All">All</button>
</div></div>
<div class="chart" id="eq-chart" aria-label="Price over time"></div></div>
</div>
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
<h2>Can you believe any of this?</h2>
<p class="subtle">The things that fail silently. Every one has happened here: the alert channel threw
away five thousand messages without a single error, the newest backup was 34 days old on the same disk,
the Mac slept through two whole US sessions. Nothing below is worth much while anything here says
&ldquo;check&rdquo;.</p>
{health_card}
<h2>Is what's running doing what was tested?</h2>
<p class="subtle">Gate 2, the forward test. Gate 1 below asks whether a rule worked on history; this asks
whether this sleeve, as configured, with this broker, on this data feed, is reproducing it. The
benchmark is measured over the sleeve's own days and no others &mdash; comparing life-to-date returns
between sleeves that started on different dates is not a comparison. WAITING means too few days or too
few closed trades, never a bad result; where it would already fail, it says so rather than implying
that waiting might rescue it.</p>
<div class="grid sleeves">{gate2_cards}</div>
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
/* ===================================================================================================
   Personal Trader — interface styles.

   Four rules, taken from how trading and fintech interfaces are actually built, and each one is a
   decision rather than a preference:

   1. NUMERALS ARE THE TYPOGRAPHY THAT MATTERS. Every figure is tabular, so digits occupy a fixed width
      and a column of prices stays aligned as it updates. Proportional figures make a live price jitter
      sideways on every tick, which reads as instability in the data rather than in the font.
   2. COLOUR MEANS STATE, NOTHING ELSE. Green and red are reserved for profit and loss. They are never
      used for decoration, so when something is green it always means the same thing. Measured, green
      against red is only 1.52:1 in contrast, so colour alone cannot carry the meaning for the ~8% of men
      with red-green colour deficiency: every figure also carries a + or - sign, and that sign is the
      real signal. Colour is a second channel on top of it, never the only one.
   3. ELEVATION COMES FROM SURFACE, NOT SHADOW. On a dark background a shadow is nearly invisible, so
      depth is built from flat steps of lightness. There are no gradients in any area that shows data:
      a gradient behind a number changes the contrast of that number across its own width.
   4. SHOW THE EXCEPTION. Space goes to what needs attention. Everything that is simply fine stays quiet.

   Contrast was checked rather than eyeballed: against both the page and the card surface, every text
   token clears WCAG AA, and most clear AAA.
   =================================================================================================== */

:root{
  /* Surfaces, as explicit elevation steps. Neutral and near-black: a warm background fights red, and
     red has to stay unambiguous. */
  --page:#0a0a0b; --s1:#101012; --s2:#16161a; --s3:#1d1d22; --s4:#26262d;
  --line:#23232a; --line2:#2f2f38;
  /* Text */
  --ink:#f2f2f4; --ink2:#b6b6c0; --muted:#8a8a96;
  /* State. Only these three carry meaning. */
  --up:#30d158; --down:#ff5a4e; --warn:#ffd60a;
  --up-bg:#0e2a18; --up-line:#1d5133; --down-bg:#2d1210; --down-line:#5e2420;
  --warn-bg:#2a2109; --warn-line:#5a4410;
  /* Brand. Deliberately not green or red, so it can never be mistaken for a result. */
  --accent:#f0a830; --accent-ink:#17170f; --accent-bg:#2a1f0a; --accent-line:#4a360e;
  /* Type scale */
  --t-xs:11px; --t-sm:12px; --t-md:13px; --t-base:14px; --t-lg:16px; --t-xl:20px; --t-2xl:28px; --t-hero:40px;
  --radius:10px; --radius-lg:14px; --radius-sm:7px;
  --rail:236px; --rail-min:62px; --panel:400px;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--page); color:var(--ink); min-height:100vh;
  font:var(--t-base)/1.5 var(--sans);
  /* Tabular figures everywhere. See rule 1. */
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1,"cv05" 1;
  -webkit-font-smoothing:antialiased; padding-left:var(--rail);
}
::selection{background:var(--accent);color:var(--accent-ink)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--s4);border-radius:6px;border:3px solid var(--page)}
::-webkit-scrollbar-thumb:hover{background:var(--line2)}
::-webkit-scrollbar-track{background:transparent}

/* --- type ---------------------------------------------------------------------------------------- */
h1{font-size:var(--t-xl);margin:0;font-weight:600;letter-spacing:-.01em}
h2{font-size:var(--t-md);font-weight:600;margin:30px 0 12px;color:var(--ink);
   letter-spacing:.04em;text-transform:uppercase}
h2 .pill{text-transform:none;letter-spacing:0}
p{margin:.5rem 0}
code{font-family:var(--mono);font-size:.92em;background:var(--s2);padding:.1em .35em;border-radius:4px;color:var(--ink2)}
.subtle{color:var(--muted);font-size:var(--t-sm);line-height:1.55}
.label{color:var(--muted);font-size:var(--t-xs);margin-bottom:5px;letter-spacing:.05em;text-transform:uppercase;font-weight:500}
.spacer{flex:1}
.mono{font-family:var(--mono);color:var(--ink2)}
.sym{font-weight:600;letter-spacing:-.01em}
/* The sign is the signal; colour is the second channel. See rule 2. */
.up{color:var(--up)} .down{color:var(--down)} p.bad{color:var(--down)}
.hero{font-size:var(--t-hero);font-weight:600;letter-spacing:-.025em;line-height:1.08;margin:4px 0 6px}
.big{font-size:var(--t-2xl);font-weight:600;letter-spacing:-.02em;margin:6px 0 2px;line-height:1.15}

/* --- header -------------------------------------------------------------------------------------- */
header{display:flex;align-items:center;gap:16px;padding:16px 24px;border-bottom:1px solid var(--line);
  flex-wrap:wrap;background:var(--page);position:sticky;top:0;z-index:30}
.brand{width:34px;height:34px;border-radius:var(--radius);background:var(--s2);border:1px solid var(--line);
  display:grid;place-items:center;font-weight:700;color:var(--accent);font-size:var(--t-sm);flex:0 0 auto}
.account{text-align:right;line-height:1.4}
.account b{font-weight:600}
.tabs{display:flex;gap:22px;padding:0 24px;border-bottom:1px solid var(--line)}
.tab{background:none;border:0;color:var(--muted);padding:14px 0;cursor:pointer;font:inherit;
  border-bottom:2px solid transparent}
.tab.active{color:var(--ink);border-bottom-color:var(--accent)}
main{padding:4px 24px 56px;max-width:1500px;margin:0 auto}

/* --- the left rail ------------------------------------------------------------------------------- */
.rail{position:fixed;left:0;top:0;bottom:0;width:var(--rail);background:var(--s1);
  border-right:1px solid var(--line);display:flex;flex-direction:column;gap:1px;
  padding:12px 10px;z-index:50;overflow-y:auto;overscroll-behavior:contain}
.rail-top{display:flex;align-items:center;gap:10px;padding:2px 4px 14px}
.rail-top button{background:none;border:0;color:var(--muted);cursor:pointer;padding:4px;
  border-radius:var(--radius-sm);display:flex}
.rail-top button:hover{background:var(--s3);color:var(--ink)}
.rail-brand{width:28px;height:28px;border-radius:8px;background:var(--accent);color:var(--accent-ink);
  font-weight:700;display:flex;align-items:center;justify-content:center;font-size:var(--t-xs);flex:0 0 auto}
.rail-name{font-size:var(--t-md);color:var(--ink);font-weight:600;white-space:nowrap;letter-spacing:-.01em}
.rail-group{font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
  padding:16px 8px 6px;white-space:nowrap;font-weight:600}
.rail-btn{width:100%;border:0;border-radius:var(--radius-sm);background:none;color:var(--ink2);cursor:pointer;
  display:flex;align-items:center;gap:11px;padding:.5rem;font:inherit;font-size:var(--t-md);text-align:left;
  position:relative}
.rail-btn svg{flex:0 0 auto;opacity:.75}
.rail-btn span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail-btn .count{margin-left:auto;font-style:normal;font-size:var(--t-xs);color:var(--muted);
  background:var(--s3);border-radius:999px;padding:.1rem .45rem;font-variant-numeric:tabular-nums}
.rail-btn:hover{background:var(--s2);color:var(--ink)}
.rail-btn:hover svg{opacity:1}
.rail-btn.active{background:var(--s3);color:var(--ink);font-weight:500}
.rail-btn.active svg{opacity:1;color:var(--accent)}
/* A bar, not just a colour: the active row stays identifiable without relying on hue. */
.rail-btn.active::before{content:"";position:absolute;left:-10px;top:6px;bottom:6px;width:3px;
  background:var(--accent);border-radius:0 3px 3px 0}
.dot-warn{width:6px;height:6px;border-radius:50%;background:var(--warn);margin-left:auto;flex:0 0 auto}
.watchlist{display:flex;flex-direction:column;gap:0;overflow-y:auto}
.wrow{display:grid;grid-template-columns:1fr auto;gap:0 10px;padding:.32rem .5rem;
  border-radius:var(--radius-sm);font-size:var(--t-sm);align-items:baseline}
.wrow:hover{background:var(--s2)}
.wsym{color:var(--ink2);font-weight:500}
.wpx{color:var(--ink);text-align:right;font-variant-numeric:tabular-nums;font-size:var(--t-sm)}
.wrow .up,.wrow .down,.wrow .subtle{grid-column:2;text-align:right;font-size:var(--t-xs)}
/* Collapsed: the same rail with a modifier, never a second layout to keep in step. */
body.rail-min .rail{width:var(--rail-min);align-items:center;padding-left:0;padding-right:0}
body.rail-min .rail-btn{width:40px;height:40px;justify-content:center;padding:0;gap:0}
body.rail-min .rail-btn span,body.rail-min .rail-btn .count,body.rail-min .rail-name,
body.rail-min .rail-group,body.rail-min .watchlist{display:none}
body.rail-min .rail-btn.active::before{left:0}
body.rail-min{padding-left:62px}

/* --- search -------------------------------------------------------------------------------------- */
#searchbar{position:fixed;left:var(--rail-min);right:0;top:0;z-index:45;padding:10px 24px;
  background:var(--s1);border-bottom:1px solid var(--line);display:none}
#searchbar.on{display:block}
#searchbar input{width:100%;max-width:560px;background:var(--s2);border:1px solid var(--line2);
  color:var(--ink);border-radius:var(--radius-sm);padding:.55rem .8rem;font:inherit}
#searchbar input:focus{outline:none;border-color:var(--accent)}
tr.hide{display:none}

/* --- the headline: the exception-first summary ---------------------------------------------------- */
ul.headline{list-style:none;margin:18px 0 24px;padding:0;display:flex;flex-direction:column;gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:var(--radius-lg);overflow:hidden}
ul.headline li{padding:.7rem 1rem .7rem 2.1rem;background:var(--s1);font-size:var(--t-md);
  line-height:1.5;color:var(--ink2);position:relative}
/* A glyph as well as a colour, so the status survives a greyscale screenshot or colourblind vision. */
ul.headline li::before{position:absolute;left:.95rem;top:.7rem;font-weight:700;font-size:var(--t-md)}
ul.headline li.hl-good{color:var(--ink)} ul.headline li.hl-good::before{content:"✓";color:var(--up)}
ul.headline li.hl-warn{color:var(--ink)} ul.headline li.hl-warn::before{content:"!";color:var(--warn)}
ul.headline li.hl-bad{color:var(--ink);background:var(--down-bg)}
ul.headline li.hl-bad::before{content:"×";color:var(--down)}
ul.headline li.hl-plain::before{content:"·";color:var(--muted)}
.label.help{border-bottom:1px dotted var(--line2);cursor:help;display:inline-block}

/* --- cards and grids ----------------------------------------------------------------------------- */
.card{background:var(--s1);border:1px solid var(--line);border-radius:var(--radius-lg);padding:18px}
.grid{display:grid;gap:14px}
.grid.top{grid-template-columns:minmax(0,2fr) minmax(290px,1fr);margin-top:18px}
.grid.sleeves{grid-template-columns:repeat(auto-fit,minmax(265px,1fr));margin-bottom:14px}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.row.gap{margin-top:6px;gap:14px}
.breakdown{display:flex;gap:16px;flex-wrap:wrap;color:var(--ink2);font-size:var(--t-sm);margin-top:10px}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.legend{display:flex;gap:18px;color:var(--ink2);font-size:var(--t-sm);margin-top:10px;flex-wrap:wrap}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}

/* --- range switch -------------------------------------------------------------------------------- */
.ranges{display:flex;gap:2px;background:var(--page);border:1px solid var(--line);
  border-radius:var(--radius-sm);padding:3px}
.ranges button{background:none;border:0;color:var(--muted);font:inherit;font-size:var(--t-sm);
  padding:4px 9px;border-radius:5px;cursor:pointer}
.ranges button:hover{color:var(--ink)}
.ranges button.on{background:var(--s3);color:var(--ink);font-weight:500}

/* --- charts -------------------------------------------------------------------------------------- */
.chart{position:relative;height:270px;margin-top:14px}
.chart svg{width:100%;height:100%;display:block;overflow:visible}
.chart text{fill:var(--muted);font-size:var(--t-xs);font-variant-numeric:tabular-nums}
.chart .end{fill:var(--ink);font-weight:600;font-size:var(--t-sm)}
.fxchart .chart{height:400px}

/* --- the side card: limits and meters ------------------------------------------------------------ */
.side .split{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0;padding:14px 0;
  border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.meter{margin:14px 0}
.track{height:5px;border-radius:3px;background:var(--s3);overflow:hidden}
.fill{height:100%;background:var(--up);border-radius:3px;transition:width .3s ease}
.fill.warn{background:var(--warn)} .fill.bad{background:var(--down)}
dl{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin:18px 0 0}
dt{color:var(--muted);font-size:var(--t-xs);letter-spacing:.04em;text-transform:uppercase}
dd{margin:3px 0 0;font-weight:600;font-size:var(--t-lg)}

/* --- tiles --------------------------------------------------------------------------------------- */
/* Four across, two rows. auto-fit fitted seven and left the eighth stranded on a row of its own,
   and it also squeezed the labels until "Average winning trade" wrapped to two lines. */
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:var(--line);
  border:1px solid var(--line);border-radius:var(--radius-lg);overflow:hidden;gap:1px}
@media (max-width:1200px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:560px){.tiles{grid-template-columns:1fr}}
.tile{padding:14px 16px;background:var(--s1)}
.tile .value{font-size:var(--t-xl);font-weight:600;margin:3px 0;letter-spacing:-.02em}

/* --- sleeve cards -------------------------------------------------------------------------------- */
.sleeve .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.sleeve b{font-size:var(--t-base)}

/* --- pills --------------------------------------------------------------------------------------- */
.pill{display:inline-block;font-size:var(--t-xs);padding:2px 8px;border-radius:999px;
  border:1px solid var(--line2);color:var(--ink2);font-weight:500;white-space:nowrap}
.pill.ok{color:var(--up);border-color:var(--up-line);background:var(--up-bg)}
.pill.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.pill.bad{color:var(--down);border-color:var(--down-line);background:var(--down-bg)}
.pill.buy{color:var(--up);background:var(--up-bg);border-color:var(--up-line)}
.pill.sell{color:var(--down);background:var(--down-bg);border-color:var(--down-line)}
.pill.open{color:var(--accent);border-color:var(--accent-line);background:var(--accent-bg)}
.pill.closed{color:var(--muted)}

/* --- tables -------------------------------------------------------------------------------------- */
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:var(--t-md);white-space:nowrap}
th{text-align:left;color:var(--muted);font-weight:500;font-size:var(--t-xs);padding:9px 10px;
  border-bottom:1px solid var(--line);letter-spacing:.05em;text-transform:uppercase;
  position:sticky;top:0;background:var(--s1);z-index:1}
td{padding:9px 10px;border-bottom:1px solid var(--line)}
tbody tr:last-child td,table tr:last-child td{border-bottom:0}
table tr:hover td{background:var(--s2)}
/* Money is right-aligned so decimal points line up down the column and magnitudes compare at a glance. */
td.num{text-align:right;font-variant-numeric:tabular-nums}
th:nth-child(n+6){text-align:left}
.why{color:var(--muted);max-width:280px;overflow:hidden;text-overflow:ellipsis}
.search{width:100%;max-width:330px;background:var(--s2);border:1px solid var(--line2);color:var(--ink);
  border-radius:var(--radius-sm);padding:8px 11px;font:inherit;margin-bottom:12px}
.search:focus{outline:none;border-color:var(--accent)}
.warnings{margin:0;padding-left:18px;color:var(--ink2)}
.warnings li{margin:7px 0}

/* --- the instrument list beside a chart -----------------------------------------------------------
   Flat rows inside one bordered container, rather than a stack of individual cards. A card per symbol
   spends ~76px of height on borders and padding to show two numbers, which fits four symbols in the
   column; these rows fit about fourteen. Density is the point of a list like this -- you are meant to
   scan a market, and a market you have to scroll is a market you do not check. */
.fxlayout{display:grid;grid-template-columns:260px 1fr;gap:14px;align-items:start}
.pairlist,.symlist{display:flex;flex-direction:column;gap:1px;max-height:560px;overflow-y:auto;
  background:var(--s1);border:1px solid var(--line);border-radius:var(--radius-lg);padding:5px}
.pair,.sym{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;
  text-align:left;background:none;border:0;border-radius:var(--radius-sm);padding:.45rem .55rem;
  cursor:pointer;color:var(--ink2);font:inherit;position:relative}
.pair:hover,.sym:hover{background:var(--s2)}
.pair.on,.sym.on{background:var(--s3)}
/* A bar marks the selected row. Background alone is too quiet at this density. */
.pair.on::before,.sym.on::before{content:"";position:absolute;left:-5px;top:4px;bottom:4px;width:2px;
  background:var(--accent);border-radius:0 2px 2px 0}
.p-l{display:flex;flex-direction:column;min-width:0}
.p-r{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.pair b,.sym b{color:var(--ink);font-size:var(--t-md);font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.p-sub{font-size:var(--t-xs);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pair .px,.sym .px{font-size:var(--t-md);font-weight:600;color:var(--ink);letter-spacing:-.01em}
.p-r .up,.p-r .down,.p-r .subtle{font-size:var(--t-xs)}
.p-chg{display:flex;align-items:baseline;gap:5px}

/* --- the per-pair trade plan ---------------------------------------------------------------------
   Prose, not a table, because the question it answers ("what did it pay, when will it sell") is a
   sentence. The numbers inside it are still tabular and still carry their units. */
.fxright{display:flex;flex-direction:column;gap:14px;min-width:0}
#fx-plans>[data-plan][hidden]{display:none}
.plan{background:var(--s1);border:1px solid var(--line);border-radius:var(--radius-lg);padding:16px 18px}
.plan-hist{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}

/* The stop ladder: left end is the level that ends the trade, so the room left is a distance you can
   see. Borrowed from the loss-limit bars in prop-firm dashboards. */
.ladder{margin:14px 0 4px}
.ladder-track{position:relative;height:8px;border-radius:5px;background:var(--s3);
  border:1px solid var(--line)}
.ladder-fill{position:absolute;left:0;top:0;bottom:0;border-radius:4px;opacity:.35}
.ladder-fill.up{background:var(--up)} .ladder-fill.down{background:var(--down)}
.ladder-mark{position:absolute;top:-4px;width:2px;height:16px;border-radius:1px;transform:translateX(-1px)}
.ladder-mark.entry{background:var(--ink2)}
.ladder-mark.now{width:3px;height:18px;top:-5px}
.ladder-mark.now.up{background:var(--up)} .ladder-mark.now.down{background:var(--down)}
.ladder-ends{display:flex;gap:10px;margin-top:7px;font-size:var(--t-xs)}

/* Each entry condition as a card: what it needs, what it is, and a pill for the verdict. */
.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:10px;margin:10px 0 0}
.rule{background:var(--s2);border:1px solid var(--line);border-radius:var(--radius);padding:11px 13px}
.rule-head{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.rule-head .label{margin:0;text-transform:none;letter-spacing:0;font-size:var(--t-sm);color:var(--ink2);
  flex:1;line-height:1.3}
.rule-vals{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.rule-vals dt{color:var(--muted);font-size:10px;letter-spacing:.05em;text-transform:uppercase}
.rule-vals dd{margin:2px 0 0;font-weight:600;font-size:var(--t-md)}

/* What it would do right now, as figures rather than a sentence. */
.would{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));gap:12px;margin-top:8px;
  background:var(--s2);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px}
.would dt{color:var(--muted);font-size:10px;letter-spacing:.05em;text-transform:uppercase}
.would dd{margin:3px 0 0;font-weight:600;font-size:var(--t-lg);letter-spacing:-.01em}
.would dd .subtle{font-weight:400;font-size:var(--t-sm)}
/* The Gate 1 verdict is a warning, not a footnote: full width, above everything it qualifies. */
.fxverdict{background:var(--down-bg);border:1px solid var(--down-line);border-radius:var(--radius);
  padding:10px 14px;margin:0 0 14px;font-size:var(--t-sm);line-height:1.55;color:var(--ink2)}
.fxverdict b{color:var(--down)}
.histtable{margin-top:8px;font-size:var(--t-sm)}
.histtable th{position:static}
.plan-head{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.plan-head b{font-size:var(--t-lg);letter-spacing:-.01em}
.plan-rows{display:grid;grid-template-columns:1fr;gap:0;margin:0 0 10px}
.plan-rows>div{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:baseline;
  padding:.4rem 0;border-bottom:1px solid var(--line)}
.plan-rows>div:last-child{border-bottom:0}
.plan-rows dt{color:var(--muted);font-size:var(--t-sm);letter-spacing:0;text-transform:none}
.plan-rows dd{margin:0;text-align:right;font-weight:600;font-size:var(--t-md)}
.plan-rows dd .subtle{font-weight:400}
.plan-note{font-size:var(--t-sm);color:var(--ink2);line-height:1.55;margin:.55rem 0 0}
.plan-note b{color:var(--ink)}
@media (max-width:760px){.plans{grid-template-columns:1fr}}
/* The two landing-page panels: what you hold, and how each strategy is doing. Every reference design
   puts these on the front page, and this dashboard had them a tab away in a 13-column table. */
.grid.holdings-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr);margin-top:14px}
.holdlist{display:flex;flex-direction:column;gap:1px;margin-top:10px;max-height:300px;overflow-y:auto}
.pair.holding{cursor:default}
.pair.holding:hover{background:var(--s2)}
.pair.holding b{display:flex;align-items:center;gap:7px}
.pair.holding .sw{margin:0;width:8px;height:8px;border-radius:50%}
@media (max-width:900px){.grid.holdings-row{grid-template-columns:1fr}}
.p-r .pill{font-size:10px;padding:1px 6px;margin-top:1px}
@media (max-width:900px){.fxlayout{grid-template-columns:1fr}.pairlist,.symlist{max-height:none}}

/* The chart's own header: the instrument named like a headline, its numbers under it. A 12px
   all-caps label was right for "ACCOUNT BALANCE" and wrong for "EUR/USD", which is the subject of
   the whole panel. */
#fx-title,#eq-title{font-size:var(--t-xl);color:var(--ink);text-transform:none;letter-spacing:-.02em;
  font-weight:600;margin-bottom:1px}
#fx-sub,#eq-sub{font-size:var(--t-md)}

/* --- Claude lives beside the data, not on its own page -------------------------------------------- */
#claude-panel{position:fixed;top:0;right:0;width:var(--panel);height:100vh;z-index:40;display:flex;
  flex-direction:column;background:var(--s1);border-left:1px solid var(--line)}
#claude-panel[hidden]{display:none}
body.claude-open main,body.claude-open header,body.claude-open nav.tabs{margin-right:var(--panel)}
.ap-head{display:flex;align-items:center;gap:8px;padding:15px 16px;border-bottom:1px solid var(--line)}
.ap-head button{background:none;border:0;color:var(--muted);font-size:var(--t-md);cursor:pointer;
  padding:3px 9px;border-radius:var(--radius-sm)}
.ap-head button:hover{background:var(--s3);color:var(--ink)}
.ap-foot{padding:12px 14px 14px;border-top:1px solid var(--line)}
.ap-note{font-size:var(--t-xs);color:var(--muted);line-height:1.5;margin:.65rem 0 0}
#claude-toggle{background:var(--s2);border:1px solid var(--line2);color:var(--ink2);border-radius:999px;
  padding:.38rem .95rem;font:inherit;font-size:var(--t-md);cursor:pointer;white-space:nowrap}
#claude-toggle:hover{border-color:var(--accent);color:var(--ink)}
body.claude-open #claude-toggle{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);
  font-weight:500}
.chatlog{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:.55rem;padding:14px}
.msg{padding:.55rem .8rem;border-radius:var(--radius);white-space:pre-wrap;line-height:1.55;
  max-width:94%;font-size:var(--t-md)}
.msg.you{align-self:flex-end;background:var(--s3);color:var(--ink)}
.msg.claude{align-self:flex-start;background:var(--s2);border:1px solid var(--line);color:var(--ink)}
.msg.tool{align-self:flex-start;font-size:var(--t-xs);color:var(--muted);font-family:var(--mono);
  padding:.2rem .5rem}
.msg.err{align-self:flex-start;background:var(--down-bg);border:1px solid var(--down-line);
  color:var(--down);font-size:var(--t-md)}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:.55rem}
.chip{background:var(--s2);border:1px solid var(--line);color:var(--ink2);border-radius:999px;
  padding:.28rem .65rem;font:inherit;font-size:var(--t-xs);cursor:pointer;text-align:left}
.chip:hover{border-color:var(--line2);color:var(--ink);background:var(--s3)}
#chat-form{display:flex;gap:.5rem}
#chat-input{flex:1;margin:0;background:var(--s2);border:1px solid var(--line2);color:var(--ink);
  border-radius:var(--radius-sm);padding:.55rem .75rem;font:inherit;font-size:var(--t-md)}
#chat-input:focus{outline:none;border-color:var(--accent)}
#chat-send{background:var(--accent);color:var(--accent-ink);border:0;border-radius:var(--radius-sm);
  padding:.55rem .95rem;font:inherit;font-weight:600;cursor:pointer}
#chat-send:disabled{opacity:.45;cursor:default}

/* --- tooltip ------------------------------------------------------------------------------------- */
.tip{position:fixed;pointer-events:none;background:var(--s3);border:1px solid var(--line2);
  border-radius:var(--radius);padding:9px 11px;font-size:var(--t-sm);z-index:60;max-width:300px;
  white-space:normal;line-height:1.5;box-shadow:0 10px 30px rgba(0,0,0,.6)}
.tip i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}

/* --- narrow screens ------------------------------------------------------------------------------ */
@media (max-width:1100px){
  #claude-panel{width:100%}
  body.claude-open main,body.claude-open header,body.claude-open nav.tabs{margin-right:0}
}
@media (max-width:900px){
  .grid.top{grid-template-columns:1fr}
  header,main,.tabs{padding-left:16px;padding-right:16px}
  .hero{font-size:32px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

# --- the light theme ------------------------------------------------------------------------------
# One set of values, written once and used twice: the OS-preference block and the explicit override
# need identical colours, and two copies of a palette is exactly the drift this file already warns
# about elsewhere.
#
# The four rules above still hold. Surfaces are neutral steps of lightness (white card, grey page) so
# elevation still comes from surface rather than shadow, and the state colours are darkened until they
# carry on white: measured, every token clears WCAG AA against BOTH the page and the card, and --ink
# and --ink2 clear AAA. Green against red is 1.05:1 here -- even worse than the dark theme's 1.52 --
# which is the whole reason every figure carries its own + or - sign and every headline its glyph.
_LIGHT = """
  --page:#f7f7f8; --s1:#ffffff; --s2:#f2f2f4; --s3:#e9e9ec; --s4:#dededf;
  --line:#e4e4e8; --line2:#d2d2d8;
  --ink:#17171a; --ink2:#4a4a55; --muted:#6b6b78;
  --up:#137a3a; --down:#c42a1e; --warn:#8a6200;
  --up-bg:#eaf7ef; --up-line:#b5e2c6; --down-bg:#fdeeec; --down-line:#f6c4bf;
  --warn-bg:#fdf6e3; --warn-line:#e8d08a;
  --accent:#9a5b06; --accent-ink:#ffffff; --accent-bg:#fdf3e3; --accent-line:#edcf9a;
"""

# Dark stays the default in :root, so a page that never runs the toggle looks exactly as it did. The
# OS preference is honoured only when the reader has not chosen for themselves -- an explicit
# data-theme="dark" must win over a light Mac, or the toggle would appear not to work.
CSS += (
    "@media (prefers-color-scheme: light){:root:not([data-theme=\"dark\"]){" + _LIGHT + "}}\n"
    ":root[data-theme=\"light\"]{" + _LIGHT + "}\n"
    # Only the chrome fades; a number must never animate its colour, because a figure caught
    # mid-transition is briefly the wrong colour for its sign.
    "body,.card,.rail,.pill{transition:background-color .15s ease,border-color .15s ease}\n"
    # The theme control. `Ask Claude` is deliberately a ghost button that turns accent only while the
    # panel is open (body.claude-open), so it is left alone -- that is a state model, not an oversight.
    ".icon-btn{display:inline-grid;place-items:center;width:30px;height:30px;padding:0;cursor:pointer;"
    "background:var(--s2);color:var(--ink2);border:1px solid var(--line);border-radius:var(--radius-sm);"
    "font-size:14px;line-height:1}\n"
    ".icon-btn:hover{background:var(--s3);color:var(--ink);border-color:var(--line2)}\n"
    # The hand-executed plan. Laid out as cards because each pair is a separate decision.
    ".irv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:var(--gap,14px);"
    "margin:0 0 22px}\n"
    ".irv .irv-head{margin:.1rem 0 .6rem;font-size:var(--t-md);color:var(--ink2)}\n"
    ".irv .irv-now{margin:.55rem 0 0;font-size:var(--t-md);color:var(--ink2)}\n"
    # What you actually did, distinct from the system's own quote above it -- never a replacement for
    # it, because the two can and do disagree (a fill a touch above the mid, a ticket typed late).
    ".irv .irv-reported{margin:.35rem 0 0;font-size:var(--t-md);color:var(--ink2)}\n"
    ".irv .irv-reported.subtle{color:var(--muted)}\n"
    # Rule 4: space goes to what needs attention. A live trade is the only state you can still get
    # wrong, so it is the only one that raises its voice -- a left edge, not a colour wash, because a
    # wash behind these figures is the gradient-under-a-number problem rule 3 exists to prevent.
    ".irv-in{border-left:3px solid var(--warn)}\n"
    ".irv-waiting{border-left:3px solid var(--line2)}\n"
    # A month of shape beside each watchlist price. A row of figures says where a market IS; it takes
    # a line to say where it has BEEN, and that is the thing you cannot get from a single number.
    # aria-hidden: the price and the signed percent beside it already say this in text.
    ".wspark{display:block;width:46px;height:16px;opacity:.75}\n"
    ".wspark svg{display:block;overflow:visible}\n"
    # The row was two columns with the percent wrapping under the price. The sparkline needs its own
    # column and spans both rows, so it sits level with the pair rather than with either figure.
    ".wrow{grid-template-columns:1fr auto auto}\n"
    ".wsym{grid-column:1;grid-row:1}\n"
    ".wspark{grid-column:2;grid-row:1/3;align-self:center}\n"
    ".wpx{grid-column:3;grid-row:1}\n"
    ".wrow>span:last-child{grid-column:3;grid-row:2;justify-self:end}\n"
    # One glyph per theme, swapped by the attribute rather than by script, so the button is already
    # correct on first paint instead of flickering to the right icon after load.
    ".t-moon{display:none}.t-sun{display:block}\n"
    "@media (prefers-color-scheme: light){:root:not([data-theme=\"dark\"]) .t-sun{display:none}"
    ":root:not([data-theme=\"dark\"]) .t-moon{display:block}}\n"
    ":root[data-theme=\"light\"] .t-sun{display:none}:root[data-theme=\"light\"] .t-moon{display:block}\n"
    ":root[data-theme=\"dark\"] .t-sun{display:block}:root[data-theme=\"dark\"] .t-moon{display:none}\n"
)

FX_JS = r"""
window.__ptCandlePanel = function (opts) {
  var data = JSON.parse(document.getElementById('chart-data').textContent);
  var chart = document.getElementById(opts.chart), list = document.querySelector(opts.list);
  if (!chart || !list) return;
  var series = data[opts.key] || {};
  var title = document.getElementById(opts.title), sub = document.getElementById(opts.sub);
  var range = '3M', current = null;
  var UP = tok('--up', '#30d158'), DOWN = tok('--down', '#ff5a4e');
  var RANGE = { '1W': 7, '1M': 30, '3M': 91, '1Y': 365 };

  var MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  function rate(v) {
    // Money is always cents: $769.710 is three decimals of false precision on a stock. FX rates pick
    // their places by magnitude instead -- gold quotes to 2, the majors to 5.
    if (opts.money) return '$' + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    var dp = v >= 1000 ? 2 : v >= 100 ? 3 : 5;
    return v.toLocaleString(undefined, {minimumFractionDigits: dp, maximumFractionDigits: dp});
  }
  function compact(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'k';
    return String(Math.round(v));
  }
  function nice(lo, hi, n) {
    var span = hi - lo || Math.abs(hi) || 1, step = Math.pow(10, Math.floor(Math.log10(span / n))), err = span / n / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var t = [], v = Math.floor(lo / step) * step;
    // Clamped to the drawn range on purpose. The first tick rounds DOWN below `lo` and the loop used to
    // run past `hi`, so the outermost gridlines landed outside the plot -- and because .chart svg is
    // overflow:visible they escaped the box rather than being clipped. Measured on the FX panel: a
    // 400px chart was drawing labels at y=-24 (which landed on top of the chart's own subtitle) and
    // y=460. Dropping the out-of-range ticks fixes both, and a gridline you cannot place against the
    // axis was never worth drawing anyway.
    for (; v <= hi + step * 0.5; v += step) { if (v >= lo && v <= hi) t.push(v); }
    return t;
  }
  // Chart colours come from the stylesheet so the canvas and the rest of the page cannot drift apart.
  function tok(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function draw(candles) {
    var W = chart.clientWidth || 800, H = chart.clientHeight || 380;
    var L = 72, R = 14, T = 12, B = 26;
    var hasVol = candles.some(function (c) { return c[5] > 0; });
    var volH = hasVol ? Math.round((H - T - B) * 0.22) : 0;
    var priceH = H - T - B - volH - (hasVol ? 8 : 0);

    var lo = Infinity, hi = -Infinity, vmax = 0;
    candles.forEach(function (c) { lo = Math.min(lo, c[3]); hi = Math.max(hi, c[2]); vmax = Math.max(vmax, c[5]); });
    var pad = (hi - lo) * 0.06 || hi * 0.01 || 1;
    lo -= pad; hi += pad;
    var Y = function (v) { return T + priceH - (v - lo) / (hi - lo) * priceH; };
    var n = candles.length, slot = (W - L - R) / n;
    var X = function (i) { return L + slot * (i + 0.5); };
    // Below ~2px a candle body is thinner than its own outline, so draw a line instead.
    var thin = slot < 2.2;
    var bw = Math.max(1, Math.min(slot * 0.68, 14));

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img">';
    nice(lo, hi, 4).forEach(function (v) {
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="' + tok('--line', '#23232a') + '"/>' +
             '<text x="' + (L - 8) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + rate(v) + '</text>';
    });
    if (thin) {
      var d = '';
      candles.forEach(function (c, i) { d += (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(c[4]).toFixed(1); });
      svg += '<path d="' + d + '" fill="none" stroke="' + tok('--accent', '#f0a830') + '" stroke-width="1.6" stroke-linejoin="round"/>';
    } else {
      candles.forEach(function (c, i) {
        var up = c[4] >= c[1], col = up ? UP : DOWN, x = X(i);
        svg += '<line x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' + Y(c[2]).toFixed(1) +
               '" y2="' + Y(c[3]).toFixed(1) + '" stroke="' + col + '" stroke-width="1"/>';
        var yo = Y(c[1]), yc = Y(c[4]), top = Math.min(yo, yc), h = Math.max(Math.abs(yc - yo), 1);
        svg += '<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + bw.toFixed(1) +
               '" height="' + h.toFixed(1) + '" fill="' + col + '"/>';
      });
    }
    if (hasVol) {
      var vTop = T + priceH + 8, VY = function (v) { return vTop + volH - (v / vmax) * volH; };
      svg += '<text x="' + (L - 8) + '" y="' + (vTop + 10) + '" text-anchor="end">' + compact(vmax) + '</text>';
      candles.forEach(function (c, i) {
        if (!c[5]) return;
        var up = c[4] >= c[1], x = X(i), h = Math.max(vTop + volH - VY(c[5]), 0.5);
        svg += '<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + VY(c[5]).toFixed(1) + '" width="' + bw.toFixed(1) +
               '" height="' + h.toFixed(1) + '" fill="' + (up ? UP : DOWN) + '" opacity="0.45"/>';
      });
    }
    // Intraday candles need times: six labels all reading "2 Oct" tells you nothing about where you are
    // in the session.
    var spanDays = (Date.parse(candles[n - 1][0]) - Date.parse(candles[0][0])) / 864e5;
    // Label count from the available WIDTH, not a fixed six. At 1512px six dates fit; in a narrower
    // pane or with the Claude panel open they collide into "21 Jul5 Aug20 Aug". 64px is the widest
    // label this axis draws ("20 Aug", "12:00") plus breathing room.
    var step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - L - R) / 64))));
    for (var i = 0; i < n; i += step) {
      var dt = new Date(candles[i][0]), lab;
      if (spanDays <= 1.5) {
        lab = String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
      } else if (spanDays <= 10) {
        lab = dt.getDate() + ' ' + String(dt.getHours()).padStart(2, '0') + ':00';
      } else {
        lab = dt.getDate() + ' ' + MON[dt.getMonth()];
      }
      svg += '<text x="' + X(i).toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + lab + '</text>';
    }
    /* The crosshair, hidden until the pointer is over the plot. The balance chart has had one of
       these since it was written; this renderer -- which draws BOTH the forex and the stocks panel --
       never did, so the only way to read a candle was to guess against the axis. */
    svg += '<line class="xh-v" y1="' + T + '" y2="' + (T + priceH) + '" stroke="' + tok('--muted', '#8a8a96') +
           '" stroke-dasharray="3 3" style="display:none"/>';
    svg += '<line class="xh-h" x1="' + L + '" x2="' + (W - R) + '" stroke="' + tok('--muted', '#8a8a96') +
           '" stroke-dasharray="3 3" style="display:none"/>';
    chart.innerHTML = svg + '</svg>';
    hover(candles, X, Y, L, R, T, W, priceH, slot, lo, hi);
  }

  /* Reading a candle off the axis is guesswork, so the pointer snaps to the nearest one and the
     panel's own subtitle becomes the readout. Writing it there rather than into a floating box keeps
     it in one place whether you are using a mouse or not, and means no element can be left behind on
     the page when the pointer leaves. */
  function hover(candles, X, Y, L, R, T, W, priceH, slot, lo, hi) {
    var svgEl = chart.querySelector('svg');
    if (!svgEl) { return; }
    var vline = chart.querySelector('.xh-v'), hline = chart.querySelector('.xh-h');
    var restore = sub ? sub.textContent : '';
    function clear() {
      if (vline) { vline.style.display = 'none'; }
      if (hline) { hline.style.display = 'none'; }
      if (sub) { sub.textContent = restore; }
    }
    svgEl.addEventListener('mousemove', function (ev) {
      var box = svgEl.getBoundingClientRect();
      var x = (ev.clientX - box.left) * (W / box.width);
      var y = (ev.clientY - box.top) * ((T + priceH + 40) / box.height);
      var i = Math.round((x - L - slot / 2) / slot);
      if (i < 0) { i = 0; } if (i >= candles.length) { i = candles.length - 1; }
      var c = candles[i];
      if (!c) { return; }
      if (vline) { vline.setAttribute('x1', X(i).toFixed(1)); vline.setAttribute('x2', X(i).toFixed(1));
                   vline.style.display = ''; }
      if (hline && y >= T && y <= T + priceH) {
        hline.setAttribute('y1', y.toFixed(1)); hline.setAttribute('y2', y.toFixed(1));
        hline.style.display = '';
      } else if (hline) { hline.style.display = 'none'; }
      if (!sub) { return; }
      var dt = new Date(c[0]);
      var when = dt.getDate() + ' ' + MON[dt.getMonth()] + ' ' + dt.getFullYear();
      if ((candles[candles.length - 1][0] - candles[0][0]) / 86400000 <= 10) {
        when += ' ' + String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
      }
      var move = c[4] - c[1], pct = c[1] ? (move / c[1]) * 100 : 0;
      sub.textContent = when + '  ·  O ' + rate(c[1]) + '  H ' + rate(c[2]) + '  L ' + rate(c[3]) +
                        '  C ' + rate(c[4]) + '  ·  ' + (move >= 0 ? '+' : '') + pct.toFixed(2) + '%' +
                        (c[5] > 0 ? '  ·  vol ' + Math.round(c[5]).toLocaleString() : '');
    });
    svgEl.addEventListener('mouseleave', clear);
  }

  var intraCache = {};
  function showIntraday(pair, window) {
    var key = pair + '|' + window;
    title.textContent = pair;
    if (intraCache[key]) return render(pair, intraCache[key], window);
    sub.textContent = 'loading ' + window + '…';
    chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">Fetching ' +
                      window + ' candles…</p>';
    fetch('/intraday?symbol=' + encodeURIComponent(pair) + '&range=' + window)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) {
          sub.textContent = '';
          chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">' + j.error + '</p>';
          return;
        }
        intraCache[key] = j.candles;
        if (current === pair && range === window) render(pair, j.candles, window);
      })
      .catch(function (e) {
        chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">Could not load ' +
                          window + ' candles: ' + e + '</p>';
      });
  }

  function render(pair, pts, label) {
    if (pts.length < 2) {
      chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">Not enough data for ' +
                        label + '.</p>';
      sub.textContent = ''; return;
    }
    var pct = pts[0][4] ? (pts[pts.length - 1][4] / pts[0][4] - 1) * 100 : 0;
    var hasVol = pts.some(function (c) { return c[5] > 0; });
    sub.textContent = rate(pts[pts.length - 1][4]) + '  ·  ' + (pct >= 0 ? '+' : '') + pct.toFixed(2) +
      '% over ' + label + (hasVol || !opts.noVolumeNote ? '' : '  ·  ' + opts.noVolumeNote);
    draw(pts);
  }

  function selectPlan(pair) {
    if (!opts.plans) return;
    var box = document.querySelector(opts.plans);
    if (!box) return;
    box.querySelectorAll('[data-plan]').forEach(function (el) {
      el.hidden = el.dataset.plan !== pair;
    });
  }
  function show(pair) {
    selectPlan(pair);
    if (range === '1D' || range === '5D') {
      current = pair;
      list.querySelectorAll('.pair,.sym').forEach(function (b) { b.classList.toggle('on', b.dataset.pair === pair); });
      return showIntraday(pair, range);
    }
    var all = series[pair];
    current = pair;
    list.querySelectorAll('.pair,.sym').forEach(function (b) { b.classList.toggle('on', b.dataset.pair === pair); });
    title.textContent = pair;
    if (!all || all.length < 2) {
      chart.innerHTML = '<p class="subtle" style="padding-top:150px;text-align:center">No price history for ' + pair + '.</p>';
      sub.textContent = ''; return;
    }
    var last = Date.parse(all[all.length - 1][0]);
    var from = range === 'All' ? -Infinity : last - RANGE[range] * 864e5;
    var pts = all.filter(function (c) { return Date.parse(c[0]) >= from; });
    if (pts.length < 2) pts = all.slice(-2);
    render(pair, pts, range);
  }

  list.addEventListener('click', function (e) {
    var b = e.target.closest('.pair'); if (b) show(b.dataset.pair);
  });
  var ranges = document.querySelector('.ranges[data-for="' + opts.tab + '"]');
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
  var railBtn = document.querySelector('.rail-btn[data-tab="' + opts.tab + '"]');
  if (railBtn) railBtn.addEventListener('click', drawWhenVisible);
  drawWhenVisible();
  var redraw; window.addEventListener('resize', function () {
    clearTimeout(redraw); redraw = setTimeout(function () { if (chart.offsetParent !== null && current) show(current); }, 150);
  });
  /* Same for the theme. See the note on the balance charts. */
  (window.__ptRedrawCharts = window.__ptRedrawCharts || []).push(function () {
    if (chart.offsetParent !== null && current) show(current);
  });
};

__ptCandlePanel({chart: 'fx-chart', list: '.pairlist', key: 'fx', title: 'fx-title', sub: 'fx-sub',
                tab: 'forex', money: false, plans: '#fx-plans',
                noVolumeNote: 'spot FX has no exchange volume to report'});
__ptCandlePanel({chart: 'eq-chart', list: '.symlist', key: 'equities', title: 'eq-title', sub: 'eq-sub',
                tab: 'stocks', money: true});
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
/* --- theme ------------------------------------------------------------------------------------
   Dark is the default; the OS preference applies only when the reader has not chosen. A choice is
   remembered per browser, and every read and write is wrapped because localStorage throws outright
   in a private window rather than returning null -- and a thrown exception here would stop the rest
   of this file running, taking the charts with it.

   The charts read their colours from CSS variables through tok(), so they are REDRAWN on a change
   rather than restyled: an SVG stroke is an attribute, not a cascading property, and will not follow
   a token that moves underneath it. */
(function () {
  var KEY = 'pt-theme';
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function apply(mode) {
    if (mode) { document.documentElement.setAttribute('data-theme', mode); }
    else { document.documentElement.removeAttribute('data-theme'); }
  }
  apply(stored());
  function current() {
    var set = document.documentElement.getAttribute('data-theme');
    if (set) { return set; }
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  window.__ptRedrawCharts = window.__ptRedrawCharts || [];
  function redraw() {
    window.__ptRedrawCharts.forEach(function (fn) { try { fn(); } catch (e) {} });
  }
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) { return; }
    btn.addEventListener('click', function () {
      var next = current() === 'light' ? 'dark' : 'light';
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
      redraw();
    });
  });
  /* Follow the Mac's own light/dark switch, but only for a reader who has not overridden it. */
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: light)');
    var onChange = function () { if (!stored()) { redraw(); } };
    if (mq.addEventListener) { mq.addEventListener('change', onChange); }
    else if (mq.addListener) { mq.addListener(onChange); }
  }
})();
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
    // Clamped to the drawn range on purpose. The first tick rounds DOWN below `lo` and the loop used to
    // run past `hi`, so the outermost gridlines landed outside the plot -- and because .chart svg is
    // overflow:visible they escaped the box rather than being clipped. Measured on the FX panel: a
    // 400px chart was drawing labels at y=-24 (which landed on top of the chart's own subtitle) and
    // y=460. Dropping the out-of-range ticks fixes both, and a gridline you cannot place against the
    // axis was never worth drawing anyway.
    for (; v <= hi + step * 0.5; v += step) { if (v >= lo && v <= hi) t.push(v); }
    return t;
  }
  // Chart colours come from the stylesheet so the canvas and the rest of the page cannot drift apart.
  function tok(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
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
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="' + tok('--line', '#23232a') + '"/>' +
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
    svg += '<line class="x" y1="' + T + '" y2="' + (H - B) + '" stroke="' + tok('--muted', '#898781') + '" stroke-dasharray="3 3" style="display:none"/>';
    ser.forEach(function (s) { svg += '<circle class="d" r="4" fill="' + s.color + '" stroke="' + tok('--s1', '#141413') + '" stroke-width="2" style="display:none"/>'; });
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
  /* Sparklines in the watchlist, drawn from the same embedded candles the charts use -- so this
     costs no request and cannot disagree with the panel you open by clicking through.
     Deliberately NOT coloured by direction. The figure beside it is the move since YESTERDAY'S close,
     while the line covers about a month -- so a symbol up today and down over the month would have
     shown a red line against a green number, two signals disagreeing about the same row. The rules at
     the top of this stylesheet give colour one job, and a reader cannot resolve a contradiction in it.
     The line carries shape; the signed percent carries direction. */
  function sparklines() {
    var series = {};
    Object.keys(data.fx || {}).forEach(function (k) { series[k] = data.fx[k]; });
    Object.keys(data.equities || {}).forEach(function (k) { series[k] = data.equities[k]; });
    document.querySelectorAll('.wrow[data-sym]').forEach(function (row) {
      var slot = row.querySelector('.wspark');
      var pts = series[row.getAttribute('data-sym')];
      if (!slot || !pts || pts.length < 3) { return; }
      pts = pts.slice(-22);                       /* about a month of trading days */
      var W = 46, H = 16, lo = Infinity, hi = -Infinity;
      pts.forEach(function (c) { lo = Math.min(lo, c[4]); hi = Math.max(hi, c[4]); });
      if (!(hi > lo)) { return; }                 /* a flat line says nothing and would divide by zero */
      var d = pts.map(function (c, i) {
        var x = (i / (pts.length - 1)) * W;
        var y = H - ((c[4] - lo) / (hi - lo)) * (H - 2) - 1;
        return (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
      }).join(' ');
      slot.innerHTML = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">' +
        '<path d="' + d + '" fill="none" stroke="' + tok('--muted', '#8a8a96') +
        '" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round"/></svg>';
    });
  }
  sparklines();

  function redraw() { charts.account(current.account); charts.sleeves(current.sleeves); charts.shadow(current.shadow); sparklines(); }
  window.addEventListener('resize', redraw); redraw();
  /* Re-stroked on a theme change: an SVG stroke is an attribute, not a cascading property, so it
     will not follow a token that moves underneath it. */
  (window.__ptRedrawCharts = window.__ptRedrawCharts || []).push(redraw);
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
