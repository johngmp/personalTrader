# Personal Trader: Build Plan (v5, approved 2026-09-18)

A personal, multi-market trading system that uses Claude to research, run and review trading strategies across **US stocks/ETFs, crypto and forex**. It starts in **paper trading with auto-approve**, and its whole purpose in this phase is to *prove with numbers* whether any strategy makes money before real money is involved.

> Software plan only, not investment advice. Approved 2026-09-18; phases are ticked off below as they are built.

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
| Smart money | ~~QuiverQuant~~ not needed (2026-09-18). Insider buys, if wanted later, come free from SEC EDGAR Form 4 filings | ✅ | US insider trades | Free |
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
DATA (Alpaca prices + news · yfinance history/fundamentals · OANDA from 4b)
  ─► TRADING ENGINE (Python, runs by itself via macOS launchd, no AI needed)
        rule sleeves: S1 trend · S2 rotation · S3 mean-rev · S4 FX trend · (S7b insider buys, optional)
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
| ~~S7a~~ | ~~Congress-buys copy~~, dropped: independent evidence shows it lags the S&P 500 after disclosure | | | | |
| **S7b** *(optional)* | Corporate insider cluster buys (free SEC EDGAR Form 4 data, ~2-day filing lag) | US stocks | Daily | Engine | SPY |

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
| **MCPs for Claude** | Alpaca MCP (**paper keys, read-only tools for scheduled tasks**), news tools included; OANDA MCP (read-only) and Co-Invest MCP (paper) from Phase 4b; TradingView MCP optional for you |
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
│   ├── data/                # history (Yahoo), live_bars (Alpaca), news, fundamentals; oanda_data in 4b
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

### Phase 4: AI strategies + learning loop ✅ built 2026-09-19 (switched off until the Phase 3 check passes)
- [x] Signal schema + `trader signal submit/validate/list`, stored in the journal with the learnings version used; tests for malformed, stale, future, duplicate, out-of-universe and over-limit signals
- [x] `/analyst-team` skill + S5 sleeve (S1 rules; entries wait for approve/veto; skipped after 8h without a verdict). S3 failed Gate 1, so S5 filters S1 only
- [x] `/investor-panel` skill + S6 sleeve (10 large US companies, weekly weights, ATR stops)
- [x] `/daily-digest` skill (6:35am) → Telegram + `vault/Daily/`, including the "engine went silent" check; the engine's 7am summary remains the fallback
- [x] **Learning loop:** `/weekly-review` skill writes `vault/Weekly/`, adds post-trade reviews, maintains `learnings.md` (evidence-linked, capped, statuses), tracks the S5 veto A/B tally and drafts `Experiments/`
- [x] AI skills read `learnings.md` (adopted entries only as rules) and every signal records the learnings version
- [x] Scheduled tasks are limited by their skill rules plus per-task tool approvals (only `uv run trader …`, `vault/`, `signals/`); the engine re-validates every signal and every order still passes the risk gate. Schedules and prompts in `ops/scheduled-tasks.md`
- [ ] (optional, not built) S7b insider cluster buys from free SEC EDGAR Form 4 data, through Gate 1 first. Quiver/S7a dropped.
- [ ] After switch-on: watch Pro usage per run; tune how many symbols the AI analyses
- **Switch-on (after the Phase 3 check):** enable S5 and S6 in `config/sleeves.yaml`, create the four scheduled tasks, and do a first "Run now" of each while watching.
- **Exit:** AI sleeves trading on paper for 2 weeks without stalled runs or usage-limit problems.

### Phase 4b: Add OANDA (forex) and Liquid (~1 week, once Phases 3–4 run smoothly)
- [ ] You open the OANDA Australia demo and Liquid accounts (Co-Invest in paper mode)
- [ ] OANDA adapter + S4 FX sleeve (if it passed Gate 1); OANDA MCP read-only for Claude
- [ ] Liquid adapter (Co-Invest MCP via Python `mcp` client, or REST API if granted) + copies of S1 crypto and S4 FX at 1×, with funding in the cost haircut
- [ ] New sleeves start their own 90-day Gate 2 clock
- **Exit:** both venues reconcile cleanly for 7 days.

