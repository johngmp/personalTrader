"""Plain English for the dashboard.

Every number on this page was named by someone who already knew what it meant. "Average R" and "profit
factor" are precise and useless to a beginner, and a figure you cannot interpret is worse than no figure
-- it looks like information while telling you nothing.

Two things live here:

* GLOSSARY -- what each term means and, more usefully, what a good value looks like. A win rate of 40%
  sounds bad until you know this kind of strategy is supposed to have one.
* headline() -- the answer to "is this working?" in sentences, computed from the data rather than
  written by an AI, so it is always there, costs nothing and cannot hallucinate.

The headline deliberately refuses to be encouraging. Early numbers are noise, and a dashboard that
celebrates a good fortnight teaches exactly the wrong habit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# term -> (what it is, what a good value looks like)
GLOSSARY: dict[str, tuple[str, str]] = {
    "Closed trades": (
        "Positions that have been bought and then sold. Only these have a final profit or loss.",
        "Open positions are still changing; nothing is settled until it closes."),
    "Win rate": (
        "The share of closed trades that made money.",
        "Trend following wins only 35-45% of the time. A few large winners pay for many small losses, so "
        "a low win rate here is normal and not a problem."),
    "Average winning trade": (
        "Average profit on the trades that worked.",
        "This should be several times the average loss. That gap is where the money comes from."),
    "Average losing trade": (
        "Average loss on the trades that did not work.",
        "Losses should be small and similar in size. One huge loss means a stop did not do its job."),
    "Average R": (
        "Profit measured in units of what you risked. +2R means you made twice what you were prepared "
        "to lose on that trade.",
        "Above 0 over many trades. Early on this bounces around wildly and means nothing."),
    "Profit factor": (
        "Total profits divided by total losses.",
        "Above 1 means profitable. Above 1.5 is good. Below 1 means it is losing money."),
    "Trading days": ("Days on which something was actually bought or sold.", ""),
    "Fees paid": (
        "What the broker actually charged, already subtracted from your balance.",
        "Mostly crypto fees. A strategy that trades often gives up a lot here."),
    "Realised": ("Profit or loss already banked by closing a position.", ""),
    "Unrealised": (
        "Profit or loss on positions still open. It can still disappear.",
        "This is not money you have; it is money you have on paper."),
    "Daily loss limit used": (
        "How close today's losses are to the point where the engine stops trading.",
        "At 100% the engine halts itself for the day."),
    "Drawdown limit used": (
        "How far the account is below its best-ever value, against the limit where trading halts.",
        "Every strategy has losing stretches. The limit is there so one cannot run away."),
    "Account balance": (
        "Pretend money. None of this is real -- it is a rehearsal to find out whether the strategies work.",
        ""),
    "Strategy vs buy-and-hold": (
        "Each strategy against B0, which simply buys and holds and does nothing clever.",
        "Beating B0 is the bar. Making money while losing to B0 means the work added nothing."),
    "Shadow tests": (
        "Strategies run on live prices with no orders sent anywhere. A rehearsal within the rehearsal.",
        "They are being measured before being given even pretend money to trade."),
}


def _age_minutes(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 60
    except ValueError:
        return None


def headline(d: dict, now: datetime | None = None) -> list[tuple[str, str]]:
    """(tone, sentence) pairs answering "is this working?". Tones: good, bad, warn, plain."""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []

    # 1. Is it alive? Nothing else matters if it is not.
    age = _age_minutes(d.get("last_run"), now)
    if d.get("kill_switch"):
        out.append(("bad", f"Trading is halted. {d.get('kill_reason') or 'The kill switch is on.'} "
                           "Existing stop orders stay at the broker."))
    elif age is None:
        out.append(("warn", "The engine has not recorded a run yet."))
    elif age > 45:
        hours = age / 60
        out.append(("warn", f"The engine last ran {hours:.1f} hours ago, so it is not making decisions. "
                            "Your positions still have stop orders resting at the broker."))
    else:
        out.append(("good", f"Everything is running. The engine last checked {age:.0f} minutes ago."))

    # 2. Money, with the benchmark comparison that gives it meaning.
    pnl, start = d.get("total_pnl", 0.0), d.get("start_equity") or 1
    direction = "up" if pnl >= 0 else "down"
    out.append(("plain", f"The account is {direction} ${abs(pnl):,.0f} ({pnl / start:+.2%}) on pretend money "
                         "since it started."))

    sleeves = [s for s in d.get("sleeves", []) if not s.get("benchmark")]
    benchmark = next((s for s in d.get("sleeves", []) if s.get("benchmark")), None)
    real = [s for s in sleeves if not str(s.get("broker", "")).startswith("shadow")]
    if benchmark and real:
        best = max(real, key=lambda s: s["return"])
        gap = best["return"] - benchmark["return"]
        if gap >= 0:
            out.append(("good", f"The best strategy, {best['id']}, is ahead of simply buying and holding "
                                f"by {gap:+.2%}."))
        else:
            out.append(("warn", f"No strategy is beating the benchmark. The best, {best['id']}, is "
                                f"{gap:.2%} behind simply buying and holding."))

    # 3. Is any of it meaningful yet? Usually the most important sentence on the page.
    closed = (d.get("kpis") or {}).get("closed", 0)
    if closed < 30:
        out.append(("plain", f"Only {closed} trades have finished. Judging a strategy needs months and "
                             "dozens of trades, so none of the numbers above mean anything yet."))

    # 4. Anything actually wrong.
    warnings = d.get("warnings") or []
    if warnings:
        out.append(("warn", f"{len(warnings)} warning(s) in the last hour — see Orders & warnings."))
    blocked = d.get("blocked") or []
    if blocked:
        out.append(("plain", f"The risk gate refused {len(blocked)} order(s). That is it working, not failing."))
    return out
