---
name: daily-digest
description: Morning digest for the personal trader - checks the engine is alive, summarises the account, strategies, positions, trades, AI signals and relevant news, writes it to the Obsidian vault and sends it to Telegram. Run by a daily scheduled task.
---

# Daily digest

Format inspired by **ZhuLinsen/daily_stock_analysis** (MIT): a short verdict per holding, then the key facts. Keep it readable on a phone.

Work in `/Users/jm/Documents/Projects/personalTrader`. Run every command as `uv run trader …` from that folder.

## Hard rules
- **Never** edit `trader/`, `config/`, `tests/` or `.env`. Never run `trader run`, `order`, `halt`, `resume`, `sync` or `serve`. Only write to `vault/Daily/`.
- Report numbers exactly as the commands return them. Don't forecast prices or give investment advice. This is a paper-trading status report.

## Steps
1. `uv run trader context --json`.
2. **Health check first.** If `last_engine_run` is more than 45 minutes old, the message must START with:
   `⚠️ The trading engine hasn't run since <time, Sydney>. Is the Mac asleep or the engine switched off? (./ops/install-engine.sh to restart)`
   Also flag: the kill switch on, any account `drawdown_from_peak` below −7.5%, `ai_requests_pending` older than 6 hours, any `warnings_recent` from the last 24 hours, any rejected `ai_signals_recent`.
3. `uv run trader news --hours 24 --json`, and keep only headlines tagged with symbols we hold.
4. Write `vault/Daily/<YYYY-MM-DD>.md` (Sydney date) with frontmatter `date`, `total_equity`, `tags: [daily]`:
   - **Account:** total equity, change since the start, and each account's day change
   - **Strategies:** each sleeve's value and return; S1 vs B0, and S5 vs S1 once S5 is enabled
   - **Holdings:** one line each: symbol, sleeve, P&L, distance to the stop in %, and a verdict: "holding", "near stop (<2%)" or "new high (stop trailing)"
   - **Last 24 hours:** trades opened and closed (with reasons) and AI signals (with verdicts)
   - **News that matters:** up to 5 headlines for held symbols (headline plus the symbol)
   - **Issues:** anything from the health check
5. Send a phone-sized version (at most about 15 lines) to Telegram: write it to `signals/outbox/digest.txt` and send it with `uv run trader notify --digest --file signals/outbox/digest.txt` (`--digest` tells the engine to skip its own plain summary today).
6. Print the phone version.
