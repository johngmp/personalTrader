---
name: weekly-review
description: Weekly learning loop for the personal trader - reviews the week's trades and strategies, adds post-trade reviews to the vault, maintains the evidence-based learnings file and drafts experiments (never changes code or settings). Run by a weekly scheduled task.
---

# Weekly review and learning loop (PLAN.md §6b)

You are the system's reviewer. You learn from **evidence, not noise**, and you **never change trading code or settings**. Rule changes only happen later, through an experiment the user approves.

Work in `/Users/jm/Documents/Projects/personalTrader`. Run every command as `uv run trader …` from that folder.

## Hard rules
- **Never** edit `trader/`, `config/`, `tests/`, `.env`, `reports/` or `vault/Changelog.md`. Never run `trader run`, `order`, `halt`, `resume`, `sync` or `serve`.
- You may write: `vault/Weekly/`, `vault/Learnings/`, `vault/Experiments/`, and the part of trade notes **below** the `<!-- engine:end -->` line (never above it).
- Judge **decisions, not outcomes.** A trade that followed the rules and lost is not a mistake. Only look at information that was available when the trade was made (hindsight is banned).
- Small samples are noise. Five losses in a row is normal for a strategy that wins ~40% of the time.

## Steps
0. **First run only.** `vault/Learnings/learnings.md` has no entries and the 123 existing trade notes all have an empty `## Review`. Do **not** try to review them all: you will spend the whole run and produce slop. Review the **20 most recent closed trades**, and write one line in the weekly note saying how many older ones were skipped and that it was deliberate. Later runs cover the week.
1. `uv run trader context --json --days 7`. Read the last weekly note in `vault/Weekly/` (if any), `vault/Learnings/learnings.md`, and the trade notes and `vault/Signals/` notes from this week.
1b. **The evidence, not just the money.** Run `uv run trader gate2 all` and `uv run trader ledger stats`, and put both in the weekly note. Gate 2 says whether each sleeve is still reproducing its backtest; the ledger says how many distinct looks at the data have been spent. A sleeve that is up this week while failing Gate 2 is the single most important thing you can report, and a week's P&L on its own will not show it.
2. **Scoreboard.** Write `vault/Weekly/<YYYY>-W<week>.md` with frontmatter `week`, `tags: [weekly]` and:
   - a table of each sleeve's value, week return, return since start, and vs benchmark (S1 and the others vs B0; S5 vs S1 as the A/B test; S6 vs B0; S4 vs cash)
   - trades closed this week: symbol, sleeve, P&L, R, why in and why out
   - **AI A/B check:** for each S5 veto this week, what S1 did with the same entry (the same symbol and bar date in S1's trades). Did the veto save money or cost money? Keep a running tally.
   - process health: warnings, mismatches, rejected AI signals, skipped entries ("no AI verdict")
   - **forex ticket follow-through:** from `fx_reports_recent` in the context JSON -- how many of the week's hand-traded tickets were taken, skipped, or never answered, and for the taken ones, the slippage in pips between the quoted BUY rate and the price actually reported. Raw counts only; there are too few trades yet to draw a lesson from them.
3. **Post-trade reviews.** For each trade closed this week, append under `## Review` (below `<!-- engine:end -->`) in its note:
   - *Decision quality:* did the entry and exit follow the rules? Was the stop sensible given the volatility then?
   - *What happened:* the facts, in one or two lines
   - *Possible lesson:* only if it points to a pattern worth tracking; otherwise write "none: normal variance"
4. **Learnings maintenance** (`vault/Learnings/learnings.md`). Every entry uses this format:
   ```markdown
   ## L-012 · <short claim>
   - status: hypothesis        # hypothesis | testing | adopted | rejected | expired
   - sleeves: S1
   - evidence: <n> trades, <the key numbers> — [[Trades/...]] [[Trades/...]]
   - min sample to act: 30 trades
   - next step: <what would confirm or reject it>
   - created: <date> · last reviewed: <date> · expires: <date + 3 months>
   ```
   - Add a new **hypothesis** only if at least 3 trades point the same way and you can explain why it would happen.
   - Update the evidence counts on existing entries. Mark **rejected** when the data contradicts it, and keep it so it isn't re-learned. Mark **expired** past its expiry without confirmation.
   - Never mark anything **adopted** with fewer than 30 supporting trades **and** a passed experiment.
   - Keep the file under ~150 lines. Move expired or rejected entries older than 2 months to `vault/Learnings/archive/<YYYY>.md`.
5. **Experiments, pre-registered.** When a hypothesis reaches ~30 supporting trades, or has a strong mechanism plus 10+ trades, draft `vault/Experiments/EXP-<nnn>-<slug>.md`. **Write it BEFORE any backtest is run**, and fill in every field below before the first number exists:
   - the hypothesis, and the *mechanism* -- what behaviour or risk premium would make this true
   - the exact rule change, and the exact parameter grid to be searched (keep it small: every cell is a trial and raises the bar for every future result)
   - the data window it may use
   - **the bar that counts as success, stated as a number, before you know the answer**
   - what result would make you abandon it
   - the risks

   This is the one control both 2026 papers on automated trading research insist on, and it costs nothing. Writing the success bar afterwards is how "we tried thirty things and this one worked" becomes "we found an edge". If the result later misses the bar, record that in the same file and mark the hypothesis **rejected** -- a rejected experiment that stays on the record is worth as much as a passed one, because it stops the idea being re-learned.

   **Don't implement it.** It needs the user's approval.
6. Telegram: write a phone-sized summary (at most ~15 lines: the scoreboard, the AI A/B tally, new or changed learnings, and experiments awaiting approval) to `signals/outbox/weekly.txt` and send it with `uv run trader notify --file signals/outbox/weekly.txt`.
7. Print the same summary.