### Improvements added 2026-09-19 (from the "how to improve" review)
- [x] **Shadow strategies** (`broker: shadow`): full engine + risk gate on live Alpaca prices, no orders sent; research before a paper slice. Separate dashboard chart, quiet on Telegram, excluded from account totals
- [x] **S8 diversified trend following** (19 markets across stocks, bonds, real assets, sectors, the dollar and crypto): Gate 1 PASS (out-of-sample Sharpe 0.98 vs 0.73; walk-forward 0.77; worst drop −22.7%). Running in **shadow** from 2026-09-19; a real paper slice needs your OK
- [x] **Outside heartbeat** (`HEARTBEAT_URL`, e.g. healthchecks.io): alerts you if the engine stops running, even if the Mac is off
- [x] Engine: one sleeve failing no longer stops the other sleeves' decisions
- [x] **MAE/MFE per trade** (2026-10-03): recorded in every backtest trade, `trader excursions`. Across 872 S1 and 4,160 S8 trades, **not one winner ever dipped past 1R** (worst heat 0.93R / 0.99R), so the stops are not cutting winners off. Winners reach a median 1.95R; only 6% of losers were ever up more than 1R
- [x] **Deflated Sharpe in Gate 1** (2026-10-01): S1 100.0% and S8 99.9% after 72 distinct settings; **S4 fails at 30.4%** even though it now passes the benchmark check
- [x] **Results in AUD** + `caffeinate` agent (2026-10-03)
- [ ] Next candidates: FX carry + trend (note: would attach to S4/S9, which already fail Gate 1); `vault/Knowledge/` research notes

#### Intraday forex: the data exists, the cost floor is the problem (researched 2026-10-03)
Dukascopy publishes its full tick feed free over plain HTTP — no account, no API key — **with real bid
and ask**, which is exactly what Yahoo cannot give and what makes intraday untestable on free data.
Verified directly: EUR/USD, GBP/USD, AUD/USD and XAU/USD all return ~4,000–21,000 ticks per hour. The
feed throttles (503s and timeouts on aggressive access); at 4 concurrent requests with retries a full day
downloads in ~35s, so **~2.5 hours per symbol-year, ~51 hours for 5 years × 4 symbols**.

Before spending that, the cost floor was measured from one real day (87,964 EUR/USD ticks, 24 Sep 2026):

| UTC hour | median spread | round trip |
|---|---|---|
| 09:00 / 15:00 / 18:00 | 0.20 pips | 0.35 bps |
| 00:00 / 06:00 / 12:00 | 0.30 pips | 0.53 bps |
| **21:00 (rollover)** | **2.10 pips** | **3.68 bps** |

At the median spread, trading **5 times a day costs 4.6% a year in spread alone**. S1 — the strategy that
passes Gate 1 — returns about **4.4% a year in total**. So a modest-frequency intraday strategy must find
more edge than our best working strategy produces, purely to break even. The 21:00 rollover is 10× the
median, and it is exactly when an unattended system would happily trade.

**Conclusion:** the data is no longer the blocker; the cost floor is. Worth revisiting only with a
specific hypothesis about where intraday edge comes from, tested through the same Gate 1 (including the
deflated-Sharpe trials penalty) — not as "daily but faster".

#### Tested and rejected (do not re-try without a different design)
| Idea | Date | Result |
|---|---|---|
| **Market-regime filter** (Kaufman efficiency ratio, entries only) | 2026-10-01 | **Rejected.** In-sample Sharpe fell at every setting, so Gate 1's in-sample selection turns it off for S1; S8 picked 0.25 and came out worse out-of-sample (0.778→0.768) and in walk-forward (0.671→0.638), while tripling the grid to 54 trials. At *fixed* parameters it halved drawdown (S1 −16.3%→−8.2%), but that was measured by peeking at out-of-sample and cannot be acted on. Code kept, default off. Worth one pre-registered retest against a **drawdown-aware objective**, since we select on Sharpe and Sharpe does not reward what it does |
| **Post-earnings announcement drift** (buy after a positive EPS surprise) | 2026-10-03 | **Rejected.** Event study, 891 announcements across our 10 individual stocks, 2002-2026, measured as excess over each stock's own average return for the same horizon. Beats of >5% produced **−0.15% / −0.28% / −0.58% / −0.06%** at +1/+5/+20/+60 days: no drift at all. The only real signal was a **−4.13% drift over 60 days after a MISS** — which the no-shorting rule makes untradeable. The test is asymmetric and that is why the rejection holds: the sample is 2026's mega-caps chosen by hindsight, and Yahoo's stored EPS estimates are as-recorded-now rather than consensus-at-the-time, so **both biases favoured finding drift and it still was not there**. Consistent with PEAD having decayed in heavily-analysed large caps |
| **Portfolio volatility targeting** (scale weights down when the book's own vol exceeds a target) | 2026-10-03 | **Rejected on in-sample alone.** Nothing to manage: S1 already runs at **7.5%/yr** realised volatility and S8 at **9.9%**, so a normal 12% target binds on only 6.9% / 15.4% of days and changes Sharpe by ≤0.006. Forced to bind (an 8% target, below S8's 8.5% median) it makes S8 *worse*: 0.698 → 0.670, CAGR 6.38% → 5.74%. The existing machinery — ATR-derived position size, 1% risk per trade, exposure caps — has already done this job; exposure averages just 30% (S1) and 70% (S8). Vol targeting's documented benefit is for books running at 15-20%. `run_backtest(vol_target=…)` kept as research infrastructure, defaulted off and tested |
| **Economic calendar blackout** (no entries N days before US payrolls) | 2026-10-03 | **Rejected on in-sample alone** — out-of-sample never touched. S8 got monotonically worse (0.698 → 0.690 → 0.672 → 0.659 at 1/2/3 days); S1 moved +0.011 Sharpe by blocking **2 trades out of 454**, which is noise. Structural reason: trend following holds for *weeks* and enters only a few hundred times in 22 years, so a narrow pre-event window almost never catches an entry — and where it did bite, the trades it removed were profitable |
| **Jev / TypeSafe AI as the decision model** | 2026-10-03 | **Rejected, revisit only if S5 proves itself.** Jev returns typed answers (`Choice`/`Score`/`Noul`) with probabilities in 70–500ms at $0.042/M input tokens, and cannot produce a type error or hallucinate a value. The shape genuinely fits S5's approve/veto — a boolean with a confidence the gate could threshold on, better than parsing a free-text verdict. It was rejected because **speed of decision is not this system's bottleneck**: S1's decision is a moving-average comparison that takes microseconds, so Jev would make the loop ~1000x slower, not faster. There is no AI in the live decision path at all. S5 and S6 have never run, so there is no evidence AI adds anything here, and the forex rules it would judge (S9) have a NEGATIVE out-of-sample Sharpe — deciding faster would lose faster. It also adds a paid API dependency that this project was explicitly built to avoid, and cannot hold the dashboard's conversation. Independent review of Jev for trading says the same: it "should not be the sole authority for market calculations or order execution." **Revisit when:** S5 is switched on, runs 90 days against S1 as its A/B control, and the veto demonstrably adds something |


