"""One-off: seed the trial ledger with every search this project has already done.

Run once. It is idempotent -- re-running adds nothing, because the trial ids are deterministic.

Without this the ledger starts empty and the Deflated Sharpe's N would collapse from 216 to zero, which
would be a far bigger lie than the one we are fixing. Two sources:

1. `reports/research_log.jsonl` -- 19 Gate 1 runs. Every grid cell of every run, keyed exactly the way
   the OLD counter keyed them: (strategy, params, symbols). Nothing is invented. The historical cost
   model and strategy source cannot be recovered, so backfilled rows deliberately leave those out of the
   key, which reproduces the old count exactly: S1 72, S8 90, S4 36, S3 12, S2 6.

   A consequence worth stating: re-running `trader gate1 S1` after this will add its 18 cells again,
   because the new key includes today's cost model and strategy source. That is not double counting --
   it is the new rule applied honestly. Looking again with today's code IS looking again, and the old
   counter's inability to see that is defect 2 in the plan. It moves the bar the safe way.

2. Research that never went through `trader gate1` at all, and so was never counted anywhere. Each line
   below cites where the count is written down. Where the settings themselves were not recorded, the
   trial carries an index and a note rather than invented parameters: the honest claim is "this many
   looks happened", not "these were the settings".

   Nothing is listed here that is not documented in the repo. Volatility targeting is known to have been
   tested on S1 and S8 (`backtest.py:99-109`, commit 437d76a) but the number of targets tried is not
   recorded anywhere, so it is NOT backfilled -- an uncounted trial is better than a fabricated one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader.config import PROJECT_ROOT                                        # noqa: E402
from trader.research.ledger import _rows, record_trial, trial_count, verify_chain   # noqa: E402

RESEARCH_LOG = PROJECT_ROOT / "reports" / "research_log.jsonl"

# (strategy, batch, n_trials, symbols, where the count is written down)
#
# `batch` is part of the trial id, and it has to be: two of these entries are the SAME grid on the SAME
# pair, measured a second time after a bug was fixed. Without a discriminator those collapse into one
# trial, which is precisely the under-counting this ledger exists to end -- re-measuring the same data
# after changing what you charge is a second look at it, not the same look.
UNLOGGED = [
    ("S-IRB", "clean-18", 18, ["EUR/USD"],
     "intraday.py:21 / PLAN.md:471 -- 'best of 18 settings on the first 60% of the data'"),
    ("S-IRB", "whole-period-3", 3, ["EUR/USD"],
     "PLAN.md:471 -- the earlier run that chose three settings using the WHOLE period before splitting"),
    ("S-IRV", "first-pass", 30, ["EUR/USD"], "PLAN.md:481 -- '30 settings on the first 60%, then measured ONCE'"),
    ("S-IRV", "first-pass", 30, ["GBP/USD"], "PLAN.md:481 -- same grid applied unchanged to the second pair"),
    ("S-IRV", "first-pass", 30, ["AUD/USD"], "PLAN.md:481 -- same grid applied unchanged to the third pair"),
    ("S-IRV", "after-close-bug", 30, ["EUR/USD"],
     "config/intraday.yaml:24-25 -- the whole grid re-measured after the 16:00 close bug ('24/30 of "
     "the grid positive out of sample'). A second look at the same data is a second trial"),
    ("S-IRV", "after-close-bug", 30, ["GBP/USD"],
     "config/intraday.yaml:26 -- 'only 10/30 of the grid is positive in both halves'"),
    ("S-IRV-SHORT", "short-grid", 30, ["EUR/USD"],
     "PLAN.md:808 -- '30 of 30 settings net positive out of sample', the result that was correctly "
     "refused because the honest in-sample pick had only 6 out-of-sample trades"),
    ("S1", "calendar-blackout", 3, ["BTC/USD", "ETH/USD", "QQQ", "SPY"],
     "PLAN.md:421 -- economic calendar blackout at 1/2/3 days, rejected on in-sample alone"),
    ("S8", "calendar-blackout", 3, [],
     "PLAN.md:421 -- blackout at 1/2/3 days: S8 Sharpe 0.698 -> 0.690 -> 0.672 -> 0.659"),
    ("S-PEAD", "horizons", 4, [],
     "PLAN.md:419 -- event study over 891 announcements at +1/+5/+20/+60 day horizons"),
]


def main() -> int:
    ok, why = verify_chain()
    if not ok:
        print(f"refusing to append to a broken ledger: {why}")
        return 1
    before = len(_rows())

    added = 0
    for line in RESEARCH_LOG.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        for params in entry.get("grid", []):
            # Only the CHOSEN cell's Sharpe was ever recorded, so the rest carry None. Future runs
            # record every cell's own Sharpe, because `run_backtest` now does it per backtest.
            chosen = params == entry.get("chosen_params")
            record_trial(strategy=entry["strategy"], params=params, symbols=entry.get("symbols", []),
                         costs={}, span=("", ""), source="backfill",
                         sharpe=entry.get("is_sharpe") if chosen else None,
                         note=f"backfilled from research_log.jsonl run {entry['ts']}")
            added += 1

    for strategy, batch, n, symbols, note in UNLOGGED:
        for i in range(n):
            record_trial(strategy=strategy, params={"batch": batch, "unrecorded_setting": i}, symbols=symbols,
                         costs={}, span=("", ""), source="backfill", sharpe=None,
                         note=f"backfilled, settings not recorded -- {note}")
            added += 1

    print(f"{before} rows before, {added} appended\n")
    for code in sorted({r["strategy"] for r in _rows()}):
        print(f"  {code:<14} {trial_count(code):>4} distinct trials")
    ok, why = verify_chain()
    print(f"\nchain: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
