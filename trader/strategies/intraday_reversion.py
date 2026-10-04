"""Session mean reversion: the opposite hypothesis to the opening-range breakout, and a different one.

The breakout in `intraday.py` was tested on real ticks and had no edge at all -- it lost money BEFORE
any spread was charged. This is not that idea reparameterised; it is the other mechanism.

Daily FX trends, because carry and macro flows push in one direction for weeks. Intraday FX mostly does
not: inside a session it is dominated by liquidity provision and the absorption of order flow, so a
sharp move away from where the session opened tends to overshoot and come back as that flow is worked
through. Trending and reverting are different regimes on different clocks, and the strategy that fits
one is the wrong shape for the other.

Two consequences follow from that, and both matter:

* It has a TARGET. Reverting means reverting TO something -- here the session's opening price. A trend
  strategy has no target because cutting a winner at a fixed level is what stops it working; a reversion
  strategy without one has no thesis at all.
* It can only take half the trades. The risk gate forbids shorting, so this can buy dips but never sell
  rallies. If falls revert differently from rises -- and there is no reason to assume they are symmetric
  -- this test cannot see it. That is a real limitation on the evidence, not a detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import ClassVar

import pandas as pd

LONDON_OPEN = time(7, 0)


@dataclass
class SessionReversion:
    """Buys a fall away from the session open, aiming to be paid when it comes back."""

    drop_atr: float = 1.0        # how far below the open a fall has to go before it is worth buying
    stop_atr: float = 1.0        # how much further it may go before the idea is wrong
    atr_days: int = 20           # days of session range used to size "how far is far"
    close_hour: int = 16         # flat before the thin late session
    target_frac: float = 1.0     # 1.0 = all the way back to the open

    name: ClassVar[str] = "IMR"
    title: ClassVar[str] = "Session mean reversion"

    def sessions(self, bars: pd.DataFrame):
        """Yield (day, session) and carry the recent daily ranges along for scale."""
        days = sorted(bars.index.normalize().unique())
        ranges: list[float] = []
        for day in days:
            chunk = bars[bars.index.normalize() == day]
            if not chunk.empty:
                self._atr = (sum(ranges[-self.atr_days:]) / len(ranges[-self.atr_days:])) if ranges else None
                yield day.date(), chunk
                ranges.append(float(chunk["high"].max() - chunk["low"].min()))

    def plan(self, session: pd.DataFrame) -> dict | None:
        atr = getattr(self, "_atr", None)
        if session.empty or not atr:
            return None
        open_bars = session[session.index.time >= LONDON_OPEN]
        open_bars = open_bars[open_bars.index.hour < self.close_hour]
        if len(open_bars) < 4:
            return None
        session_open = float(open_bars["open"].iloc[0])
        trigger = session_open - self.drop_atr * atr
        stop = trigger - self.stop_atr * atr
        target = session_open - (1 - self.target_frac) * (session_open - trigger)

        after = open_bars.iloc[1:]
        hit = after[after["low"] <= trigger]
        if hit.empty or trigger <= stop:
            return None
        return {"entry_ts": hit.index[0], "entry": trigger, "stop": stop, "target": target,
                "session_open": session_open, "exit_deadline": after.index[-1]}