#### Desktop layout, second pass (2026-10-03)
Worked from real designs rather than memory — Dribbble shots by Shakuro, U1CORE (Zeex), Ronas IT and the
EchoFi concept. Three things those share that this dashboard did not:

* **Instrument lists are flat dense rows inside one container, not a card each.** A card per symbol spends
  ~76px of height on border and padding to show two numbers, which fitted four symbols in the column; the
  rows fit about fourteen. You are meant to scan a market, and a market you have to scroll is one you do
  not check.
* **The chart names its instrument like a headline.** A 12px all-caps label is right for "Account balance"
  and wrong for "EUR/USD", which is the subject of the whole panel.
* **Holdings and per-strategy performance belong on the landing page.** Every reference puts them there;
  this dashboard had open positions a tab away inside a 13-column table.

Also balanced the objectives tiles to a fixed 4x2 — `auto-fit` fitted seven and stranded the eighth on a
row of its own.

One rule fell out of it. Writing a test that every percentage states its baseline caught three that did
not: holdings (`+1.10%` — since when?), the benchmark sleeve's own return, and anything measured `vs B0`.
All now say `since entry`, `since start` or `vs B0`. A percentage with no baseline is decoration.

#### Interface rules (2026-10-03 redesign)
Researched against how trading and fintech interfaces are actually built, then applied as four rules that
`tests/test_design_system.py` pins. Each is a decision, not a preference:

1. **Numerals are the typography that matters.** Every figure is tabular (`font-variant-numeric` on `body`,
   inherited). Proportional digits make a live price jitter sideways on each tick, which reads as the data
   being unstable rather than the font.
2. **Colour means state, nothing else.** Green and red are reserved for profit and loss and never used for
   decoration. Measured, the green and the red are only **1.52:1** against each other, so colour cannot carry
   the meaning for the ~8% of men with red-green deficiency — every figure carries a `+`/`−` sign and every
   headline line carries a glyph (`✓ ! × ·`). The sign is the signal; colour is a second channel on top.
   The brand amber is deliberately neither state colour, so chrome can never be mistaken for a result.
3. **Elevation from surface, not shadow.** Dark backgrounds swallow shadows, so depth is flat steps of
   lightness (`--page` → `--s1` → `--s4`). No gradients anywhere data is shown: a gradient behind a number
   changes that number's contrast across its own width. The card gradient was removed for this reason.
4. **Show the exception.** Space goes to what needs attention; what is fine stays quiet.

Contrast was computed rather than eyeballed: against both page and card, every text token clears WCAG AA
and most clear AAA.

