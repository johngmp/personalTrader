# Personal Trader: Build Plan (v5, awaiting approval)

A personal, multi-market trading system that uses Claude to research, run and review trading strategies across **US stocks/ETFs, crypto and forex**. It starts in **paper trading with auto-approve**, and its whole purpose in this phase is to *prove with numbers* whether any strategy makes money before real money is involved.

> Software plan only, not investment advice. Nothing is built until you approve this document.

**Your requirements (from our chat):**
- Based in **Australia**
- **Paper trading only** for now, with **every trade auto-approved**
- **No Claude API.** All AI work runs on your **Claude Pro subscription** through the Claude desktop app / Claude Code.
- **Self-learning:** every trade is recorded as a note in an **Obsidian vault inside this folder**, and a curated **learnings document** lets the AI improve strategies and decisions over time (§6b)
- Goal: find strategies that actually make money and confirm the whole system works

---

## 0. What changed and why

| Earlier version | v5 | Why |
|----|----|-----|
| A "portfolio brain" merges every signal into one portfolio | A **strategy tournament**: each strategy trades its own virtual slice of the paper account and is scored separately against a benchmark | You want to know *which* ideas make money |
| Claude mainly acts as the trader | Claude **designs, tests and reviews** strategies; rule-based strategies do most of the trading; AI-driven strategies compete as their own entries | Evidence (§1): LLMs trading directly have lost money |
| Runtime AI via the Anthropic API (TradingAgents / ai-hedge-fund libraries) | **Claude subscription only.** AI strategies become **Claude Code skills** run as **scheduled tasks in the Claude desktop app**. They *write trade signals to a file*; the Python engine checks and executes them. | Your choice. Those two repos call the paid API directly, so we reuse their *method and prompts*, not their code. |
| Engine hosted on a VPS | Everything on **your Mac** for the paper phase (VPS/cloud is an option later, decision 2) | Desktop scheduled tasks run on your machine and need local files |
| No memory between runs | **Self-learning loop:** every trade becomes an Obsidian note; a weekly review maintains an evidence-based `learnings.md`; rule changes go through experiments and challenger versions (§6b) | Your request, designed so the system learns from evidence rather than noise |
| Brokers: Alpaca + OANDA + Co-Invest from day one | **Start with Alpaca paper only** (US stocks, ETFs, crypto). **OANDA** (forex) and **Liquid / Co-Invest** (third venue) are added in **Phase 4b**; **IBKR** later for live | You already have an Alpaca account; one broker keeps the first build simple. The broker layer is built to add the others without changes to strategies. |

---

## 1. What the research says

