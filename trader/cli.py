"""Command line: `uv run trader <command>`.

  status          account, positions, kill switch, recent orders
  order --test    send a tiny order through risk gate -> Alpaca paper -> journal
  sync            pick up fills for orders that were still open
  halt / resume   turn the kill switch on / off
  check-config    validate the files in config/
  data            download / refresh daily price history for research
  backtest S1     one backtest with a strategy's default settings
  gate1 S1|all    full Gate 1 evaluation + HTML report in reports/backtests/
  run             one engine cycle (the background schedule runs this every 15 minutes)
  run --dry-run   same, on a copy of the journal: shows what it would do, sends nothing
  news            latest headlines (Alpaca/Benzinga), stored in the journal; --json for AI skills
  fundamentals X  company fundamentals snapshot (Yahoo); --json for AI skills
  serve           dashboard at http://localhost:8050 (read-only)
  telegram-setup  finds your chat ID for the bot in .env and sends a test message
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from trader.brokers.base import Broker, LiveTradingRefused
from trader.config import Settings, load_secrets, load_settings
from trader.engine.executor import ExecutionResult, Executor
from trader.journal.db import Journal, trading_day_start
from trader.models import OrderRequest, Side
from trader.risk.kill_switch import KillSwitch

TEST_SLEEVE = "TEST"


def _broker() -> Broker:
    from trader.brokers.alpaca import AlpacaBroker

    secrets = load_secrets()
    return AlpacaBroker(secrets.alpaca_api_key, secrets.alpaca_secret_key)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def cmd_check_config(settings: Settings, _: argparse.Namespace) -> int:
    enabled = [s.id for s in settings.sleeves.values() if s.enabled]
    print(f"config OK: mode={settings.risk.mode}, autonomy={settings.risk.autonomy}, "
          f"{len(settings.universe.symbols)} symbols, sleeves={list(settings.sleeves)} (enabled: {enabled})")
    return 0


def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    ks = KillSwitch(settings.kill_switch_path)
    broker = _broker()
    account = broker.get_account()
    journal = Journal(settings.journal_path)
    try:
        now = datetime.now(timezone.utc)
        day_pnl = account.equity - account.last_equity
        print(f"Mode: {settings.risk.mode.upper()} (Alpaca paper) · autonomy: {settings.risk.autonomy}")
        print(f"Kill switch: {'ON: ' + ks.reason if ks.active else 'off'}")
        print(f"Last engine run: {journal.kv_get('last_run', 'never')} ({journal.kv_get('last_run_summary', '-')})")
        print(f"Equity {_money(account.equity)} · cash {_money(account.cash)} · "
              f"today {'+' if day_pnl >= 0 else ''}{_money(day_pnl)} · account status {account.status}")
        positions = broker.get_positions()
        print(f"\nPositions ({len(positions)}):")
        for p in positions:
            print(f"  {p.symbol:<9} qty {p.qty:<14g} value {_money(p.market_value):>12}  avg {_money(p.avg_entry_price)}")
        holdings = journal.all_holdings()
        if holdings:
            print("\nSleeve holdings (journal):")
            for (sleeve, symbol), qty in sorted(holdings.items()):
                print(f"  {sleeve:<5} {symbol:<9} {qty:g}")
        print(f"\nOrders sent today: {journal.orders_submitted_since(trading_day_start(now))}"
              f" / {settings.risk.account.max_orders_per_day}")
        rows = journal.recent_order_rows(5)
        if rows:
            print("Recent orders:")
            for r in rows:
                size = f"qty {r['qty']:g}" if r["qty"] else f"{_money(r['notional'])}"
                print(f"  {r['created_at'][:19]}  {r['sleeve_id']:<5} {r['side']:<4} {r['symbol']:<9} {size:<12} "
                      f"{r['status']}")
    finally:
        journal.close()
    return 0


def _print_result(result: ExecutionResult) -> None:
    o = result.order
    print(f"\nOrder {o.client_order_id}: {o.side.value} {o.symbol} "
          f"{'qty ' + format(o.qty, 'g') if o.qty else _money(o.notional or 0)} (sleeve {o.sleeve_id})")
    print("Risk checks:")
    for c in result.decision.checks:
        print(f"  {'PASS' if c.passed else 'FAIL'}  {c.name}{'  ' + c.detail if c.detail else ''}")
    if not result.decision.approved:
        print("\nBLOCKED by the risk gate. Nothing was sent to Alpaca.")
        return
    if result.error:
        print(f"\nAlpaca rejected the order: {result.error}")
        return
    bo = result.broker_order
    print(f"\nAlpaca status: {bo.raw_status}" if bo else "")
    for f in result.fills or []:
        fee = f" · Alpaca fee {_money(f.broker_fee_usd)}" if f.broker_fee_usd else ""
        print(f"Filled {f.qty:g} @ {_money(f.price)}{fee} · estimated extra real-world cost {_money(f.cost_haircut_usd)}")
    if bo and bo.status.is_open:
        print("Not filled yet (stock orders wait for the US market to open). Run `trader sync` later.")


def cmd_order(settings: Settings, args: argparse.Namespace) -> int:
    if not args.test:
        print("Only test orders are available in Phase 1: use `trader order --test`.")
        return 2
    if args.symbol not in settings.sleeves[TEST_SLEEVE].symbols:
        print(f"{args.symbol} is not in the TEST sleeve. Choose one of {settings.sleeves[TEST_SLEEVE].symbols}.")
        return 2
    journal = Journal(settings.journal_path)
    try:
        executor = Executor(settings, _broker(), journal)
        side = Side(args.side)
        if side is Side.BUY:
            quote = executor.broker.get_quote(args.symbol, settings.asset_class(args.symbol))  # type: ignore[arg-type]
            order = OrderRequest(
                sleeve_id=TEST_SLEEVE, symbol=args.symbol, side=side, notional=args.notional,
                # Recorded for the risk check; broker-held stop orders arrive in Phase 3.
                stop_price=round(quote.price * 0.97, 2),
                reason="pipeline smoke test", is_test=True,
            )
        else:
            held = journal.sleeve_holdings(TEST_SLEEVE).get(args.symbol, 0.0)
            at_broker = sum(p.qty for p in executor.broker.get_positions() if p.symbol == args.symbol)
            if held > at_broker:
                # Safety net: never ask to sell more than the broker says is there. Tiny gaps are just the
                # broker rounding quantities (Alpaca uses 9 decimals for crypto); bigger ones are worth a note.
                if held - at_broker > held * 1e-3:
                    print(f"Note: journal shows {held:g} but Alpaca holds {at_broker:g}; selling {at_broker:g}.")
                held = at_broker
            if held <= 0:
                print(f"The TEST sleeve holds no {args.symbol}, so there is nothing to sell.")
                return 1
            order = OrderRequest(sleeve_id=TEST_SLEEVE, symbol=args.symbol, side=side, qty=held,
                                 reason="close pipeline smoke test", is_test=True)
        result = executor.submit(order, wait_for_fill_s=args.wait)
        _print_result(result)
        return 0 if result.decision.approved and not result.error else 1
    finally:
        journal.close()


def cmd_sync(settings: Settings, _: argparse.Namespace) -> int:
    journal = Journal(settings.journal_path)
    try:
        fills = Executor(settings, _broker(), journal).sync_open_orders()
        for f in fills:
            print(f"Filled {f.side.value} {f.qty:g} {f.symbol} @ {_money(f.price)} (sleeve {f.sleeve_id})")
        print(f"{len(fills)} new fill(s); {len(journal.open_orders())} order(s) still open.")
    finally:
        journal.close()
    return 0


def _research_bars(code: str, settings: Settings, refresh: bool = False):
    from trader.data.history import load_many
    from trader.research.evaluate import benchmark_symbols, strategy_setup

    _, symbols, _, _ = strategy_setup(code, settings)
    return load_many(sorted(set(symbols) | set(benchmark_symbols(code, symbols))), refresh=refresh)


def cmd_data(settings: Settings, args: argparse.Namespace) -> int:
    from trader.data.history import YAHOO_TICKERS, load_history

    for symbol in YAHOO_TICKERS:
        df = load_history(symbol, refresh=args.refresh)
        print(f"  {symbol:<9} {len(df):>6} days  {df.index[0].date()} → {df.index[-1].date()}")
    return 0


def cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    from trader.research.backtest import run_backtest
    from trader.research.evaluate import strategy_setup, symbol_costs
    from trader.research.metrics import compute_metrics

    cls, symbols, capital, limits = strategy_setup(args.strategy, settings)
    strat = cls(params=dict(cls.default_params), symbols=symbols)
    bars = _research_bars(args.strategy, settings)
    res = run_backtest(strat, bars, capital, limits, symbol_costs(symbols, settings))
    m = compute_metrics(res.equity.loc[strat.first_decision_date(bars):], res.trades, res.exposure)
    print(f"{args.strategy} {cls.title} · {', '.join(symbols)} · settings {strat.params}")
    print(f"{m.start} → {m.end}: yearly return {m.cagr:.1%} · Sharpe {m.sharpe:.2f} · worst drop "
          f"{m.max_drawdown:.1%} · {m.n_trades} trades · win rate {m.win_rate:.0%} · invested {m.exposure:.0%} of the time")
    print(f"Total trading costs paid: ${res.total_costs:,.0f}")
    return 0


def cmd_gate1(settings: Settings, args: argparse.Namespace) -> int:
    from trader.research.evaluate import evaluate, log_result
    from trader.research.report import write_report
    from trader.strategies import REGISTRY

    codes = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    for code in codes:
        result = evaluate(code, settings, _research_bars(code, settings, refresh=args.refresh))
        path = write_report(result)
        log_result(result, path)
        print(f"\n{code} {REGISTRY[code].title}: {'PASS' if result.passed else 'FAIL'}"
              f"  (out-of-sample Sharpe {result.oos_metrics.sharpe:.2f} vs benchmark {result.bench_oos.sharpe:.2f})")
        for c in result.checks:
            print(f"  {'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
        print(f"  report: {path.relative_to(settings.root)}")
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    import shutil
    import tempfile

    from trader.brokers.alpaca import AlpacaBroker
    from trader.brokers.dry_run import DryRunBroker
    from trader.data.live_bars import AlpacaDailyBars
    from trader.engine.live import Engine, EngineBusy
    from trader.notify.telegram import NullNotifier, from_env

    secrets = load_secrets()
    broker: Broker = AlpacaBroker(secrets.alpaca_api_key, secrets.alpaca_secret_key)
    notifier = from_env()
    if args.dry_run:
        tmp = Path(tempfile.mkdtemp(prefix="trader-dry-run-"))
        (tmp / "data").mkdir()
        if settings.journal_path.exists():
            shutil.copy(settings.journal_path, tmp / "data" / "journal.db")
        if settings.kill_switch_path.exists():
            shutil.copy(settings.kill_switch_path, tmp / settings.risk.guards.kill_switch_file)
        settings = settings.model_copy(update={"root": tmp})
        broker = DryRunBroker(broker)
        notifier = NullNotifier()
    journal = Journal(settings.journal_path)
    try:
        engine = Engine(settings, broker, journal, AlpacaDailyBars(secrets.alpaca_api_key, secrets.alpaca_secret_key),
                        notifier=notifier)
        try:
            report = engine.run_once()
        except EngineBusy as exc:
            print(f"{datetime.now():%Y-%m-%d %H:%M} skipped: {exc}")
            return 0
        stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
        print(f"{stamp} {'DRY RUN ' if args.dry_run else ''}{report.summary()}")
        for f in report.fills:
            print(f"  fill: {f.sleeve_id} {f.side.value} {f.qty:g} {f.symbol} @ {_money(f.price)}")
        for a in report.actions:
            print(f"  {a}")
        for p in report.problems:
            print(f"  PROBLEM: {p}")
        if args.dry_run:
            print("\nWould send (nothing was sent):" if broker.would_send else "\nWould send nothing.")  # type: ignore[attr-defined]
            for o in broker.would_send:  # type: ignore[attr-defined]
                size = f"qty {o.qty:g}" if o.qty else _money(o.notional or 0)
                stop = f" stop {o.stop_price:,.2f}" if o.stop_price else ""
                print(f"  {o.sleeve_id} {o.side.value} {o.symbol} {size} {o.order_type.value}{stop} ({o.reason})")
        return 1 if report.problems else 0
    finally:
        journal.close()


def cmd_news(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.data.news import AlpacaNews, recent, refresh

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else \
        sorted({s for sl in settings.sleeves.values() if sl.enabled for s in sl.symbols})
    secrets = load_secrets()
    journal = Journal(settings.journal_path)
    try:
        new = refresh(journal, AlpacaNews(secrets.alpaca_api_key, secrets.alpaca_secret_key), symbols, args.hours)
        items = recent(journal, symbols, args.hours)
    finally:
        journal.close()
    if args.json:
        print(json.dumps({"symbols": symbols, "hours": args.hours, "items": items}, indent=1))
        return 0
    print(f"{len(items)} headline(s) in the last {args.hours:g}h for {', '.join(symbols)} ({new} new)")
    for i in items[: args.limit]:
        print(f"  {i['created_at'][:16].replace('T', ' ')}  [{', '.join(i['symbols']) or '-'}] {i['headline']}")
    return 0


def cmd_fundamentals(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.data.fundamentals import load_fundamentals

    snaps = [load_fundamentals(s.strip().upper(), refresh=args.refresh) for s in args.symbols.split(",")]
    if args.json:
        print(json.dumps(snaps if len(snaps) > 1 else snaps[0], indent=1))
        return 0
    for f in snaps:
        pct = lambda v: "–" if v is None else f"{v:.1%}"  # noqa: E731
        num = lambda v, d=1: "–" if v is None else f"{v:,.{d}f}"  # noqa: E731
        print(f"{f['symbol']} {f.get('name') or ''} · {f.get('sector') or ''}")
        print(f"  P/E {num(f['pe_trailing'])} (forward {num(f['pe_forward'])}) · P/B {num(f['price_to_book'])} · "
              f"ROE {pct(f['return_on_equity'])} · net margin {pct(f['net_margin'])}")
        print(f"  revenue growth {pct(f['revenue_growth_yoy'])} · earnings growth {pct(f['earnings_growth_yoy'])} · "
              f"debt/equity {num(f['debt_to_equity_pct'], 0)}% · free cash flow "
              f"{'–' if f['free_cash_flow_usd'] is None else '$' + format(f['free_cash_flow_usd'] / 1e9, ',.1f') + 'B'}")
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    from trader.engine.serve import serve

    serve(settings, port=args.port)
    return 0


def cmd_telegram_setup(settings: Settings, _: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv

    from trader.notify.telegram import Telegram, find_chat, set_env_value

    env_path = settings.root / ".env"
    load_dotenv(env_path, override=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("No TELEGRAM_BOT_TOKEN in .env yet.\n"
              "1. In Telegram, message @BotFather, send /newbot, pick a name and a username ending in 'bot'.\n"
              "2. Copy the token it gives you into .env as TELEGRAM_BOT_TOKEN=... (not into chat).\n"
              "3. Open your new bot in Telegram, press Start (or send 'hi').\n"
              "4. Run this command again.")
        return 1
    try:
        username, chat = find_chat(token)
    except ValueError as exc:
        print(exc)
        return 1
    if chat is None:
        print(f"Token works (bot @{username}), but nobody has messaged it yet.\n"
              f"Open https://t.me/{username} in Telegram, press Start (or send 'hi'), then run this again.")
        return 1
    chat_id = str(chat["id"])
    set_env_value(env_path, "TELEGRAM_CHAT_ID", chat_id)
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "you")
    Telegram(token, chat_id).send(
        "✅ Personal Trader is connected.\n"
        "You'll get a message for every buy, sell and problem, plus a summary each morning at 7am.\n"
        "Commands: /status · /halt (stop new orders) · /resume")
    print(f"Connected bot @{username} to {who}'s chat. Saved TELEGRAM_CHAT_ID to .env and sent a test message.\n"
          "The engine uses it from its next run (within 15 minutes).")
    return 0


def cmd_halt(settings: Settings, args: argparse.Namespace) -> int:
    KillSwitch(settings.kill_switch_path).engage(args.reason)
    print(f"Kill switch ON. No new orders will be sent. Reason: {args.reason}")
    return 0


def cmd_resume(settings: Settings, _: argparse.Namespace) -> int:
    KillSwitch(settings.kill_switch_path).release()
    print("Kill switch OFF. Orders can be sent again (still subject to every risk check).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader", description="Personal paper-trading engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="account, positions, kill switch, recent orders")
    sub.add_parser("check-config", help="validate config/*.yaml")
    sub.add_parser("sync", help="pick up fills for open orders")
    p_order = sub.add_parser("order", help="send an order through the full pipeline")
    p_order.add_argument("--test", action="store_true", help="tiny smoke-test order in the TEST sleeve")
    p_order.add_argument("--symbol", default="BTC/USD", help="default BTC/USD (trades 24/7)")
    p_order.add_argument("--side", choices=["buy", "sell"], default="buy",
                         help="sell closes the TEST sleeve's whole position in the symbol")
    p_order.add_argument("--notional", type=float, default=10.0, help="USD amount for buys (default $10)")
    p_order.add_argument("--wait", type=float, default=15.0, help="seconds to wait for a fill")
    p_halt = sub.add_parser("halt", help="turn the kill switch on")
    p_halt.add_argument("--reason", default="manual halt")
    sub.add_parser("resume", help="turn the kill switch off")
    p_data = sub.add_parser("data", help="download/refresh daily price history for research")
    p_data.add_argument("--refresh", action="store_true", help="re-download even if the cache is fresh")
    p_bt = sub.add_parser("backtest", help="one backtest with default settings")
    p_bt.add_argument("strategy", choices=["S1", "S2", "S3", "S4"])
    p_g1 = sub.add_parser("gate1", help="full Gate 1 evaluation with HTML report")
    p_g1.add_argument("strategy", choices=["S1", "S2", "S3", "S4", "all"])
    p_g1.add_argument("--refresh", action="store_true", help="re-download price history first")
    p_news = sub.add_parser("news", help="latest headlines for enabled symbols (or --symbols)")
    p_news.add_argument("--symbols", help="comma-separated, e.g. SPY,BTC/USD")
    p_news.add_argument("--hours", type=float, default=48)
    p_news.add_argument("--limit", type=int, default=20)
    p_news.add_argument("--json", action="store_true")
    p_fund = sub.add_parser("fundamentals", help="company fundamentals snapshot")
    p_fund.add_argument("symbols", help="comma-separated, e.g. AAPL,MSFT")
    p_fund.add_argument("--refresh", action="store_true")
    p_fund.add_argument("--json", action="store_true")
    p_serve = sub.add_parser("serve", help="dashboard at http://localhost:8050")
    p_serve.add_argument("--port", type=int, default=8050)
    sub.add_parser("telegram-setup", help="connect the Telegram bot in .env and send a test message")
    p_run = sub.add_parser("run", help="one engine cycle")
    p_run.add_argument("--dry-run", action="store_true", help="show what would happen; send nothing")

    args = parser.parse_args(argv)
    handlers = {"status": cmd_status, "check-config": cmd_check_config, "sync": cmd_sync, "order": cmd_order,
                "halt": cmd_halt, "resume": cmd_resume, "data": cmd_data, "backtest": cmd_backtest,
                "gate1": cmd_gate1, "run": cmd_run, "news": cmd_news, "fundamentals": cmd_fundamentals,
                "serve": cmd_serve, "telegram-setup": cmd_telegram_setup}
    try:
        return handlers[args.command](load_settings(), args)
    except LiveTradingRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
