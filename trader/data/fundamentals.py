"""Company fundamentals from Yahoo Finance (yfinance) for the Phase 4 investor-panel strategy.

A small, consistent snapshot per stock (valuation, quality, growth, balance sheet, recent quarters),
cached for a day in data/fundamentals/. `trader fundamentals AAPL --json` prints it for the AI skills.
Yahoo data is for personal research; figures can lag company filings by days.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trader.config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "data" / "fundamentals"

# Yahoo field -> our name. Ratios are fractions (0.25 = 25%) unless noted.
INFO_FIELDS = {
    "shortName": "name", "sector": "sector", "industry": "industry", "marketCap": "market_cap_usd",
    "trailingPE": "pe_trailing", "forwardPE": "pe_forward", "priceToBook": "price_to_book",
    "enterpriseToEbitda": "ev_to_ebitda", "returnOnEquity": "return_on_equity", "returnOnAssets": "return_on_assets",
    "profitMargins": "net_margin", "operatingMargins": "operating_margin", "grossMargins": "gross_margin",
    "revenueGrowth": "revenue_growth_yoy", "earningsGrowth": "earnings_growth_yoy",
    "debtToEquity": "debt_to_equity_pct", "currentRatio": "current_ratio", "freeCashflow": "free_cash_flow_usd",
    "totalCash": "cash_usd", "totalDebt": "debt_usd", "dividendYield": "dividend_yield_pct", "beta": "beta",
    "fiftyTwoWeekHigh": "high_52w", "fiftyTwoWeekLow": "low_52w", "currentPrice": "price",
    "heldPercentInsiders": "insider_ownership", "recommendationKey": "analyst_consensus",
}
QUARTER_ROWS = {"Total Revenue": "revenue", "Operating Income": "operating_income", "Net Income": "net_income",
                "Diluted EPS": "eps_diluted"}


def _clean(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):  # numpy scalars
        return _clean(value.item())
    return value


def snapshot_from_ticker(symbol: str, ticker: Any) -> dict:
    info = ticker.info or {}
    snap: dict[str, Any] = {"symbol": symbol, "as_of": datetime.now().strftime("%Y-%m-%d")}
    for yahoo, ours in INFO_FIELDS.items():
        snap[ours] = _clean(info.get(yahoo))
    quarters = []
    try:
        stmt = ticker.quarterly_income_stmt
        for col in list(stmt.columns)[:5]:
            q = {"quarter_end": str(col.date())}
            for row, name in QUARTER_ROWS.items():
                q[name] = _clean(stmt.at[row, col]) if row in stmt.index else None
            quarters.append(q)
    except Exception:  # some tickers (ETFs, crypto) have no statements
        pass
    snap["quarters"] = quarters
    if len(quarters) == 5 and quarters[0]["revenue"] and quarters[4]["revenue"]:
        snap["revenue_growth_same_quarter_last_year"] = quarters[0]["revenue"] / quarters[4]["revenue"] - 1
    return snap


def load_fundamentals(symbol: str, refresh: bool = False, cache_dir: Path = CACHE_DIR) -> dict:
    path = cache_dir / f"{symbol.replace('/', '_')}.json"
    if path.exists() and not refresh:
        if datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < timedelta(hours=20):
            return json.loads(path.read_text())
    import yfinance as yf

    snap = snapshot_from_ticker(symbol, yf.Ticker(symbol.replace("/", "-")))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=1))
    return snap