Two real bugs surfaced during the redesign and were fixed. `nice()` rounded its first gridline tick *down*
below the low and ran past the high, and `.chart svg` is `overflow:visible`, so the outermost labels escaped
the plot — on a 400px FX chart they drew at **y=−24** (landing on the chart's own subtitle, which looked
like a text-overlap bug) and **y=460**. Both chart renderers clamp now. Separately the x-axis drew a fixed
six date labels regardless of width, which collided into `21 Jul5 Aug20 Aug` in a narrow pane; the count now
comes from the available width.
| **London opening-range breakout, intraday FX** (S-IRB) | 2026-10-03 | **Rejected on real tick data.** 212 trading days of Dukascopy EUR/USD 5-minute bars with the spread actually quoted (median 0.30 pips). At the defaults: 161 trades, 32.9% win rate, **gross −$64 BEFORE any spread**, spread then −$611, net **−6.75%** over ten months. The cost floor was not the binding problem after all — there is no gross edge for the cost to eat. Selected cleanly (best of 18 settings on the first 60%, measured once on the rest) it returned **−13.67% in four months, ≈−36%/yr**, gross −$1,116; 8 of the 18 were profitable in-sample, which is noise around zero, and taking the best of them was actively harmful. A deliberate methodology note: an earlier check that chose three settings using the WHOLE period and only then split it showed them "holding" at +$31 — the identical data inverts its answer depending on whether selection saw the test set. Also measured: `min_range_pips` never binds (median opening range 12.9 pips; 0 of 212 sessions below 3) |


#### Intraday attempt 2: session mean reversion (2026-10-04) — PROMISING, NOT PROVEN
The opening-range breakout failed because it had no gross edge. This is the other mechanism rather than
the same one reparameterised: daily FX trends on carry and macro flow, but *inside* a session it is
dominated by liquidity provision, so a sharp move away from the session open tends to overshoot and come
back. It therefore has a target (reverting means reverting TO something), which trend following must not.

Tested on 272 trading days of Dukascopy EUR/USD 5-minute bars with the spread actually quoted. Chosen
cleanly — 30 settings on the first 60%, then measured ONCE on the rest:

| | in-sample | out-of-sample |
|---|---|---|
| best setting (drop 0.5 / stop 0.3 / target 1.0) | +$1,161 | **+$679 = +17.2%/yr** |
| gross, out-of-sample | | +$792 against $113 of spread |
| settings net positive | 27/30 (90%) | **23/30 (77%)** |
| settings gross positive, out-of-sample | | 25/30 (83%) |

That 77% is the number that matters: the breakout managed 8/18 in-sample and then −36%/yr out-of-sample,
which is what noise looks like. Here the whole neighbourhood survives, and the chosen setting is not an
outlier within it (+$595, +$756, +$636 sit beside it). Mechanics check out too — stops fire 13 times in
34 trades and R is capped at −1.02, so nothing is running away.

**It is not proven and must not be traded yet.** Three things are missing. It is ONE pair, and Gate 1's
"works on most of its symbols" check exists precisely for this — AUD/USD and GBP/USD ticks are
downloading. Out-of-sample is 34 trades against Gate 1's bar of 50. And the long-only rule means it can
buy dips but never sell rallies, so if falls and rises revert differently this test cannot see it.

##### The other two pairs came back, and they do not confirm it (2026-10-05)
EUR/USD **+17.2%/yr**, GBP/USD **+3.0%/yr**, AUD/USD **−1.3%/yr** out-of-sample. So the breadth check
that mattered most is the one it effectively failed: one pair out of three carries the whole result.

The cause is measurable rather than mysterious, which is the only reason this was not abandoned outright.
The edge is a fixed number of pips per trade, and the spread is subtracted from it, so the pair with the
tightest spread keeps the most: EUR/USD's median is 0.5 pips, GBP/USD's 0.9, AUD/USD's 1.3. Gross of
costs all three are positive. Net, only the cheapest one survives, and **no filter rescues AUD/USD** —
0.0% of its bars trade under 0.5 pips, so there is no subset of the day where it is cheap enough. That is
not a parameter that needs tuning; it is the pair being too expensive for an edge this size.

What that licenses is narrow. Running this on EUR/USD alone is defensible, because the mechanism is
understood and the one pair where it works is the one where the arithmetic says it should. Running it on
a basket is not. So it ships as **EUR/USD only, as messages for you to place by hand, with no capital
and no Gate 2 claim** — an honest shadow, not a sleeve.


### Phase 5: Tournament (≥ 90 days, mostly waiting)
- [ ] Weekly scoreboard in Telegram + dashboard
- [ ] Monthly experiment session with you: run the proposed experiments in the research lab; approved ones become challenger versions (e.g. S3 v2 vs v1) logged in `Changelog.md`
- [ ] Monthly check-in: retire losers, research replacements (new rule sleeves pass Gate 1 first)
- [ ] Optional: ASX via IBKR paper
- **Exit:** a Gate 2 report per sleeve saying which, if any, are candidates for live money.

