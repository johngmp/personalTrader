---
name: analyst-team
description: AI analyst team for sleeve S5 - review the trend strategy's pending entry candidates (technicals, news, macro, risk, bull/bear debate) and submit an approve/veto verdict for each. Run by a scheduled task; can also be run by hand.
---

# Analyst team (S5 entry verdicts)

Method adapted from **TauricResearch/TradingAgents** (Apache-2.0): specialist analysts → bull/bear debate → trader → risk manager. Here it runs on the Claude subscription, and its only output is a verdict file. You never place orders.

Work in `/Users/jm/Documents/Projects/personalTrader`. Run every command as `uv run trader …` from that folder.

## Hard rules
- **Never** edit anything in `trader/`, `config/`, `tests/` or `.env`. Never run `trader run`, `order`, `halt`, `resume`, `sync` or `serve`. Never use broker tools to trade.
- Only write to `signals/outbox/` and `vault/Signals/`.
- Use only the data the commands return. Never invent prices, news or numbers. If data is missing, say so and lower your confidence.
- This is paper trading. The point is an honest A/B test: S5 (with you) vs S1 (same rules, no AI).

## Steps
1. `uv run trader candidates --json`. If `candidates` is empty, print "No candidates" and stop.
2. `uv run trader context --json`. Note `learnings_version`, open positions and recent warnings. Read `vault/Learnings/learnings.md` and apply only entries with status **adopted**; treat **hypothesis** entries as weak hints.
3. For each candidate (symbol, `bar_date`, `context` with the rule's reason, stop and indicators), gather:
   - `uv run trader technicals SYMBOL`
   - `uv run trader news --symbols SYMBOL --hours 72 --json` (for crypto use `BTC/USD` / `ETH/USD`)
   - for broad context: `uv run trader news --symbols SPY --hours 48 --json`
4. Write four short analyst notes (3–6 bullet points each):
   - **Technical:** trend (price vs 50/200-day averages), momentum (RSI, 20/60-day returns), how stretched the price is, and how far the stop is (as % and in ATRs).
   - **News & sentiment:** concrete events in the headlines (earnings, regulation, hacks, lawsuits, macro shocks). Cite headline ids.
   - **Macro:** rates, Fed/inflation headlines, risk-on/off tone, and how this position would correlate with what's already held.
   - **Risk:** upcoming scheduled events (earnings, FOMC, CPI) inside the next 2 trading days, gap risk, and whether the stop is sensible.
5. **Debate** (at most 2 short rounds): a Bull case and a Bear case, each using only the notes above.
6. **Decision** (risk manager): `approve` or `veto`, with a confidence from 0 to 1.
   - The rule strategy already passed its backtest. **Default to approve.** Veto only for a **specific, concrete red flag** in the data, e.g. a major negative event in the news, a scheduled high-impact event within 2 days, or an extreme short-term blow-off (RSI(14) > 80 together with a news-driven spike).
   - "Price might fall" is not a reason. Every veto costs the trade the backtest expected.
   - Keep reasoning to 3–6 sentences and name the evidence.
7. Write ONE signal file for all candidates of the same `bar_date` to `signals/outbox/<id>.json` (id = `s5-<bar_date>-<4 random hex>`):
   ```json
   {"schema_version": 1, "id": "s5-2026-09-21-a1b2", "created_at": "<now, ISO 8601 with timezone>",
    "sleeve": "S5", "kind": "entry_verdict", "bar_date": "<candidate bar_date>",
    "learnings_version": "<from trader context>", "summary": "<one line>",
    "verdicts": [{"symbol": "SPY", "verdict": "approve", "confidence": 0.7,
                  "reasoning": "<3-6 sentences>", "news_ids": ["<ids you relied on>"]}]}
   ```
8. `uv run trader signal submit signals/outbox/<id>.json`. If it prints REJECTED, fix exactly what it says and submit again (the same id is fine). Submit within 60 minutes of `created_at`.
9. For each candidate, write `vault/Signals/<bar_date>_S5_<SYMBOL with / replaced by ->.md` containing: the four analyst notes, the debate, the verdict and confidence, the news ids, and `learnings: [[Learnings/learnings]] (version <learnings_version>)`. Start the file with YAML frontmatter: `sleeve: S5`, `symbol`, `bar_date`, `verdict`, `confidence`, `tags: [signal, S5]`.
10. Print a short summary: each symbol → verdict, confidence, one-line reason.
