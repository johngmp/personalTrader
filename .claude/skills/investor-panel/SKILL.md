---
name: investor-panel
description: Weekly AI investor panel for sleeve S6 - score 10 large US companies through famous investors' lenses using real fundamentals, technicals and news, then submit target portfolio weights. Run by a weekly scheduled task; can also be run by hand.
---

# Investor panel (S6 weekly target weights)

Method adapted from **virattt/ai-hedge-fund** (MIT): each "investor" agent scores a stock by its own philosophy, then a portfolio manager combines the scores. It runs on the Claude subscription, and its only output is a weights file. You never place orders.

Work in `/Users/jm/Documents/Projects/personalTrader`. Run every command as `uv run trader …` from that folder.

## Hard rules
- **Never** edit anything in `trader/`, `config/`, `tests/` or `.env`. Never run `trader run`, `order`, `halt`, `resume`, `sync` or `serve`. Never use broker tools to trade.
- Only write to `signals/outbox/` and `vault/Signals/`.
- Use only numbers the commands return. If a figure is missing (`null`), say "no data"; never estimate it from memory. Your memory of these companies may be out of date.

## Universe
AAPL, MSFT, NVDA, GOOGL, AMZN, META, JPM, BRK.B, JNJ, XOM (exactly the S6 symbols in `config/sleeves.yaml`).

## Steps
1. `uv run trader context --json`. Note `learnings_version` and the current S6 positions. Read `vault/Learnings/learnings.md` (use **adopted** entries; treat **hypothesis** entries as weak hints).
2. Gather data for all ten:
   - `uv run trader fundamentals AAPL,MSFT,NVDA,GOOGL,AMZN,META,JPM,BRK.B,JNJ,XOM --json`
   - `uv run trader technicals AAPL,MSFT,NVDA,GOOGL,AMZN,META,JPM,BRK.B,JNJ,XOM`
   - `uv run trader news --symbols <the ten, comma-separated> --hours 168 --json`
3. Each persona scores each stock **bullish / neutral / bearish** with a confidence from 0 to 100 and a one-line reason that names the numbers used:
   - **Warren Buffett:** durable advantage, high and steady return on equity, low debt, sensible price
   - **Charlie Munger:** quality and simplicity of the business, margins, avoiding stupidity
   - **Benjamin Graham:** margin of safety (low P/E and P/B), strong balance sheet (current ratio, debt)
   - **Peter Lynch:** growth at a reasonable price (P/E relative to earnings growth), understandable story
   - **Phil Fisher:** long-run growth, margins and reinvestment
   - **Michael Burry:** contrarian deep value; flags overvaluation and balance-sheet risk
   - **Stanley Druckenmiller:** trend and momentum (vs the 200-day average, 60/252-day returns) plus the macro picture from the news
   - **Cathie Wood:** disruptive innovation and high growth; tolerant of high valuations
4. Portfolio manager: combine the scores **mechanically** (don't override the maths):
   - For each stock: `score = average over personas of (+1 bullish / 0 neutral / −1 bearish) × confidence/100`, from −1 to +1.
   - Stocks with `score > 0.15` qualify. Raw weight = score. Scale the raw weights so they add up to at most **0.90** (keep ≥10% cash), then cap each at **0.15** (don't redistribute what the cap removes). Round down to 3 decimals.
   - Stocks that don't qualify get weight **0** (the engine sells them if held).
   - If none qualify, every weight is 0 (all cash). That's allowed.
5. Write `signals/outbox/<id>.json` (id = `s6-<YYYY-MM-DD>-<4 random hex>`) listing **all ten** symbols:
   ```json
   {"schema_version": 1, "id": "s6-2026-09-20-c3d4", "created_at": "<now, ISO 8601 with timezone>",
    "sleeve": "S6", "kind": "target_weights", "bar_date": "<latest `as_of` date from technicals>",
    "learnings_version": "<from trader context>", "summary": "<one line: what the panel favours and why>",
    "weights": [{"symbol": "MSFT", "weight": 0.15, "score": 0.42,
                 "reasoning": "<which personas liked or disliked it and why, with numbers>"}]}
   ```
6. `uv run trader signal submit signals/outbox/<id>.json`. If REJECTED, fix exactly what it says and resubmit (the same id is fine).
7. Write `vault/Signals/<YYYY-MM-DD>_S6_investor-panel.md`: frontmatter (`sleeve: S6`, `date`, `learnings_version`, `tags: [signal, S6]`), a persona × stock table (signal and confidence), the scores and final weights, the three biggest disagreements between personas, and `[[Learnings/learnings]]`.
8. Print a summary: final weights, and what changed from the current S6 positions.