#### S8 widened 19 → 40 markets (2026-10-03)
Done to compress **time**, not to chase return. Trade count scales with breadth, and breadth is what sets how
fast a sleeve can be judged: measured over 31 years of history, 19 markets complete **7.0** trades a month and
40 complete **15.1**, so the 50 trades Gate 2 asks for arrive in **3.3 months instead of 7.2**. For contrast S1,
on 4 markets, completed **zero** round trips in its first 15 days live — it bought at inception and held. This is
the honest lever. The dishonest one is trading more often per market, and the intraday research already measured
where that ends: 5 trades a day costs 4.6%/yr in spread against S1 earning 4.4%/yr in total.

Gate 1 re-ran on the 40-market basket and **PASSED** all eight checks (out-of-sample Sharpe 0.85 vs buy-and-hold
0.62, worst drop −19.0%, 4261 trades, neighbours keep 95% of the Sharpe, deflated Sharpe 99.3% after 90 distinct
trials, profitable out-of-sample on 29 of 40 symbols). Note the 19-market basket's much-quoted **0.98 was a
one-off from its first run and never reproduced** — its three most recent runs scored 0.78 / 0.77 / 0.79, so 0.85
is an improvement on the real baseline, not a decline from 0.98.

The old shadow run was archived as sleeve **S8S** rather than carried over: its 10 positions were never actually
bought, so reconciling them against the paper account would have flagged every symbol and refused to trade it,
and a sleeve whose history is half shadow and half paper reports a meaningless return. No fills were invented.

**S8 ships DISABLED, blocked on one number.** A 40-market cold start needs **80 orders** (an entry and a
protective stop each); `risk.yaml` caps the whole account at `max_orders_per_day: 60`, shared with B0 and S1,
and **shadow orders count too** — they get a `broker_order_id`. Measured, not guessed: with the cap lifted a cold
start placed exactly 80; at 60 the entries fill and then the stops are refused, leaving open positions
unprotected for the rest of the day. That is the one failure this system exists to prevent. Raising
`max_orders_per_day` to ~150 covers a cold start plus stop maintenance and still catches a runaway loop (which
would be thousands). `test_s8_at_the_shipped_order_budget_starves_its_stops` pins the defect.

**Resolved 2026-10-08.** You authorised the edit, so `max_orders_per_day` is now **150** with a comment
in `risk.yaml` recording why. S8 is **live at $33,000** on the paper account.

#### S1 retired into S8, and what the broker taught us (2026-10-08)
S8 going live made S1 redundant and then actively harmful. S1 trades 4 markets that are a strict **subset**
of S8's 40, with identical rules, so both sleeves wanted the same symbols at the same moment — and Alpaca
refuses a buy while an opposite-side order rests on the same symbol (code `40310000`, a wash trade). S1 was
not adding diversification; it was blocking S8 from four of its positions. It is therefore `retiring: true`:
it wants nothing, sells through the ordinary exit path so stops are cancelled and every close is journalled,
and switches itself off once flat.

Three bugs surfaced only against the real broker, none of which a backtest could have shown:

1. **The wash-trade rejection itself.** Now detected *before* submitting, by looking for another sleeve's
   resting stop on the symbol (`_blocking_stop`). Limit orders are rejected too — Alpaca's advice to "use
   complex orders" means brackets or OCO, not a limit price.
2. **An unconfirmed cancel was being trusted.** The engine marked a stop `CANCELED` on asking. An equity stop
   cancelled while the market is shut sits at `pending_cancel` until the open, so our journal said cancelled
   while Alpaca still said otherwise on both SPY and QQQ. Asking is not the same as it being done, so the
   order now stays open until the broker's own answer comes back.
3. **The same exit sent every run.** A retiring sleeve re-decides every cycle by design, and its first sell
   was still queued against a shut market. The broker reserves the shares behind that order and refused each
   repeat — 8 rejections overnight. Fixed by subtracting what is already in flight.

That third fix is worth recording for its **first attempt, which was wrong and was reverted**. Skipping any
symbol with an open sell looked right and broke a legitimate exit, because `_cancel_stop` deliberately leaves
the just-cancelled stop open in the journal (bug 2 above): the guard counted it, subtracted the whole
position and sent nothing, leaving a position that should have closed with neither stop nor exit. A fix that
silences a log at the cost of not selling is worse than the noise. The guard now counts only plain sells, and
`test_a_resting_stop_does_not_block_the_exit_it_is_being_replaced_by` pins the trap so it cannot come back.

#### Forex messages to your phone (2026-10-08)
The session-reversion watcher sends EUR/USD levels to Telegram: a plan at the 07:00 UTC London open
(buy level, stop, target, size), a prompt when the rate reaches one, and flat by 16:00 either way. It holds
no capital, touches no broker and places nothing — you execute by hand. It is kill-switch aware on purpose:
a halted system that keeps texting you trades has not really halted.

