# Vision

A personal trading system for one person. Its job is to send a forex ticket to a phone each
weekday and to tell the truth about whether anything it does makes money. It trades paper
money in equities and crypto to build evidence; the only trades a human places by hand are
EUR/USD tickets typed into MetaTrader 4 on an iPhone.

The hardest thing this project does is not finding a strategy. It is being able to say "no".
Two 2026 papers on LLM-run trading research found that a loop which keeps whatever improves
the metric is a machine for producing false discoveries: across two dozen campaigns, almost
nothing survived honest evaluation, while passive buy-and-hold benchmarks did. This project is
built on the assumption that it is the same, and most of its machinery exists to catch itself.

That assumption is currently being borne out. As of 2026-11-23, no sleeve has a passing Gate 1
on data the seal allows. S8 fails Gate 1 out-of-sample (Sharpe 0.656 vs buy-and-hold 0.976) and
fails five of nine Gate 2 checks on its live record. The FX trend sleeve's out-of-sample excess
Sharpe is negative. The one thing with positive out-of-sample evidence is the EUR/USD intraday
watcher: +4.0%/yr over 34 trades — thin, one pair, and the only pair that passed.

## Goals
What this should achieve in the next months, in priority order:

1. **The forex loop arrives, every weekday, without being chased.** A ticket at the London open
   or a note saying there is no setup, in the Forex tickets Telegram group, with every field
   matching an MT4 screen. This is the product. Everything else is scaffolding for it.
2. **Silence is never ambiguous.** A quiet market and a dead Mac must never look the same. Six
   launchd agents went four days without executing and nothing noticed, because every symptom
   of a dead system was identical to a calm one.
3. **Gate 2 reaches a real verdict on S1 and S8** — 90 days live and 30 closed trades each, on
   settings their own backtest chose, measured against the benchmark over the same days.
4. **The evidence gets sharper, not larger.** Fewer strategies, better measurement. A real edge
   should be unmistakable against this machinery; a fake one should die.

Not a goal: more strategies. On the published evidence and this project's own record, strategy
discovery is the lowest-value direction available.

## Target users
One person, and the design should not pretend otherwise.

- **Non-developer.** Reads messages on a phone and runs the occasional copy-pasted command.
  Error messages are a user interface: a traceback is a bug in the message, not in the reader.
- **Australia (AEDT).** The London session runs 6pm–3am local. Times shown to the user belong
  in their timezone, and a rule that needs them awake at 3am is a rule they cannot follow.
- **MetaTrader 4 on an iPhone**, by hand. There is no broker connection on that path. Tickets
  must use MT4's own field names and step order — it has no Fill Policy box, and its trade
  screen opens on Market Execution, so a ticket that does not say to change the type buys at the
  wrong price.
- **Claude Pro, no API key.** Anything that needs paid API calls per run is out of reach.
- **$175 in the real forex account.** The broker's 0.01-lot floor, not the 1% risk rule, sets
  the size of every trade: 0.7%–3.1% of the account, about 1.4% typically. Messages say so.

## Principles
- **The engine executes; AI never places an order.** Every order passes the risk gate. An AI
  component may propose, veto or score — it may not trade. This is not negotiable.
- **Structural controls, not procedural ones.** Completeness must come by construction rather
  than by author diligence. A rule in a skill file saying "don't peek at out-of-sample" is worth
  having and not worth relying on; code that cannot load out-of-sample data is worth relying on.
- **A check that cannot fail is worse than no check**, because it occupies the space where the
  answer should be. This project has shipped three: a `GateCheck(..., True, ...)` hardcoded to
  pass, an "Engine heartbeat" row reading a key nothing wrote, and a cost check that happened to
  be right via the median. Each looked green for weeks.
- **Absence of a record must never read as absence of a problem.** A weekday with no state, an
  empty log, a `runs` counter incrementing while nothing executes.
- **Every test is verified by reintroducing the bug it guards.** A test that passes with and
  without its fix is worse than no test: it certifies nothing while claiming to. Roughly one in
  six written here has failed this check and been rewritten.
- **Small, well-tested changes.** One behaviour per change, with the reasoning in the code where
  the next reader will meet it.
- **No new dependencies without a strong reason.** The deflated Sharpe hand-rolls `norm_ppf`
  rather than taking scipy; PBO is 80 lines of numpy. An alpha-status package in the code that
  decides what to trade is a bad trade.
- **Numbers must be measured, and say where.** "Roughly" is acceptable; an unsourced figure that
  turns out to come from a broken harness is how GBP/USD was briefly added on a +10%/yr that did
  not exist.

## Out of scope
Things the factory must never propose:

- **Live trading, or weakening any paper-only guard.** Five independent layers enforce it. Going
  live is a human decision with pre-agreed conditions, not a change.
- **Auto-applying a research conclusion.** Research writes reports and pre-registered
  experiments. A human approves every rule or settings change, and it is recorded in the
  changelog with its reasoning.
- **New strategies, or widening a parameter grid.** Every cell is a trial and raises the bar for
  every future result.
- **Relaxing a gate, threshold or benchmark because its answer is unwelcome.** If a gate measures
  the wrong thing, the fix is a pre-registered experiment with the success bar written first.
- **Choosing settings from the live record.** That spends the forward test and proves nothing.
- **Opening the sealed holdout.** It is a one-shot read, costs a receipt, and is the user's
  command to type.
- **Trading more often.** Measured: five intraday trades a day costs 4.6%/yr in spread against
  S1 earning 4.4%/yr. Breadth is the honest speed lever; frequency is not.
- **GBP/USD or AUD/USD on the intraday rule.** Out-of-sample: −2.0%/yr and −1.3%/yr. The gross
  edge is similar on all three; the spread is not, and no filter fixes it.
- **Editing `config/risk.yaml`.** It says so itself.

## Quality bar
- Every behaviour change comes with tests, and the tests are verified by reintroducing the bug.
- `uv run pytest -q` fully green before merge — 749 passing as of 2026-11-23, and a change that
  reduces that number needs to say which test it removed and why.
- Every backtest records a trial in the hash-chained ledger. Gate 1 refuses to score against an
  altered chain.
- No behaviour change to user-facing messages without rendering one and reading it.
- Error paths get the same care as happy paths: network calls to a host this project's logs show
  timing out need a retry and a readable failure, not a traceback.
- Comments explain *why*, including what was tried and what it cost. The code's history of its
  own mistakes is load-bearing documentation here.

## Protected areas
Paths (globs in backticks) and topics that always need a human. The factory never merges
changes that touch them; they go to your inbox.

- `config/risk.yaml` — the hard limits, enforced on every order
- `config/sleeves.yaml` — which sleeves are enabled, their capital and their parameters
- `config/intraday.yaml` — the forex levels, position size and the broker's lot floor
- `.env` — Telegram credentials and broker keys; never printed, never committed
- `trader/risk/**` — the gate every order passes through
- `trader/brokers/**` — where paper-only is enforced
- `trader/research/holdout.py` — the seal date and the open-once receipt
- `trader/research/ledger.py` — the hash-chained trial record
- `ops/install-*.sh`, `ops/uninstall-*.sh`, `ops/launchd/**` — what runs unattended on the Mac
- `vault/Changelog.md` — the human record of approved changes
- `VISION.md` — this file
- live trading, or the mode/autonomy settings
- position sizing, risk percentages and stop placement
- the sealed holdout, and anything that changes what research data can be read
- gate thresholds, benchmarks and verdict logic
- credentials, secrets and anything sent to an external service
