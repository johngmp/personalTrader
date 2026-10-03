"""Download EUR/USD 5-minute bars back from today. Resumable: skips days already on disk."""
import sys, time
from datetime import date, timedelta
import httpx
from trader.data.dukascopy import download_day, cache_path

SYM, MINUTES, DAYS = "EUR/USD", 5, 400
# Sequential on one connection: concurrency is what got us throttled.
client = httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"},
                      limits=httpx.Limits(max_connections=1))
start = date(2026, 10, 2)
done = skipped = 0
t0 = time.time()
for i in range(DAYS):
    day = start - timedelta(days=i)
    if day.weekday() >= 5:               # no FX at weekends
        continue
    if cache_path(SYM, day, MINUTES).exists():
        skipped += 1
        continue
    try:
        bars = download_day(SYM, day, MINUTES, client)
        done += 1
        if done % 5 == 0:
            rate = (time.time() - t0) / done
            print(f"{day}  {len(bars):>4} bars   {done} days in {(time.time()-t0)/60:.0f}m "
                  f"({rate:.0f}s/day)", flush=True)
    except Exception as e:
        print(f"{day}  FAILED {type(e).__name__}: {e}", flush=True)
        time.sleep(5)
print(f"finished: {done} downloaded, {skipped} already cached, {(time.time()-t0)/60:.0f} minutes", flush=True)