**The host, not the code, is the limiting factor.** The watcher's levels are measured FROM the 07:00 open, so
a first run that lands hours late would be trading a different setup while looking identical; it refuses the
day instead. On 2026-10-07 that is exactly what happened — the Mac slept from 06:24 to 08:18 UTC and no run
occurred at the open. The engine's `caffeinate -i -s` only holds sleep off **on AC power**, and the machine
was on battery at 17%. So the honest statement of this feature's reliability is: it works when the Mac is
awake and plugged in at 18:00 Sydney, and skips the day otherwise. A skipped day now **sends a message
saying so**, because a feature whose purpose is to message you must not fail by going quiet. Making it
sleep-proof needs a system power setting (`pmset`), which is yours to change, or a host that does not sleep.

Verified live on 2026-10-08: the scheduled engine built the plan at **07:05 UTC** and sent it, three
minutes after the open and inside the 30-minute window. That is the first time this has worked without
being run by hand.

The same sleep problem has a sharper edge at the other end of the day. `close_out` needs a run after
16:00 UTC — 3am Sydney, the most likely moment for the Mac to be asleep. Slept through, the day keeps
state `in` for ever: **you were told to buy and never told to sell**, and the next morning a fresh plan
is built straight over the top. An alert that opens a trade and then goes quiet is worse than one that
never fires, because you are the one holding the position. Every run now sweeps for a day left open and
says so once, quoting no exit price — the rate at a close nothing was awake for is not a number to
invent, so it points you at your broker instead.

##### Three bugs in the sell half, found by exercising it rather than waiting (2026-10-08)
The buy went out at 07:05 and the sell path had never run live, so it was driven deliberately through
every outcome — target, stop, session end, and a gap — against a copy of the real journal using the
day's real plan. All three of these were live defects, not test gaps:

1. **The target you are told was not the target being watched.** `target_frac 1.0` makes the target the
   session open, so it is almost never a round number: 1.118943691 prints as "Target 1.11894", and a rate
   of exactly 1.11894 then failed `rate >= target` by four ten-millionths. The day you watched the rate
   touch your own number was the day nothing was sent. The levels are now rounded to the precision they
   are printed in, so the number in the message is the number being compared.
2. **A gap through both levels was sent as a buy.** Crossing the trigger and the stop inside one
   fifteen-minute window — an ordinary news spike — produced "BUY now" quoting a stop *above* the entry,
   and the next run would have told you to sell at a loss. The tested rule enters with a resting limit at
   the trigger, so either that filled and the stop closed it, or it never happened; a fresh entry below
   the stop is a different trade. The day is now skipped, with a message that says which case you are in.
3. **The result was measured from the trigger, not from where you were told to buy.** The buy fires on
   the first observation at or *under* the trigger, so the sell understated every gain. Wrong in your
   favour is still wrong, and it would read as a mistake the first time it was checked against a broker
   statement. The signalled entry is now recorded and reported.

Bug 1 is the one worth remembering: it cannot be caught by any test whose numbers are tidy. The suite had
used a flat 20-day range, which makes `drop_atr * atr` land exactly on five decimals by luck and hides
the whole class of defect. The tests now use ranges shaped like real EUR/USD days.

##### And the levels were sized off the wrong number entirely (2026-10-08)
The watcher took its ATR from Yahoo's daily bars while the research used the range of its own Dukascopy
5-minute bars over the whole UTC day. A comment said the two "differ a little at the edges of the day;
it moves the levels by a fraction of a pip". That was wrong by most of an order of magnitude. Measured
over the 268 days where both sources exist:

| 20-day ATR, versus the bars the research used | median error | mean | worst |
|---|---|---|---|
| hourly bars grouped into whole UTC days | **1.2 pips** | 1.2 | 2.0 |
| Yahoo's own daily bars *(what was live)* | **6.9 pips** | 8.3 | 21.0 |

Yahoo's FX daily bar understates the range — its day does not start at midnight UTC and it misses
extremes in thin hours — and it understates it in one direction, so the error does not average out.
Eight pips of ATR moves the buy level about 3.5 pips and tightens the stop by about 9%, which means the
live version entered earlier and stopped out more often than the one that produced +17.2%/yr. Sizing a
strategy off a different statistic from the one it was tested on makes it a different strategy.

It now reads hourly bars and groups them into whole UTC days, with a day of fewer than 20 hourly bars
discarded as a weekend, holiday or partial feed — all of which understate a range. On today's data that
moves the ATR from 54.7 to 59.7 pips, the buy 2.4 pips deeper and the stop from 16.4 to 17.9 pips.
There is deliberately **no fallback** to the daily bars: falling back would quietly resume trading the
biased version, so a failed fetch means no plan, and the missed-open message says so.

Two things this did not change. Today's plan was already sent, so it keeps the levels on the phone
rather than being quietly re-pointed at different ones. And the +17.2%/yr figure still stands on the
tick data, because this brings live *towards* that test rather than away from it.

