"""Outside heartbeat: ping a monitoring URL (e.g. healthchecks.io) after every engine run.

If the pings stop (Mac asleep or off, engine crashed or switched off), the monitoring service alerts you
by email or Telegram. That catches the one failure the engine can't report itself: not running at all.
Set HEARTBEAT_URL in .env; without it, nothing happens.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def ping(ok: bool, message: str = "") -> None:
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return
    try:
        # healthchecks.io convention: <url> = success, <url>/fail = problem; the body shows in its log.
        httpx.post(url if ok else url.rstrip("/") + "/fail", content=message[:10_000].encode(), timeout=10)
    except httpx.HTTPError as exc:  # never let monitoring break trading
        log.warning("heartbeat ping failed: %s", exc)
