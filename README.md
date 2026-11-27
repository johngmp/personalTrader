# Personal Trader

A trading system for one person. Most of it trades pretend ("paper") money in US stocks, ETFs
and crypto, purely to build evidence about whether any of its strategies actually make money.
The one thing it asks a human to do by hand is place a EUR/USD forex ticket in MetaTrader 4 on
an iPhone, when the system finds a setup worth taking. Everything else — the research, the
backtesting, the day-to-day running — happens on its own.

See [`VISION.md`](VISION.md) for what this project is trying to prove and why, and
[`PLAN.md`](PLAN.md) for how it is built. This document is only about running it.

## Installing

This project uses [`uv`](https://docs.astral.sh/uv/) to manage its Python environment and
dependencies. From the repo root:

```sh
uv sync
```

That creates a `.venv` and installs everything `pyproject.toml` lists. Every command below is
run the same way, from the repo root: `uv run trader <command>`.

## Setting up `.env`

Copy the example file and fill in your own keys — never type them into a chat message, and
never commit `.env`:

```sh
cp .env.example .env
```

What goes in it:

- **`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`** — your Alpaca **paper** account keys (switch to the
  Paper account in the Alpaca dashboard before generating them; paper keys start with `PK`, and
  live keys are refused on sight).
- **`TELEGRAM_BOT_TOKEN`** — from [@BotFather](https://t.me/BotFather) in Telegram: message it,
  send `/newbot`, pick a name and a username ending in `bot`, and paste the token it gives you.
  Don't paste the token into a chat with the bot itself — only into `.env`.
- **`TELEGRAM_CHAT_ID`** and **`TELEGRAM_FX_CHAT_ID`** — don't fill these in by hand. The two
  `telegram-setup` commands below find them for you and write them into `.env`.
- `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID` and `HEARTBEAT_URL` are optional and only needed if
  you've set those integrations up; leave them blank otherwise.

## Connecting Telegram

The system uses **two separate Telegram conversations**: a main chat for everything the engine
does, and a second, quieter chat just for forex tickets, so the one message a day you actually
need to act on isn't buried among dozens of paper-trading alerts.

### `uv run trader telegram-setup`

Sets up the **main chat**. Message your bot in Telegram first (open it, press Start, or just say
"hi"), then run:

```sh
uv run trader telegram-setup
```

This finds your chat, saves `TELEGRAM_CHAT_ID` to `.env`, and sends a one-time "connected"
message confirming it worked. The engine starts using it from its next run (within 15 minutes).

### `uv run trader telegram-setup --fx`

Sets up the **forex chat**, a separate Telegram *group* (not your private chat with the bot) that
only ever receives EUR/USD tickets. Run `telegram-setup` (above) first — this needs the bot token
it relies on. Telegram won't let a bot create or join a group on its own, so you make the group
yourself:

```sh
uv run trader telegram-setup --fx
```

If no group is visible yet, it prints exact steps: create a group in Telegram, add the bot by
its bare username (no `@`), name the group, and send `/start@<botusername>` inside it (a plain
message won't reach the bot — that `/start` command is what does). Run the command again
afterwards. Once it finds the group, it saves `TELEGRAM_FX_CHAT_ID` to `.env` and sends a
confirmation message there.

## Telegram commands

Each command only works in the chat it's listed under — `/halt` typed into the forex chat does
nothing, and `/taken` typed into the main chat does nothing either. That's deliberate: the forex
chat only ever answers forex tickets, and the main chat only ever answers everything else.

### Main chat

| Command | What it does |
|---|---|
| `/status` | Account balance, every sleeve's position, the kill switch state, recent orders. |
| `/open` | Every position currently open, with entry price, current stop and unrealized P&L — including the forex ticket, if one is open, even though no broker holds it. |
| `/today` | Today's forex plan: the EUR/USD levels for the day, or a plain "no plan yet" / "switched off" if there isn't one. |
| `/halt` | Turns the kill switch **on**. No new orders go out; stops already resting at the brokers stay in place. |
| `/resume` | Turns the kill switch back **off**. Trading resumes on the engine's next run. |

Anything else typed in the main chat gets a reply listing these five commands.

### Forex chat

| Command | What it does |
|---|---|
| `/taken <price>` | Records that you placed the EUR/USD ticket in MT4 at that price, e.g. `/taken 1.08421`. |
| `/skipped` | Records that you decided not to take today's ticket. |

Anything else typed in the forex chat — including the main chat's commands — gets a reply
listing just these two.

## Paper vs live, in plain terms

**Paper** means every trade is make-believe: the broker (Alpaca) keeps a pretend account that
behaves like a real one — real prices, real fills — but no real money ever moves. That's the
only mode this system runs in today. Going **live** would mean real money is actually at risk,
and this project treats that as a decision a human makes deliberately, not something a config
change or a good-looking result switches on by itself. Five independent layers in the code exist
purely to refuse to place a live order even by accident, and none of them are something
day-to-day use of the system can turn off.

The one exception is the forex side: there, "placing the trade" always means a human types a
ticket into MetaTrader 4 by hand. The system doesn't have a live connection to that broker at
all — it can only tell you what it would do.

## Sleeves, in plain terms

A **sleeve** is one strategy, trading its own slice of the (paper) account, kept completely
separate from every other strategy. Instead of one pool of money with everything mixed together,
each idea — "follow the trend", "buy dips", "follow an AI panel's picks" — gets its own virtual
account and its own scoreboard, measured against a plain benchmark like buy-and-hold.

The point of splitting it up this way is honesty: if sleeves shared one account, a strategy that
is actually losing money could hide behind one that's winning. Keeping them apart means each
idea has to prove itself on its own, and a losing one can be switched off without touching
anything else. `/status` shows where every sleeve currently stands.