The lesson, recorded because it will recur: a comment asserting a quantity ("a fraction of a pip") is a
measurement nobody took. The DST question was checked the same way and came out clean — the research
uses a fixed `time(7, 0)` on UTC ticks and so does the engine, so live and backtest agree even though
07:00 UTC is 08:00 London in summer and 07:00 London in winter. That inconsistency is in the *research*,
not in the gap between research and production, and closing it would mean trading something untested.

##### The loudest alert carried the quietest caveat (2026-10-08)
S9 is a shadow sleeve: it places nothing and its fills are simulated. It also runs the FX rules that
**failed Gate 1 at an out-of-sample Sharpe of −0.184**, and it is in `HAND_EXECUTED`, so its messages go
to the phone. They read:

> 🟢 S9 Bought 60000 EUR/USD @ 1.11894 (trend up)
> 🛑 Stop 1.11456 · risks $263 if it is hit

Identical in shape to a real fill from the paper account, with a stop and a dollar figure beside it —
the most actionable-looking thing this system sends, for the one strategy it has concluded does not work.
The dashboard states that verdict plainly; the phone did not state it at all. Meanwhile the intraday
watcher, whose test result is *positive*, ends every message with "Shadow test, EUR/USD only. Not
advice." The caveats were exactly the wrong way round.

Shadow fills now say "would have bought", carry a 👻 rather than a green circle, and end with a line
saying nothing was traded, that these rules failed Gate 1, and not to place the trade. Real fills are
untouched: hedging a real fill with a caveat that does not apply is its own bug.

Found alongside it: three places asked "is this a shadow broker?" as `name != "shadow"`, an exact match,
so every shadow broker except the one literally called "shadow" was messaged regardless of
`HAND_EXECUTED`. It agreed with `_should_notify` only by coincidence — `shadow_fx` happens to be
hand-executed — and a second shadow broker would have been texted to the phone while the method that
supposedly decides said to stay quiet. One rule decides it now.

The same defect was then found in the sibling path, where it was worse. `_stop_move_text` is reached
*only* for a hand-executed broker, so every message it has ever sent was for that failed strategy — and
it ended with a flat instruction: "Move your stop if you are following this by hand." There is no
position behind it. Telling someone to adjust a real position on behalf of rules that lost to T-bills is
the most direct way this system could cost money — more direct than the fill message, because this one
is phrased as something to go and do. It now says a stop *would* move and not to move a real one. A
genuinely hand-executed real broker, if one is ever configured, still gets the instruction it needs.

Sweeping every message that can reach the phone — rather than guessing where else the same mistake was —
found four more of it. A shadow sleeve announced that it "closed its EUR/USD position" (it held none),
that an order was "Queued, fills when the market opens" (none was sent anywhere), that a retirement
"sold the last of its positions", and, in the **daily summary**, listed itself as "shadow_fx paper
account $9,973" directly beneath the real Alpaca account with its return beside the real sleeves'. That
last one is the message most likely to be read and least likely to be questioned: a glance down the list
read a simulation of a losing strategy as money that had been made. It now reads "simulated, NOT an
account". None of these were actionable on their own, which is exactly why they had survived.

**Then switched off entirely, which was the right fix all along (2026-10-08).** Labelling those messages
treated the symptom. S9 runs rules this system has already concluded lose to T-bills; sending a buy, a
stop and a dollar figure to a phone for them is a standing invitation to act, however carefully the
caveat is worded. `HAND_EXECUTED` is now empty and S9 is silent. It keeps running and keeps accumulating
evidence on the dashboard and in the daily summary, marked as simulated — it just no longer interrupts.
The shadow-aware message wording stays as a guard for whatever is added to that set next, and the set's
comment now says that anything added to it goes straight to the phone.

##### A rule that was right, tested, and never reached production (2026-10-08)
The risk gate judges a price by age, with one deliberate exception: when a market is shut, the last
session's close is as fresh as a price can be, so it is accepted rather than called stale. Forex is in
that exception on purpose — `test_a_closed_forex_market_accepts_the_last_session_price` says "without
this, an FX sleeve could never act on a Sunday; the price is always ~36 hours old".

It never worked. Every test of it builds the gate's context by hand and passes a clock in. The executor,
which builds the real one, supplied a clock only for US equities — so in production `ctx.market` was
`None` for forex, the FOREX arm of the check was unreachable, and a weekend FX order was refused as
"price is 171982s old (limit 300s)". Two of the nine refusals in the journal say exactly that: the feed
blamed for a market that was simply closed. `fx_clock` already existed and the dashboard was already
using it; only the executor had never been wired to it.

