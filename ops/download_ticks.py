"""Download 5-minute bars back from today for one or more pairs. Resumable: skips days already on disk.

Usage: download_ticks.py [SYMBOL ...]   (default: EUR/USD)
"""
import sys
import time
from datetime import date, timedelta

import httpx

from trader.data.dukascopy import cache_path, download_day

MINUTES, DAYS = 5, 400
START = date(2026, 10, 2)
symbols = sys.argv[1:] or ["EUR/USD"]

# Sequential on one connection: concurrency is what got us throttled.
client = httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"},
                      limits=httpx.Limits(max_connections=1))
for symbol in symbols:
    done = skipped = 0
    t0 = time.time()
    print(f"=== {symbol} ===", flush=True)
    for i in range(DAYS):
        day = START - timedelta(days=i)
        if day.weekday() >= 5:               # no FX at weekends
            continue
        if cache_path(symbol, day, MINUTES).exists():
            skipped += 1
            continue
        try:
            bars = download_day(symbol, day, MINUTES, client)
            done += 1
            if done % 5 == 0:
                rate = (time.time() - t0) / done
                print(f"{symbol} {day}  {len(bars):>4} bars   {done} days in "
                      f"{(time.time()-t0)/60:.0f}m ({rate:.0f}s/day)", flush=True)
        except Exception as e:
            print(f"{symbol} {day}  FAILED {type(e).__name__}: {e}", flush=True)
            time.sleep(5)
    print(f"{symbol} finished: {done} downloaded, {skipped} already cached, "
          f"{(time.time()-t0)/60:.0f} minutes", flush=True)