### Evidence on AI trading
- **Alpha Arena, Season 1** (real $10k per model, crypto perps, Oct–Nov 2025): Claude finished **−30.8%**, and only 2 of 6 models made money. ([nof1](https://nof1.ai/), [DXRG analysis](https://www.dxrg.ai/blogs/can-claude-trade))
- **FINSABER** (20 years, 100+ stocks): published LLM trading agents *underperformed buy-and-hold* with no statistically significant alpha; too cautious in bull markets, too aggressive in bear markets. ([paper](https://arxiv.org/html/2505.07078))
- **Look-ahead bias:** backtesting an LLM on past dates is unreliable because the model may already *know what happened*. AI strategies can only be judged by **forward paper testing**. ([benchmark](https://arxiv.org/pdf/2601.13770))
- **DXRG's conclusion:** Claude is best as a **rule designer and consistent executor**, not a stock picker. The checks built around it matter most.

### What the videos showed
- **Miles Deutscher, "7 GitHub repos":** OpenBB (data), TradingView MCP (charts), Fincept Terminal (research app), daily_stock_analysis (watchlist scoring → Telegram), TradingAgents (AI analyst team), ai-hedge-fund (investor personas), Alpaca MCP (orders). Only Alpaca places trades.
- **Samin Yasar, "Claude's trading agent":** Co-Invest (Liquid) for trades, QuiverQuant for Congress/insider data, Firecrawl to watch Discord alerts, Claude **routines** to run it on a schedule. This is the same subscription-based approach this plan uses, but with risk limits in code instead of only in a prompt.
- **Miles Deutscher, "$168,236 bot":** only **1 of 12** famous strategies beat holding Bitcoin, and it lagged Nasdaq buy-and-hold. Claude's "improvements" cut profit from ~$173k to ~$80k. Keep rules simple; size positions by risk.
- **DaviddTech, "make as much money as possible":** Claude's first result (+2,000,000%) was a backtesting glitch; the second had a 66% drawdown. It only became plausible with realistic risk rules and out-of-sample testing, and he still forward-tests 3 months before trading.
- **Humbled Trader, Claude + IBKR bot:** Python bot, rules in a JSON file, 1% risk per trade, max 5 positions, time windows, Telegram alerts, P&L dashboard, and a guard that refuses to run if the paper flag is on but the live connection is used. Close to our design.
- ⚠️ **Ale Ser, "AI Trading Bot 2026":** asks you to deploy an Ethereum "arbitrage bot" contract and send it 1–20 ETH. **This is a known scam pattern.** Never deploy contracts or send funds based on a video.

### What this means for the design
1. **Rule-based strategies are the backbone.** They can be backtested honestly and cost nothing to run.
2. **AI strategies compete as their own entries** and as *filters* on rule-based strategies, each **A/B tested** against the same strategy without the AI.
3. **Every strategy is compared to a benchmark** after costs. It has to beat simply holding.
4. **Forward paper results decide** what survives.

---

## 2. Brokers, data and AI for an Australian user

| Purpose | Choice | Status for AU | Covers | Cost |
|---------|--------|---------------|--------|------|
| **Paper: US stocks/ETFs + crypto** *(from Phase 1)* | **Alpaca paper** (your existing account, paper keys) | ✅ Paper-only accounts open to anyone worldwide with an email, crypto included ([docs](https://alpaca.markets/learn/start-paper-trading)) | US stocks, ETFs, options, ~20+ crypto pairs, free real-time data | Free |
| **Paper: forex** *(Phase 4b)* | **OANDA Australia fxTrade Practice** (v20 REST API) | ✅ ASIC-regulated, accepts AU residents ([OANDA AU API](https://www.oanda.com/au-en/trading/platforms/rest-api/)) | FX majors/minors, gold | Free |
| **Paper: third venue** *(Phase 4b)* | **Liquid / Co-Invest** paper mode | ✅ Australia not restricted; offshore, not ASIC-regulated | Perpetual futures on crypto, stocks, FX, commodities; 24/7 | Free (paper) |
| Optional paper: ASX | **IBKR free trial / paper** | ✅ ([IBKR](https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/)) | ASX, US, FX, futures | Free |
| **Future live** (not in this plan) | IBKR AU, OANDA AU, an AUSTRAC-registered crypto exchange | ⚠️ Alpaca *live* eligibility for Australia isn't confirmed; brokers sit behind swappable adapters | n/a | n/a |
| Market data | Alpaca feeds (OANDA from Phase 4b), `yfinance`, OpenBB | ✅ | All | Free |
| Smart money | QuiverQuant API + MCP | ✅ | US Congress/insider trades | ~US$30/mo (decision 6) |
| Fundamentals (AI persona strategy) | `yfinance` (`trader fundamentals`); Financial Datasets or FMP only if needed | ✅ | US stocks | Free |
| News (AI analyst team) | Alpaca's Benzinga feed via your existing keys (`trader news`), stored in the journal so AI decisions are auditable | ✅ | Stocks + crypto, history to 2016 | Free |
| **AI** | **Your Claude subscription**: Claude Code in the desktop app for building and research; **desktop scheduled tasks** for AI strategies, the daily digest and the weekly review | ✅ | n/a | Already paying; uses your plan's usage limits (§9) |

**Paper trading is optimistic.** Alpaca's paper account doesn't simulate slippage, market impact, dividends or regulatory fees, and Liquid's paper mode doesn't document how it handles funding. The journal subtracts a **cost haircut** (spread + slippage + fees + funding) from every fill, and strategies are judged on that adjusted P&L.

---

## 3. Architecture

The key idea is that **Claude never places trades directly.** The Python engine does all trading on its own schedule, with no AI required. Claude's scheduled tasks *propose* trades by writing signal files, and the engine checks each one against the risk limits before executing it.

```
┌──────────────── CLAUDE (your subscription, Claude desktop app) ────────────────┐
│ A. Research lab: you + Claude Code, interactive                                 │
│    design strategy → write Python rules → backtest → explain → revise           │
│ B. AI strategies: desktop SCHEDULED TASKS running Claude Code skills            │
│    /analyst-team (TradingAgents-style)  /investor-panel (ai-hedge-fund-style)   │
│    read data via `trader` CLI + MCPs → write signals/inbox/<id>.json            │
│ C. Reviewer + learner: daily digest, weekly review → Telegram + vault/;        │
│    updates vault/Learnings/learnings.md, proposes experiments (§6b)            │
│    Changes to strategy code always need YOUR approval                           │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                │ signal files (JSON: symbol, side, confidence, stop, reasoning)
                                ▼
DATA (Alpaca · yfinance/OpenBB · Quiver · OANDA from 4b)
  ─► TRADING ENGINE (Python, runs by itself via macOS launchd, no AI needed)
        rule sleeves: S1 trend · S2 rotation · S3 mean-rev · S4 FX trend · S7a/b Quiver
        AI sleeves:   S5 filter · S6 persona panel  ← read from signals/inbox
            │ proposed orders
            ▼
        RISK GATE (plain Python, unit-tested; per-sleeve + account-wide limits)
            │ approved (auto on paper)
            ▼
        EXECUTION ROUTER ─► Alpaca paper (US stocks, crypto)
                         ─► OANDA AU practice (FX)                           [Phase 4b]
                         ─► Liquid / Co-Invest paper (S1 crypto + S4 FX copies) [Phase 4b]
            │ fills
            ▼
        JOURNAL (SQLite) ─► RECONCILER · SCOREBOARD · TELEGRAM alerts · DASHBOARD
            │
            └─► OBSIDIAN VAULT (vault/): trade notes auto-written by the engine,
                AI reviews, learnings.md, experiments (read by AI sleeves + reviewer)
```

**Why split it this way:**
- **Reliability.** Trading, stops and alerts keep running even if the Claude app is closed, a scheduled task is skipped, or you hit your subscription usage limit. An AI sleeve simply trades nothing that day.
- **Safety.** Scheduled tasks run unattended, so their broker connections are **read-only**. Only the engine, with the risk gate, can place orders. A bad AI output or injected text can't do more than the risk limits allow.
- **Measurability.** Every AI signal is a file with reasoning attached, so the reviewer and you can audit exactly why an AI strategy traded.

**Sleeves:** each paper account is split into virtual slices (e.g. $10k each) tagged by `sleeve_id`, so every strategy gets its own equity curve.

---

## 4. Strategy lineup

Test candidates only. Each must pass Gate 1 (§6) before getting paper money, except AI sleeves, which can't be honestly backtested and go straight to forward testing with smaller slices.

| Sleeve | Idea | Markets | Frequency | Runs as | Benchmark |
|--------|------|---------|-----------|---------|-----------|
| **B0** | Buy-and-hold references (SPY, QQQ, BTC) + cash | All | n/a | Engine | n/a |
| **S1** | Trend following: moving-average trend + ATR trailing stop, volatility sizing | BTC, ETH, QQQ, SPY on Alpaca (+ BTC/ETH copy on Liquid from 4b) | Daily | Engine | Buy & hold same asset |
| **S2** | Cross-asset momentum rotation (strongest of stocks/intl/bonds/gold, or cash) | SPY, EFA, TLT, GLD, BIL | Monthly | Engine | SPY / 60-40 |
| **S3** | Short-term mean reversion (buy sharp dips in an uptrend) | Liquid US large-cap ETFs/stocks | Daily | Engine | SPY |
| **S4** *(from Phase 4b)* | FX trend following with volatility sizing | EUR/USD, AUD/USD, USD/JPY, GBP/USD, XAU/USD on OANDA (+ copy on Liquid) | Daily | Engine | Cash |
| **S5** | **AI filter A/B:** when S1/S3 want to enter, a scheduled `/analyst-team` skill (TradingAgents method: technical, news, sentiment and fundamentals analysts → bull/bear debate → risk check) can **veto** | Same as S1/S3 | Daily, only on days with entries | Scheduled task → engine | Unfiltered S1/S3 |
| **S6** | `/investor-panel` skill (ai-hedge-fund method: Buffett, Munger, Lynch, Graham, Burry, Druckenmiller, Wood personas score each stock) → target weights | ~10 US large caps (Pro usage) | Weekly | Scheduled task → engine | SPY |
| **S7a** *(Quiver)* | Congress-buys copy, buying only **after disclosure**, size-weighted, weekly rebalance | US stocks | Weekly | Engine | SPY |
| **S7b** *(Quiver)* | Corporate insider cluster buys (SEC Form 4, ~2-day filing lag) | US stocks | Daily | Engine | SPY |

**Built-in improvements from the research:**
- **Market regime filter** (trend + volatility)
- **Economic calendar blackout** before FOMC, US CPI/NFP and RBA decisions
- **Correlated-exposure cap** (BTC and the Nasdaq often move together)
- **Volatility-targeted position sizing**
- **Stops held at the broker**, so a crashed process or sleeping Mac doesn't leave a position unprotected
- **Results in AUD** alongside USD

---

## 5. Risk gate (enforced in code, even in auto mode)

```yaml
mode: paper                   # paper | live  (live refuses to start in this plan)
autonomy: auto                # your choice for paper
account:
  max_gross_exposure_pct: 100
  max_daily_loss_pct: 3       # pause new entries for the day
  max_drawdown_halt_pct: 15   # pause everything, Telegram alert, needs /resume
  max_correlated_exposure_pct: 40
  max_orders_per_day: 60
sleeve_defaults:
  max_position_pct_of_sleeve: 25
  risk_per_trade_pct: 1
  max_leverage: 1.0           # also applies to Liquid perps and FX
  require_stop: true
  max_drawdown_disable_pct: 25
ai_signals:
  inbox: signals/inbox
  schema_version: 1           # malformed or unknown fields → rejected and logged
  max_age_minutes: 90         # stale signals are ignored
  allowed_symbols_only: true  # AI can only trade its sleeve's universe
  ai_sleeve_capital_pct: 5    # smaller slices until proven
guards:
  kill_switch_file: ./KILL_SWITCH
  refuse_if_paper_flag_but_live_endpoint: true
  stale_data_seconds: 300
  duplicate_order_window_s: 60
```

---

## 6. How we decide if a strategy "makes money"

**Gate 1: backtest (rule-based sleeves) before they get paper money**
- Fees, spread and slippage included; fills on the **next bar**; no intrabar trailing-stop tricks
- **In-sample / out-of-sample split** plus walk-forward testing
- Multiple assets or periods; ≥ 50 trades where frequency allows
- **Better Sharpe than its benchmark**, max drawdown ≤ 25%, robust to small parameter changes
- Number of variants tried is recorded, to guard against lucky finds
- **S7a extra:** first re-run Quiver's advertised Congress Buys backtest ourselves, buying after disclosure

**Gate 2: forward paper test (all sleeves, the real test)**
- At least **90 days** and **≥ 30 closed trades** (monthly/weekly sleeves judged on consistency instead)
- After the cost haircut: **positive return and better Sharpe than its benchmark**
- Forward Sharpe at least ~50% of backtest Sharpe (rule sleeves)
- AI sleeves must also beat their non-AI comparison (S5 vs unfiltered S1/S3, S6 vs SPY)
- No unexplained broker vs journal differences

Passing both makes a sleeve a **"candidate for live"**. Going live is a separate decision and plan.

---

## 6b. Self-learning loop and Obsidian vault

### The idea
Everything the system does is written as plain Markdown notes into `vault/` inside this project. You open that folder in **Obsidian** to browse it (free app; no plugins or Obsidian MCP required, since Claude reads the files directly). The AI reads a curated **learnings document** before making decisions and updates it after reviewing results.

### The risk this design avoids
Naive "self-learning" usually makes trading systems **worse**, not better:
- **Learning from noise:** 5 losing trades in a row is normal randomness. An AI that writes "never buy on Mondays" after 5 trades is fitting to luck.
- **Stories over statistics:** LLMs are good at explaining *why* a trade lost after the fact, and those explanations sound convincing even when they're wrong.
- **Constant rule changes** make it impossible to tell whether a strategy works, because it's never the same strategy for long.
- **Research warning:** FinMem, an LLM trader built around layered memory and reflection, still underperformed buy-and-hold in the 20-year FINSABER test. Memory alone isn't an edge.

So the loop separates **observations** (cheap, written freely) from **rules** (changed only with evidence), and it measures whether each change actually helped.

### Two layers
1. **Journal (facts):** SQLite `data/journal.db` stays the **source of truth** for numbers. After every fill, the engine automatically writes the matching Markdown trade note. No AI is involved, so this costs no subscription usage. Scoreboard statistics are always computed from the database, never estimated by the AI from notes.
2. **Knowledge (interpretation):** AI-written reviews, learnings and experiments, each linking back to the trade notes they're based on.

### How a learning becomes a strategy change
```
 trade closes ─► engine writes vault/Trades/…md (facts, frontmatter)
                      │
 weekly review task ──┘ reads scoreboard + the week's trade notes (Sonnet, 1 run/week)
      │
      ├─► writes vault/Weekly/2026-W38.md
      └─► updates vault/Learnings/learnings.md:
            new observation        → status: hypothesis (must cite trade links + sample size)
            enough evidence        → proposes an experiment in vault/Experiments/
            contradicted by data   → status: rejected (kept, so it isn't re-learned)

 experiment ─► research lab (you + Claude Code): backtest / out-of-sample test
      │
      ├─ fails  → learning marked rejected
      └─ passes → YOU approve → new strategy version (e.g. S3 v2) runs as a
                  "challenger" next to v1 on paper → promoted only if it beats v1
```

**What can learn automatically, and what can't:**

| Part | Learns how | Guardrail |
|------|-----------|-----------|
| AI sleeves (S5 analyst team, S6 investor panel) | Read `learnings.md` before each run and may use it in their judgement right away | Each run records which learnings version it used, so performance can be compared before and after |
| Rule sleeves (S1–S4, S7) | Only through experiment → backtest → your approval → challenger version | Scheduled tasks **cannot edit** strategy code or config (blocked by permission rules) |
| Risk limits (`risk.yaml`) | **Never learned automatically** | Changed only by you |

### Learning entry format
```markdown
## L-012 · Mean-reversion entries before US CPI underperform
- status: hypothesis        # hypothesis | testing | adopted | rejected | expired
- sleeves: S3
- evidence: 14 trades, avg −0.6R vs +0.2R on other days — [[Trades/2026/09/…]] …
- min sample to act: 30 trades
- next step: [[Experiments/EXP-004 CPI blackout for S3]]
- created: 2026-10-02 · last reviewed: 2026-10-09 · expires: 2027-01-09
```
Rules for the file:
- **Capped at ~150 lines.** The weekly review merges duplicates and archives old entries to `Learnings/archive/`. This keeps it cheap for the AI to read on Pro.
- **Every claim needs trade links and a sample size.** No evidence, no entry.
- **Rejected learnings are kept,** so the AI doesn't rediscover the same wrong idea.
- **Learnings expire** unless re-confirmed, because markets change.

### Vault layout
```
vault/
├── Home.md                         # links: scoreboard, open positions, latest reviews
├── Trades/2026/09/2026-09-18_S1_BTCUSD_long_T0042.md
│      frontmatter: id, sleeve, strategy_version, venue, symbol, side, entry/exit,
│                   stop, size, pnl_after_costs, r_multiple, regime, signal_link
│      body: why it was taken (rule/AI reasoning) · outcome · review notes (added weekly)
├── Signals/                        # AI signal reasoning (S5 vetoes, S6 scores)
├── Sleeves/S1-trend.md             # description, version history, stats (auto-updated)
├── Daily/2026-09-18.md             # daily digest
├── Weekly/2026-W38.md              # weekly review
├── Learnings/learnings.md          # curated, capped
├── Learnings/archive/
├── Experiments/EXP-004-cpi-blackout-s3.md   # hypothesis · test · result · decision
└── Changelog.md                    # every strategy/version change and who approved it
```
The vault is committed to git, so every change to learnings and strategies has a history. Obsidian's free **Dataview** plugin is optional, for tables like "all S3 losses this month".

---

## 7. Tools and tech stack

| Area | Tools |
|------|-------|
| **Building + research** | **Claude Code in the Claude desktop app** (your subscription) |
| **Scheduled AI work** | **Claude desktop local scheduled tasks** (Code tab → Routines → Local) running project **skills** (`.claude/skills/`): `/analyst-team`, `/investor-panel`, `/daily-digest`, `/weekly-review`. Tools pre-approved per task so runs don't stall. |
| **MCPs for Claude** | Alpaca MCP (**paper keys, read-only tools for scheduled tasks**), OpenBB MCP, QuiverQuant MCP; OANDA MCP (read-only) and Co-Invest MCP (paper) from Phase 4b; TradingView MCP optional for you |
| **Language / env** | Python 3.12 + `uv` (existing `pyproject.toml` with `alpaca-py`, pandas, numpy, pydantic, httpx, pyyaml, python-dotenv) |
| **Brokers** | `alpaca-py` (Phase 1) · OANDA v20 via `httpx` (4b) · Liquid (4b) via the Co-Invest MCP using the Python `mcp` client (OAuth), or its REST API if Liquid grants access · IBKR `ib_async` later |
| **Data** | Alpaca feeds + Alpaca/Benzinga news (OANDA from 4b) · OpenBB dropped: Alpaca news + `yfinance` cover what the AI needs · `yfinance` · OpenBB · Quiver API · economic calendar |
| **Backtesting** | `vectorbt` for parameter sweeps + our own trade-by-trade replay with costs; **same strategy code** for backtest and paper |
| **Engine scheduling** | macOS **launchd** runs `trader run` on a timer (e.g. every 5 min; handles AEST vs New York time), independent of the Claude app |
| **Storage** | SQLite `data/journal.db` (source of truth for numbers); AI signals as JSON files in `signals/` |
| **Knowledge / learning** | **Obsidian** vault at `vault/` (plain Markdown + YAML frontmatter; free Obsidian app to browse; Dataview plugin optional); engine writes trade notes, AI writes reviews/learnings/experiments; committed to git for history |
| **Alerts / control** | Telegram bot (`httpx`): fills, errors, digest, `/status`, `/halt`, `/resume` |
| **Dashboard** | Streamlit: per-sleeve equity vs benchmark, positions, AI signal log |
| **Tests** | `pytest`; full coverage of the risk gate, signal validation, sleeve accounting, cost haircut |
| **Not used** | Anthropic API / `anthropic` SDK; TradingAgents and ai-hedge-fund *code* (their prompts/methods are adapted into skills, with license attribution) |

**Repository layout**
```
personalTrader/
├── PLAN.md
├── .claude/skills/          # analyst-team/, investor-panel/, daily-digest/, weekly-review/, research-lab/
├── .mcp.json                # project MCP servers (no secrets)
├── config/                  # risk.yaml · sleeves.yaml · universe.yaml · costs.yaml
├── trader/
│   ├── models.py            # Bar, Signal, Order, Fill, Position, SleeveState
│   ├── data/                # alpaca_data, oanda_data, yf_history, quiver, calendar
│   ├── strategies/          # base.py, s1_trend.py, s2_rotation.py, ... (shared backtest & live)
│   ├── signals/             # schema.py, inbox.py (validate + ingest AI signal files)
│   ├── risk/                # gate.py, limits.py, kill_switch.py, regime.py
│   ├── brokers/             # base.py, alpaca.py, oanda.py, liquid.py, sim.py
│   ├── engine/              # runner, order manager, reconciler
│   ├── research/            # backtest.py, walkforward.py, report.py
│   ├── journal/             # db.py, scoreboard.py, cost_haircut.py
│   ├── notify/              # telegram.py
│   ├── dashboard/           # app.py (Streamlit)
│   └── cli.py               # trader status | run | backtest | data | signal | report | halt
├── ops/launchd/             # com.personaltrader.engine.plist
├── signals/                 # inbox/ processed/ rejected/ (git-ignored)
├── vault/                   # Obsidian vault: Trades/ Signals/ Sleeves/ Daily/ Weekly/ Learnings/ Experiments/ (§6b)
├── reports/                 # backtest HTML reports
├── tests/
└── data/                    # journal.db, cached history (git-ignored)
```

---

## 8. Phased roadmap

### Phase 0: Accounts (you, ~1 evening)
- [ ] In your **Alpaca** dashboard, switch to the **Paper** account and generate **paper** API keys (not live keys)
- [ ] **Telegram** bot via @BotFather → token; message it so we can read your chat ID
- [ ] **Claude desktop app:** turn on *Settings → Desktop app → General → Keep computer awake*
- [ ] Later (Phase 4b): OANDA Australia demo account + v20 token; Liquid account with Co-Invest in paper mode
- [ ] Optional: QuiverQuant API key (Phase 4), IBKR free trial (Phase 5)
- [ ] I create `.env.example`; you paste keys into `.env` yourself (never into chat)

### Phase 1: Foundations (~1 week) ✅ done 2026-09-18
- [x] Config, domain models, SQLite journal, cost-haircut model
- [x] Broker interface + **Alpaca adapter** (paper only; refuses live endpoints) + `sim` broker for tests. The interface is designed so OANDA and Liquid plug in later without changes elsewhere
- [x] Risk gate + kill switch + paper/live guard, fully unit-tested
- [x] `trader status` shows the Alpaca paper account; `trader order --test` sends a tiny order through gate → broker → journal
- [x] Project `.mcp.json` + MCP setup so you can use Claude interactively on the same accounts (read-only Alpaca toolsets)
- **Exit:** a valid test order fills on Alpaca paper (a stock and a crypto pair) and is journaled; limit-breaking orders are rejected and logged.

### Phase 2: Research lab (~1–2 weeks) ✅ done 2026-09-18
- [x] Historical data loader + cache (stocks, ETFs, crypto; FX history too, so S4 is ready for 4b)
- [x] Backtester: costs, next-bar fills, in/out-of-sample, walk-forward, robustness sweep, HTML report (own engine instead of `vectorbt`: the grids are small enough, and it shares strategy + sizing code with live trading)
- [x] S1–S3 as shared strategy classes; Gate 1 on each (S4 backtested here too, but only goes live in 4b)
- [x] `/research-lab` skill: "design → backtest → explain → revise", every variant logged
- **Exit:** a report of which S1–S4 pass Gate 1 (possibly none, which is still useful).
- **Result (2026-09-18):** S1 trend ✅ PASS · S4 FX trend ✅ PASS · S2 rotation ✕ FAIL (robustness 66% < 70%) · S3 buy-the-dip ✕ FAIL (Sharpe 0.43 < SPY 0.84). Reports in `reports/backtests/`, log in `reports/research_log.jsonl`.

### Phase 3: Paper engine live (~1 week) ✅ built 2026-09-18 · 7-day unattended check pending
- [x] Engine runner, order manager (broker-held stops: GTC stop orders on whole shares; stop-limit + engine backup for crypto), reconciler
- [x] **Vault writer:** engine creates/updates a trade note for every fill and close, plus `Sleeves/` stat notes and `Home.md` (no AI usage)
- [x] launchd job so the engine runs on its own schedule (`ops/install-engine.sh` / `ops/uninstall-engine.sh`); "engine went silent" alert moves to the Phase 4 daily-digest task (a stopped engine can't alert about itself)
- [x] Telegram alerts + /status /halt /resume (active once you add a bot token); dashboard as a self-refreshing HTML page (`data/dashboard.html`) instead of Streamlit: no server to keep running
- [x] B0 + S1 enabled (approved 2026-09-18); `trader run --dry-run` verified against the real paper account. **Auto-execution starts when you run `ops/install-engine.sh`.**
- **Exit:** 7 days unattended with zero reconciliation mismatches.

### Phase 4: AI + Quiver strategies (~1–2 weeks)
- [ ] Signal file schema + `trader signal` validation/ingest (with tests for malformed, stale and out-of-universe signals)
- [ ] `/analyst-team` skill + S5 scheduled task (runs after the daily scan, only if S1/S3 have entries)
- [ ] `/investor-panel` skill + S6 weekly scheduled task
- [ ] `/daily-digest` (7am AEST) scheduled task → Telegram + `vault/Daily/`
- [ ] **Learning loop:** `/weekly-review` scheduled task writes `vault/Weekly/`, adds post-trade reviews to the week's closed trades, maintains `learnings.md` (evidence-linked, capped, statuses) and drafts `Experiments/`
- [ ] AI sleeves read `learnings.md` before each run and record the learnings version they used
- [ ] Permission rules: scheduled tasks may write only to `vault/` and `signals/`, never `trader/` or `config/`
- [ ] Quiver client; re-check the Congress Buys backtest; S7a/S7b sleeves (if approved)
- [ ] Track subscription usage per run; tune how many tickers AI sleeves analyze
- **Exit:** AI sleeves trading on paper for 2 weeks without stalled runs or usage-limit problems.

### Phase 4b: Add OANDA (forex) and Liquid (~1 week, once Phases 3–4 run smoothly)
- [ ] You open the OANDA Australia demo and Liquid accounts (Co-Invest in paper mode)
- [ ] OANDA adapter + S4 FX sleeve (if it passed Gate 1); OANDA MCP read-only for Claude
- [ ] Liquid adapter (Co-Invest MCP via Python `mcp` client, or REST API if granted) + copies of S1 crypto and S4 FX at 1×, with funding in the cost haircut
- [ ] New sleeves start their own 90-day Gate 2 clock
- **Exit:** both venues reconcile cleanly for 7 days.

### Phase 5: Tournament (≥ 90 days, mostly waiting)
- [ ] Weekly scoreboard in Telegram + dashboard
- [ ] Monthly experiment session with you: run the proposed experiments in the research lab; approved ones become challenger versions (e.g. S3 v2 vs v1) logged in `Changelog.md`
- [ ] Monthly check-in: retire losers, research replacements (new rule sleeves pass Gate 1 first)
- [ ] Optional: ASX via IBKR paper
- **Exit:** a Gate 2 report per sleeve saying which, if any, are candidates for live money.

### Phase 6: Live readiness (separate plan, only if something passes)
IBKR/OANDA AU live accounts, small sizes, manual approval at first, ATO record-keeping.

---

## 9. Running costs and subscription usage (paper phase)

| Item | Cost | Notes |
|------|------|-------|
| Brokers, market data | Free | Alpaca paper, yfinance, OpenBB (OANDA and Liquid paper also free from 4b) |
| Claude | **Your existing subscription** | Building, research and scheduled tasks all draw from your plan's usage limits |
| QuiverQuant API (decision 6) | ~US$30 / month | Cancel if S7a/S7b fail Gate 2 |
| Mac electricity / staying awake | Minimal | Mac stays on, plugged in, lid open (or a Mac mini) |
| Optional: Financial Datasets / FMP | US$0 to start | Only if S6 needs better fundamentals |

**Estimated subscription use by scheduled tasks** (tuned in Phase 4):
Sized for **Pro**:
- S5 analyst team: 0–1 run per weekday, only when entries exist, **max 3 candidates per run**, Sonnet
- S6 investor panel: 1 run per week, **~10 stocks**, Sonnet
- Daily digest: 1 short run per day, Haiku/Sonnet, mostly reads pre-computed numbers
- Weekly review + learning update: 1 run per week, Sonnet
- Trade notes and statistics: written by the engine, **no AI usage**
- Runs scheduled for early morning AEST (after the US close), when you're less likely to be using Claude

Pro has 5-hour usage windows plus weekly limits, shared with your own Claude use, including building this project with me. If the AI tasks crowd out your own use, first cut S6 to every two weeks, then reduce S5 candidates. Rule sleeves are unaffected either way. Upgrading to Max is optional later, only if the AI sleeves earn it.

---

## 10. Limitations and gotchas

- **Your Mac must be on and awake** for scheduled tasks and the engine. Scheduled tasks skip runs while the Mac sleeps and catch up once on wake. Closing the lid still sleeps the Mac. Rule-based daily strategies tolerate this, and broker-held stops protect open positions.
- **Cloud routines instead of the Mac** (decision 2) would run with the laptop off, but have a 1-hour minimum interval, a daily run cap (Pro 5, Max 15), need a GitHub repo, and can't see local files. Possible later, more complex.
- **Subscription limits (Pro):** if you hit your usage limit, AI sleeves skip that run; rule sleeves and risk limits are unaffected. Building phases also use Pro usage, so expect some sessions to pause until the window resets.
- **Self-learning can overfit:** learnings need linked evidence and minimum sample sizes; rule changes go through experiments, your approval and challenger testing (§6b). Expect few rule changes in the first 90 days, because there won't be enough trades yet.
- **Scheduled task permissions:** each task gets its tools pre-approved on first run so later runs don't stall waiting for you.
- **Liquid:** perpetual futures only (no ownership, funding costs, no dividends), young offshore platform not regulated by ASIC, routes to DeFi venues. Paper venue only, 1× leverage.
- **Copy-trading Discord alerts:** high manipulation risk; not included.
- **Fincept Terminal, TradingView MCP, daily_stock_analysis:** optional personal tools; the engine doesn't depend on them.
- **Short-term/scalping strategies:** costs kill them; we start with daily/4h.
- **Scams:** ignore anything asking you to deploy a contract or send crypto to a "bot".
- **Paper ≠ live:** even with the cost haircut, real results will be somewhat worse.

---

## 11. Decisions to confirm when you approve

1. ~~Claude plan~~ **Pro** (confirmed; usage sized in §9).
2. **Where it runs:** (a) your Mac, kept awake with the Claude app open (recommended for paper); or (b) engine on a small Sydney VPS (~A$15/mo) plus Claude **cloud routines** for the AI tasks.
3. **ASX:** leave for Phase 5 via IBKR paper (recommended), or include from the start?
4. **Strategy lineup:** keep B0 and S1–S7b, or add/drop any?
5. ~~Brokers~~ **Alpaca only to start** (confirmed); OANDA + Liquid added in Phase 4b.
6. **QuiverQuant:** API (~US$30/mo) from Phase 4 for S7a/S7b (recommended), skip, or website Premium only for your own research?
7. **Learning loop:** AI sleeves may use learnings immediately, but rule strategies change only via experiment → your approval → challenger version (recommended); or also let the weekly review auto-apply small parameter changes (not recommended).

---

### Sources
Videos: [Miles Deutscher: 7 GitHub repos](https://www.youtube.com/watch?v=me5uegV7vOU) · [Samin Yasar: Claude trading agent](https://www.youtube.com/watch?v=x2pY9kI0zBY) · [Miles Deutscher: $168,236 bot](https://www.youtube.com/watch?v=VDpTU5kdj8A) · [DaviddTech: make as much money as possible](https://www.youtube.com/watch?v=wYuP1QFZ0Bg) · [Ale Ser: ETH "arbitrage bot" (scam pattern)](https://www.youtube.com/watch?v=vDUoCJ7F8xU)
Research: [DXRG: Can Claude trade?](https://www.dxrg.ai/blogs/can-claude-trade) · [FINSABER](https://arxiv.org/html/2505.07078) · [Look-ahead bias benchmark](https://arxiv.org/pdf/2601.13770) · [Alpha Arena](https://nof1.ai/) · [Humbled Trader: Claude + IBKR bot](https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/) · [Congress trades study](https://equibles.com/research/what-happens-after-congress-buys-or-sells-a-stock-evidence-from-17-859-disclosures)
Claude: [Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks) · [Routines](https://code.claude.com/docs/en/routines) · [Claude Code with Pro/Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan) · [Agent SDK / claude -p with your plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
Brokers & data: [Alpaca paper trading](https://alpaca.markets/learn/start-paper-trading) · [Alpaca paper limitations](https://docs.alpaca.markets/us/docs/paper-trading) · [OANDA Australia API](https://www.oanda.com/au-en/trading/platforms/rest-api/) · [IBKR paper](https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/) · [Liquid docs](https://docs.tryliquid.xyz/llms.txt) · [QuiverQuant](https://www.quiverquant.com/)
Repos: [TradingAgents](https://github.com/TauricResearch/TradingAgents) · [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) · [OpenBB](https://github.com/OpenBB-finance/OpenBB) · [alpaca-mcp-server](https://github.com/alpacahq/alpaca-mcp-server)