Worth recording how this nearly went wrong. The first fix changed the *gate* to refuse forex while the
market is shut, which reads sensibly in isolation and contradicted a decision the project had already
made and written a test for. The two failing tests were the record of that decision. The real fix is one
line in the executor, and the new test asserts on the context the executor actually builds — because a
unit test on the gate cannot catch a caller that never passes the argument.

##### An entry sized by weight and judged by its stop (2026-10-08)
The allocation comes from the strategy's target weight. The gate then judges it by how far the stop is.
Nothing connected the two, so when a wide stop met a large weight the gate refused the order **outright**
and the sleeve held nothing — the same shape of failure as the $544 allocation that floored to zero
shares and left S8 without SPY, QQQ and MDY. S1 wanted $1,211 of ETH/USD behind a stop 8.3% below,
risking $100.67 against a $100 budget, and was refused over **67 cents**.

The backtest applies no per-trade risk cap at all; it sizes on weight alone. So this check is a
live-only rule and it makes live differ from the test whichever way it goes — and holding **97%** of the
tested position is a far smaller divergence than holding none of it. Entries are now shrunk to fit the
budget, and the shrink is reported rather than done quietly.

The test for it was written wrong first, in the same way as the bug above: it called the helper directly
and passed with the call site deleted. A unit test on a helper says nothing about whether anything calls
it. There is now a test that goes through the entry path and checks the order that reaches the broker.



### Phase 6: Live readiness (separate plan, only if something passes)
IBKR/OANDA AU live accounts, small sizes, manual approval at first, ATO record-keeping.
- **Planned starting capital: ~US$100** (noted 2026-09-18). Needs a *small-account mode*: fractional/dollar-amount
  orders, engine-checked stops where the broker can't hold them (fractional shares), and only the 1–2 strategies that
  pass Gate 2. $100-friendly venues: OANDA (1-unit minimum) and Alpaca crypto (~$1 minimum). Not suitable at $100:
  Pepperstone (1,000-unit minimum needs leverage), IBKR ($2 minimum per order), Mitrade (no API).
- Add overnight forex financing to S4's cost model before any live forex.

---

## 9. Running costs and subscription usage (paper phase)

| Item | Cost | Notes |
|------|------|-------|
| Brokers, market data | Free | Alpaca paper, yfinance, OpenBB (OANDA and Liquid paper also free from 4b) |
| Claude | **Your existing subscription** | Building, research and scheduled tasks all draw from your plan's usage limits |
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
6. ~~QuiverQuant~~ **Not needed** (decided 2026-09-18). S7a (Congress copy) dropped: evidence says it lags the S&P 500. S7b (insider cluster buys) optional later, built on free SEC EDGAR data and put through Gate 1 first.
7. **Learning loop:** AI sleeves may use learnings immediately, but rule strategies change only via experiment → your approval → challenger version (recommended); or also let the weekly review auto-apply small parameter changes (not recommended).

---

### Sources
Videos: [Miles Deutscher: 7 GitHub repos](https://www.youtube.com/watch?v=me5uegV7vOU) · [Samin Yasar: Claude trading agent](https://www.youtube.com/watch?v=x2pY9kI0zBY) · [Miles Deutscher: $168,236 bot](https://www.youtube.com/watch?v=VDpTU5kdj8A) · [DaviddTech: make as much money as possible](https://www.youtube.com/watch?v=wYuP1QFZ0Bg) · [Ale Ser: ETH "arbitrage bot" (scam pattern)](https://www.youtube.com/watch?v=vDUoCJ7F8xU)
Research: [DXRG: Can Claude trade?](https://www.dxrg.ai/blogs/can-claude-trade) · [FINSABER](https://arxiv.org/html/2505.07078) · [Look-ahead bias benchmark](https://arxiv.org/pdf/2601.13770) · [Alpha Arena](https://nof1.ai/) · [Humbled Trader: Claude + IBKR bot](https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/) · [Congress trades study](https://equibles.com/research/what-happens-after-congress-buys-or-sells-a-stock-evidence-from-17-859-disclosures)
Claude: [Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks) · [Routines](https://code.claude.com/docs/en/routines) · [Claude Code with Pro/Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan) · [Agent SDK / claude -p with your plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
Brokers & data: [Alpaca paper trading](https://alpaca.markets/learn/start-paper-trading) · [Alpaca paper limitations](https://docs.alpaca.markets/us/docs/paper-trading) · [OANDA Australia API](https://www.oanda.com/au-en/trading/platforms/rest-api/) · [IBKR paper](https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/) · [Liquid docs](https://docs.tryliquid.xyz/llms.txt) · [QuiverQuant](https://www.quiverquant.com/)
Repos: [TradingAgents](https://github.com/TauricResearch/TradingAgents) · [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) · [OpenBB](https://github.com/OpenBB-finance/OpenBB) · [alpaca-mcp-server](https://github.com/alpacahq/alpaca-mcp-server)
