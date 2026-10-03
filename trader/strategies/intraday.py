"""Opening-range breakout on the London session: the one intraday shape the cost floor allows.

Measured from real ticks, a round trip costs ~0.35 bps at the median EUR/USD spread. At 20 trades a day
that is 18% a year in spread alone, against S1 -- the strategy that passes Gate 1 -- returning 4.4% a
year in total. So any intraday idea worth testing has to trade RARELY. This one takes at most one
position per day and flattens before the session ends, which puts the friction near 0.9% a year.

The rule: measure the high and low of the first `range_minutes` after the London open. If price later
trades above that high, go long. Exit on a stop below the range, or at `close_hour`, whichever comes
first. Long only, because the risk gate forbids shorting.

Why this shape rather than something cleverer: the London open is when overnight order flow meets the
deepest liquidity of the day, which is a reason for a breakout to mean something rather than a pattern
found by searching. It is still a hypothesis, and it still has to clear Gate 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import ClassVar

import pandas as pd

LONDON_OPEN = time(7, 0)        # UTC; 8am London in summer, 7am in winter


@dataclass
class OpeningRangeBreakout:
    """Decides on intraday bars. Deliberately not a `Strategy`: that interface is built around one
    decision per daily bar, and forcing this into it would hide the difference rather than show it."""

    range_minutes: int = 60
    close_hour: int = 16            # flatten by 16:00 UTC, before the thin late session
    stop_buffer: float = 0.25       # stop this far below the range low, as a fraction of range height
    min_range_pips: float = 3.0     # a range this tight is noise, not a level worth breaking

    name: ClassVar[str] = "IRB"
    title: ClassVar[str] = "London opening-range breakout"

    def sessions(self, bars: pd.DataFrame):
        """Yield (day, session_bars). Bars must be tz-aware UTC with an OHLC index."""
        for day, chunk in bars.groupby(bars.index.date):
            yield day, chunk

    def plan(self, session: pd.DataFrame) -> dict | None:
        """The trade for one session, or None. Uses only bars at or before each decision point."""
        if session.empty:
            return None
        open_bars = session[session.index.time >= LONDON_OPEN]
        if open_bars.empty:
            return None
        start = open_bars.index[0]
        window_end = start + pd.Timedelta(minutes=self.range_minutes)
        window = open_bars[open_bars.index < window_end]
        if len(window) < 2:
            return None

        high, low = float(window["high"].max()), float(window["low"].min())
        height = high - low
        if height <= 0 or height * 1e4 < self.min_range_pips:
            return None

        after = open_bars[(open_bars.index >= window_end) & (open_bars.index.hour < self.close_hour)]
        if after.empty:
            return None
        # Entry: the first bar whose HIGH clears the range. Filled at the breakout level, not the close,
        # and never better than the bar's own high.
        breaks = after[after["high"] > high]
        if breaks.empty:
            return None
        entry_bar = breaks.index[0]
        stop = low - height * self.stop_buffer
        return {"entry_ts": entry_bar, "entry": high, "stop": stop,
                "range_high": high, "range_low": low,
                "exit_deadline": after.index[-1]}
