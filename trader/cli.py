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
  ctrader-setup   stores the cTrader Open API app credentials in .env (prompted, not echoed)
  ctrader-auth    approve the app against your cTrader DEMO account and store its tokens
  menubar         account P&L in the Mac menu bar (read-only, plus halt/resume)
  app             the dashboard in a native Mac window (same page, own Dock icon)
  excursions S1   MAE/MFE per trade: is the stop in the right place?

For the AI skills (Phase 4):
  context --json          accounts, sleeves, positions, recent trades, signals, learnings version
  candidates --json       entries waiting for an AI analyst-team verdict
  technicals SYMBOL       trend / momentum / volatility snapshot
  signal submit FILE      validate and record an AI signal (prints why if rejected)
  signal list             recent AI signals and their status
  notify [--file F] TEXT  send a Telegram message
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
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
        from trader.money import format_aud, stored_rate

        rate = stored_rate(journal)
        if rate and not rate.is_stale():
            print(f"       {format_aud(account.equity, rate)} at {1 / rate.aud_per_usd:.4f} AUD/USD")
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
    """Price history for research, which stops before the holdout seal. See `research/holdout.py`."""
    from trader.research.evaluate import benchmark_symbols, strategy_setup
    from trader.research.holdout import research_bars

    _, symbols, _, _ = strategy_setup(code, settings)
    return research_bars(sorted(set(symbols) | set(benchmark_symbols(code, symbols))), refresh=refresh)


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


def cmd_ledger(settings: Settings, args: argparse.Namespace) -> int:
    """How many distinct looks at the data each strategy has spent, and whether the record is intact."""
    import numpy as np

    from trader.research.holdout import HOLDOUT_START
    from trader.research.ledger import TRIAL_LEDGER, _rows, trial_count, trial_sharpes, verify_chain

    ok, why = verify_chain()
    if args.action == "verify" or not ok:
        print(f"trial ledger: {'intact' if ok else 'ALTERED -- ' + why}")
        if not ok:
            print("  Gate 1 will refuse to score until this is restored (try `git checkout "
                  "reports/trials.jsonl`).")
            return 1
        if args.action == "verify":
            return 0

    rows = _rows()
    strategies = sorted({r["strategy"] for r in rows})
    print(f"{len(rows)} recorded runs · holdout sealed from {HOLDOUT_START.date()}\n")
    print(f"  {'strategy':<14}{'looks':>7}{'sharpes':>9}{'spread':>9}")
    for code in strategies:
        sharpes = trial_sharpes(code)
        spread = f"{float(np.std(sharpes)):.3f}" if len(sharpes) > 1 else "–"
        print(f"  {code:<14}{trial_count(code):>7}{len(sharpes):>9}{spread:>9}")
    print(f"\n  {'total':<14}{sum(trial_count(c) for c in strategies):>7}")
    print(f"\n{TRIAL_LEDGER.relative_to(settings.root) if TRIAL_LEDGER.is_relative_to(settings.root) else TRIAL_LEDGER}")
    print("Every backtest records one. The spread of past trial Sharpes sets how high a result has to\n"
          "clear to beat what luck alone would have produced after that many attempts.")
    return 0


def cmd_holdout(settings: Settings, args: argparse.Namespace) -> int:
    """Spend a strategy's single read of the sealed holdout. Refuses, loudly, far more often than not."""
    import json

    from trader.research.backtest import run_backtest
    from trader.research.evaluate import benchmark_symbols, strategy_setup, symbol_costs
    from trader.research.holdout import (HOLDOUT_START, RECEIPTS_DIR, HoldoutAlreadyOpened,
                                         HoldoutRefused, holdout_bars, request_receipt)
    from trader.research.metrics import buy_and_hold, compute_metrics, risk_free_returns

    if args.list:
        receipts = sorted(RECEIPTS_DIR.glob("*.json")) if RECEIPTS_DIR.exists() else []
        if not receipts:
            print(f"No holdout has been opened. The seal is at {HOLDOUT_START.date()}.")
            return 0
        for path in receipts:
            r = json.loads(path.read_text())
            scored = f"Sharpe {r['result']['sharpe']:+.2f}" if r.get("result") else "read did not complete"
            print(f"  {r['strategy']:<6} {r['opened_at'][:10]}  {scored}  params {r['params']}"
                  + ("  [weak: measured against this period before the seal]"
                     if r["pre_existing_exposure"] else ""))
        return 0

    try:
        receipt = request_receipt(args.strategy, settings, confirmation=args.confirm,
                                  sleeve_id=args.sleeve)
    except (HoldoutRefused, HoldoutAlreadyOpened) as refused:
        print(f"Refused: {refused}")
        return 1

    cls, symbols, capital, limits = strategy_setup(args.strategy, settings)
    bench_syms = benchmark_symbols(args.strategy, symbols)
    bars = holdout_bars(sorted(set(symbols) | set(bench_syms)), receipt)   # spends it, before any read

    print(f"Holdout opened for {args.strategy}: {HOLDOUT_START.date()} onward. This read is now spent.")
    if receipt.pre_existing_exposure:
        print("  WEAK EVIDENCE: this strategy was measured against data from this period before the\n"
              "  seal existed, so the window was never truly unseen for it. Sealing does not unsee.")

    result = run_backtest(cls(params=receipt.params, symbols=symbols), bars, capital, limits,
                          symbol_costs(symbols, settings))
    equity = result.equity
    metrics = compute_metrics(equity, result.trades, result.exposure,
                              risk_free=risk_free_returns(equity.index))
    bench_costs = {s: c.total_bps + c.broker_fee_bps
                   for s, c in symbol_costs(bench_syms, settings).items()}
    bench = buy_and_hold(bars, bench_syms, equity.index, capital, bench_costs)
    bench_m = compute_metrics(bench, risk_free=risk_free_returns(bench.index))

    kept = metrics.sharpe / receipt.gate1_oos_sharpe if receipt.gate1_oos_sharpe else float("nan")
    passed = metrics.sharpe > bench_m.sharpe and metrics.sharpe > 0
    print(f"  settings       {receipt.params}")
    print(f"  {metrics.start} → {metrics.end}: Sharpe {metrics.sharpe:+.2f} · return "
          f"{metrics.total_return:+.1%} · worst drop {metrics.max_drawdown:.1%} · {metrics.n_trades} trades")
    print(f"  benchmark      Sharpe {bench_m.sharpe:+.2f} ({', '.join(bench_syms) or 'cash'})")
    print(f"  Gate 1 said    Sharpe {receipt.gate1_oos_sharpe:+.2f} on {receipt.gate1_ts[:10]} "
          f"→ the holdout kept {kept:.0%} of it")
    print(f"\n  HOLDOUT {'PASS' if passed else 'FAIL'} — this receipt is spent and cannot be reopened.")

    receipt.result = {"sharpe": round(metrics.sharpe, 3), "return": round(metrics.total_return, 4),
                      "max_drawdown": round(metrics.max_drawdown, 4), "n_trades": metrics.n_trades,
                      "benchmark_sharpe": round(bench_m.sharpe, 3), "passed": passed}
    receipt.save()
    return 0


def cmd_gate2(settings: Settings, args: argparse.Namespace) -> int:
    """Exit 0 even on FAIL. A non-zero exit makes a scheduled task look broken, and a sleeve failing
    its forward test is a result, not an error -- arguably the most useful result this prints."""
    from trader.journal.db import Journal
    from trader.research.gate2 import evaluate_gate2, log_gate2

    journal = Journal(settings.root / "data" / "journal.db")
    try:
        if args.sleeve == "all":
            live = {r["sleeve_id"] for r in journal.sleeve_equity_history()}
            sleeves = [s for s in settings.sleeves if s in live]
        else:
            sleeves = [args.sleeve]
        for sleeve_id in sleeves:
            if sleeve_id not in settings.sleeves:
                print(f"Unknown sleeve {sleeve_id}. Choose from {list(settings.sleeves)} or 'all'.")
                return 2
            result = evaluate_gate2(sleeve_id, settings, journal)
            rec = result.record
            print(f"\n{sleeve_id} {settings.sleeves[sleeve_id].name}: {result.headline}")
            if rec.metrics:
                print(f"  {rec.days_live} days live · Sharpe {rec.metrics.sharpe:+.2f} · "
                      f"return {rec.metrics.total_return:+.2%} · worst drop {rec.metrics.max_drawdown:.1%}")
            for c in result.checks:
                print(f"  {c.mark:<5} {c.name}: {c.detail}")
            if result.shape.n:
                sh = result.shape
                print(f"  shape: {sh.n} closed · win {sh.win_rate:.0%} · median hold {sh.median_hold_days:.1f}d "
                      f"· payoff {sh.payoff:.2f} (avg win ${sh.avg_win:,.0f} vs avg loss ${sh.avg_loss:,.0f})")
                if sh.payoff and sh.win_rate:
                    breakeven = 1 / (1 + sh.payoff)
                    print(f"  at that payoff it needs a {breakeven:.0%} win rate to break even, and gets {sh.win_rate:.0%}")
                print(f"  exits: {', '.join(f'{k} x{v}' for k, v in sh.exit_reasons.items())}")
            log_gate2(result)
    finally:
        journal.close()
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    import shutil
    import tempfile

    from trader.brokers.dry_run import DryRunBroker
    from trader.engine.brokers_setup import configured_brokers
    from trader.engine.live import Coordinator, Engine, EngineBusy
    from trader.notify.telegram import NullNotifier, from_env, fx_from_env

    notifier = from_env()
    if args.dry_run:
        tmp = Path(tempfile.mkdtemp(prefix="trader-dry-run-"))
        (tmp / "data").mkdir()
        if settings.journal_path.exists():
            shutil.copy(settings.journal_path, tmp / "data" / "journal.db")
        if settings.kill_switch_path.exists():
            shutil.copy(settings.kill_switch_path, tmp / settings.risk.guards.kill_switch_file)
        settings = settings.model_copy(update={"root": tmp})
        notifier = NullNotifier(reason="dry run")
    journal = Journal(settings.journal_path)
    try:
        brokers = configured_brokers(settings, journal)
        if args.dry_run:  # shadow sleeves send nothing anyway; wrap the real brokers so nothing is sent
            brokers = [(b if b.name == "shadow" else DryRunBroker(b), bars) for b, bars in brokers]
        engines = [Engine(settings, b, journal, bars, notifier=notifier) for b, bars in brokers]  # type: ignore[arg-type]
        try:
            # A dry run keeps the NullNotifier for forex too: `notifier` is already the discarding
            # one in that case, and reaching for the real forex chat here would send a ticket from a
            # run that is explicitly not meant to send anything.
            fx = notifier if args.dry_run else fx_from_env()
            reports = Coordinator(settings, journal, engines, notifier, fx_notifier=fx).run_once()
        except EngineBusy as exc:
            print(f"{datetime.now():%Y-%m-%d %H:%M} skipped: {exc}")
            return 0
        stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
        problems = 0
        if not args.dry_run:
            from trader.notify.heartbeat import ping

            all_problems = [p for r in reports for p in r.problems]
            ping(ok=not all_problems, message="\n".join(all_problems) or " · ".join(r.summary() for r in reports))
        for engine, report in zip(engines, reports):
            print(f"{stamp} {'DRY RUN ' if args.dry_run else ''}{engine.broker.name}: {report.summary()}")
            for f in report.fills:
                print(f"  fill: {f.sleeve_id} {f.side.value} {f.qty:g} {f.symbol} @ {_money(f.price)}")
            for a in report.actions:
                print(f"  {a}")
            for p in report.problems:
                print(f"  PROBLEM: {p}")
            problems += len(report.problems)
            if args.dry_run and hasattr(engine.broker, "would_send"):  # shadow never sends anything anyway
                sent = engine.broker.would_send  # type: ignore[attr-defined]
                print("  Would send (nothing was sent):" if sent else "  Would send nothing.")
                for o in sent:
                    size = f"qty {o.qty:g}" if o.qty else _money(o.notional or 0)
                    stop = f" stop {o.stop_price:,.5g}" if o.stop_price else ""
                    print(f"    {o.sleeve_id} {o.side.value} {o.symbol} {size} {o.order_type.value}{stop} ({o.reason})")
        return 1 if problems else 0
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


def cmd_excursions(settings: Settings, args: argparse.Namespace) -> int:
    """How far trades went against you, and in your favour, before closing.

    The P&L alone cannot tell you whether a stop is in the right place. MAE on WINNERS can: if winners
    routinely take 0.9R of heat, a 1R stop is about to start cutting them off; if they never go past
    0.5R, it is not the stop costing you money.
    """
    import numpy as np

    from trader.data.history import load_many
    from trader.research.backtest import run_backtest
    from trader.research.evaluate import strategy_setup, symbol_costs
    from trader.strategies import REGISTRY

    codes = [args.strategy] if args.strategy and args.strategy != "all" else list(REGISTRY)
    for code in codes:
        if code not in REGISTRY:
            print(f"Unknown strategy {code}. Choose from {list(REGISTRY)} or 'all'.")
            return 2
        cls, symbols, capital, limits = strategy_setup(code, settings)
        sleeve = settings.sleeves.get(code)
        params = dict(sleeve.params) if sleeve and sleeve.params else dict(cls.default_params)
        result = run_backtest(cls(params=params, symbols=symbols), load_many(symbols), capital, limits,
                              symbol_costs(symbols, settings))
        trades = [t for t in result.trades if t.mae_r is not None and t.r_multiple is not None]
        if not trades:
            print(f"{code}: no completed trades with a recorded stop.")
            continue
        won = [t for t in trades if t.pnl > 0]
        lost = [t for t in trades if t.pnl <= 0]
        pct = lambda xs, p: float(np.percentile(xs, p)) if xs else float("nan")  # noqa: E731
        print(f"\n{code} {cls.title}: {len(trades)} trades, {len(won)} winners "
              f"({len(won) / len(trades):.0%}), params {params}")
        print(f"  {'':14}{'median':>9}{'75th':>9}{'90th':>9}{'worst':>9}")
        for label, values in (("MAE winners", [t.mae_r for t in won]), ("MAE losers", [t.mae_r for t in lost]),
                              ("MFE winners", [t.mfe_r for t in won]), ("MFE losers", [t.mfe_r for t in lost])):
            if values:
                print(f"  {label:14}{pct(values, 50):>9.2f}{pct(values, 75):>9.2f}"
                      f"{pct(values, 90):>9.2f}{max(values):>9.2f}")
        threatened = [t for t in won if t.mae_r >= 0.9]
        gave_back = [t for t in lost if t.mfe_r > 1.0]
        print(f"  winners that came within 0.1R of the stop: {len(threatened)} "
              f"({len(threatened) / max(len(won), 1):.0%})  -- a stop that is too tight shows up here")
        print(f"  losers that were once up more than 1R:     {len(gave_back)} "
              f"({len(gave_back) / max(len(lost), 1):.0%})  -- open profit handed back")
    return 0


def cmd_app(settings: Settings, _args: argparse.Namespace) -> int:
    """The dashboard in a real Mac window. Starts the server first if nothing is listening."""
    from trader.app import run

    return run(settings.root)


def cmd_menubar(_settings: Settings, _args: argparse.Namespace) -> int:
    """Live P&L in the menu bar. Reads the journal; never places an order."""
    from trader.menubar import run

    return run()


def cmd_ctrader_setup(settings: Settings, _: argparse.Namespace) -> int:
    """Store the cTrader Open API app credentials in .env.

    Prompted rather than passed as arguments, so the secret never lands in shell history. The secret is
    typed blind and never printed back.
    """
    import getpass
    import os
    import stat

    from dotenv import load_dotenv

    from trader.notify.telegram import set_env_value

    env_path = settings.root / ".env"
    load_dotenv(env_path, override=True)
    print("cTrader Open API credentials (from openapi.ctrader.com -> Applications -> your app).\n"
          "These identify the APP, not your trading account. Nothing is printed back.\n")

    existing = os.environ.get("CTRADER_CLIENT_ID", "").strip()
    prompt = f"Client ID [{existing}]: " if existing else "Client ID: "
    client_id = input(prompt).strip() or existing
    if not client_id:
        print("No Client ID given; nothing written.")
        return 1
    secret = getpass.getpass("Client Secret (typing is hidden): ").strip()
    if not secret:
        print("No Client Secret given; nothing written.")
        return 1
    if secret == client_id:
        print("The Client Secret and Client ID are identical, which means one was pasted twice.")
        return 1

    set_env_value(env_path, "CTRADER_CLIENT_ID", client_id)
    set_env_value(env_path, "CTRADER_CLIENT_SECRET", secret)
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
    print(f"\nSaved to {env_path} (permissions set to owner-only).")
    print(f"  CTRADER_CLIENT_ID     = {client_id}")
    print(f"  CTRADER_CLIENT_SECRET = {'*' * 8} ({len(secret)} characters)")
    print("\nNext: `uv run trader ctrader-auth` to grant the app access to your DEMO account.")
    return 0


def _wait_for_oauth_code(timeout_s: float = 300) -> str:
    """Serve http://localhost:8765 until cTrader redirects back with ?code=..., then stop.

    Bound to 127.0.0.1 so nothing off this machine can reach it, and it serves exactly one request.
    """
    import http.server
    from urllib.parse import parse_qs, urlparse

    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (stdlib naming)
            params = parse_qs(urlparse(self.path).query)
            captured["code"] = (params.get("code") or [""])[0]
            captured["error"] = (params.get("error") or params.get("errorCode") or [""])[0]
            ok = bool(captured["code"])
            body = ("<h2>Connected.</h2><p>You can close this tab and go back to the terminal.</p>" if ok
                    else "<h2>No authorization code came back.</h2><p>Check the terminal.</p>")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif;padding:3em'>{body}</body></html>"
                             .encode())

        def log_message(self, *_args) -> None:
            return  # the request URL holds the code; keep it out of the terminal

    server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    server.timeout = timeout_s
    try:
        server.handle_request()
    finally:
        server.server_close()
    if captured.get("error"):
        raise RuntimeError(f"cTrader refused the authorization: {captured['error']}")
    if not captured.get("code"):
        raise RuntimeError("Timed out waiting for cTrader to redirect back to localhost:8765.")
    return captured["code"]


def cmd_ctrader_auth(settings: Settings, _: argparse.Namespace) -> int:
    """Grant the app access to a cTrader account and store the resulting tokens."""
    import os
    import webbrowser

    from dotenv import load_dotenv

    from trader.brokers.ctrader_auth import (
        REDIRECT_URI,
        CTraderAuthError,
        authorize_url,
        exchange_code,
        save_tokens,
    )

    load_dotenv(settings.root / ".env", override=True)
    client_id = os.environ.get("CTRADER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("No cTrader app credentials in .env. Run `trader ctrader-setup` first.")
        return 1

    url = authorize_url(client_id)
    print("A browser window will open so you can approve this app.\n"
          "IMPORTANT: grant access to your DEMO account only. If a live account is listed, leave it off.\n"
          f"\nIf the browser doesn't open, paste this in yourself:\n{url}\n"
          f"\nWaiting for cTrader to redirect back to {REDIRECT_URI} ...")
    webbrowser.open(url)
    try:
        code = _wait_for_oauth_code()
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1

    # The authorization code is only valid for about a minute, so exchange it straight away.
    try:
        tokens = exchange_code(code, client_id, client_secret)
    except CTraderAuthError as exc:
        print(f"\nCould not exchange the code for tokens: {exc}")
        return 1

    path = settings.root / "data" / "ctrader_tokens.json"
    save_tokens(path, tokens)
    print(f"\nConnected. Tokens saved to {path} (owner-only).")
    print(f"  access token expires {tokens.expires_at:%Y-%m-%d %H:%M UTC} (refreshed automatically before then)")
    return 0


def cmd_telegram_setup(settings: Settings, args: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv

    from trader.notify.telegram import Telegram, find_chat, find_group_chat, set_env_value

    env_path = settings.root / ".env"
    load_dotenv(env_path, override=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if getattr(args, "fx", False):
        journal = Journal(settings.journal_path)
        try:
            return _telegram_setup_fx(env_path, token, find_group_chat, set_env_value, Telegram, journal)
        finally:
            journal.close()
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


def _telegram_setup_fx(env_path, token: str, find_group_chat, set_env_value, Telegram, journal) -> int:
    """Give the forex tickets their own Telegram conversation.

    Worth a command rather than a line in a README: the forex messages are the only ones acted on by
    hand, and they arrived in the same chat as every paper fill, stop and warning the engine sends. The
    one message that needed doing was one of forty that did not.

    The group has to be made in Telegram by the user -- a bot cannot create one and cannot add itself.
    What this does is the part that is easy to get wrong: find the right chat id and write it to .env
    without it being typed out or pasted anywhere.
    """
    import json

    from trader.notify.telegram import LAST_GROUP_KEY

    if not token:
        print("No TELEGRAM_BOT_TOKEN in .env yet. Run `trader telegram-setup` first.")
        return 1
    try:
        username, chat = find_group_chat(token)
    except ValueError as exc:
        print(exc)
        return 1
    if chat is None:
        # The engine polls getUpdates every fifteen minutes and that CONFIRMS the queue, after which
        # Telegram deletes it. So a group added more than one engine run ago is invisible to the live
        # look above, however correctly it was set up. `poll_commands` writes down any group it is
        # shown precisely so this can still be answered. See `Telegram._remember_group`.
        remembered = journal.kv_get(LAST_GROUP_KEY)
        if remembered:
            chat = json.loads(remembered)
            print(f"Telegram had already dropped the update, but the engine had noted the group.")
    if chat is None:
        # The bot name is printed BARE, with no @ in front of it. That is what goes in Telegram's
        # search box, and a terminal that renders a leading @ as the full-width U+FF20 -- which this
        # one does -- produces a string that matches nothing when pasted. A copyable name is the whole
        # job of this message.
        print("I can't see a group yet. On your phone, in Telegram:\n"
              "  1. Tap the pencil (new message) -> New Group\n"
              f"  2. In the search box type:  {username}\n"
              "     (no @ in front) and tap the bot to select it, then Next\n"
              "  3. Name it 'Forex tickets' and tap Create\n"
              f"  4. In the new group, send:  /start@{username}\n"
              "\nThen run this command again. Step 4 matters: a plain group message does not reach a "
              "bot whose privacy mode is on, which is the default, but a command addressed to it "
              "always does.")
        return 1
    chat_id = str(chat["id"])
    set_env_value(env_path, "TELEGRAM_FX_CHAT_ID", chat_id)
    title = chat.get("title") or chat_id
    Telegram(token, chat_id).send(
        "📍 Forex tickets will arrive here from now on.\n"
        "Only EUR/USD plans you place by hand in MetaTrader 4 -- a ticket at the London open, a note "
        "when the day is done, and a warning if the watcher stops running. Everything else the system "
        "does stays in the other chat.\n"
        "Nothing here is placed for you, and nothing here is advice.")
    print(f"Forex messages now go to '{title}' ({chat_id}).\n"
          f"Saved TELEGRAM_FX_CHAT_ID to .env and sent a test message there.\n"
          f"In use from the next pass -- `trader watch-fx` runs every minute.")
    return 0


def _read_only_engines(settings: Settings, journal: Journal):
    from trader.engine.brokers_setup import configured_brokers
    from trader.engine.live import Engine

    return [Engine(settings, b, journal, bars) for b, bars in configured_brokers(settings, journal)]  # type: ignore[arg-type]


def cmd_context(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.engine.dashboard import collect
    from trader.engine.intraday_watch import IntradayWatcher
    from trader.signals import store as signals

    journal = Journal(settings.journal_path)
    try:
        d = collect(settings, journal, _read_only_engines(settings, journal))
        now = d["now"]
        trips = []
        for t in d["trips"]:
            closed_recent = t.exit_ts and (now - datetime.fromisoformat(t.exit_ts)).days <= args.days
            if t.is_open or closed_recent:
                px = d["price"](t.symbol) if t.is_open else None
                r = t.r_multiple(px)
                trips.append({"trade_id": t.trade_id, "sleeve": t.sleeve_id, "symbol": t.symbol,
                              "status": "open" if t.is_open else "closed", "entry_time": t.entry_ts,
                              "entry_price": round(t.entry_price, 6), "exit_time": t.exit_ts,
                              "exit_price": t.exit_price, "qty": t.buy_qty, "initial_stop": t.initial_stop,
                              "pnl_usd": round(t.pnl(px), 2), "r_multiple": None if r is None else round(r, 2),
                              "reason_in": t.entry_reason, "reason_out": t.exit_reason})
        out = {
            "now": now.isoformat(timespec="seconds"), "learnings_version": signals.learnings_version(),
            "kill_switch": d["kill_switch"], "last_engine_run": d["last_run"],
            "accounts": [{"broker": a["broker"], "equity": round(a["account"].equity, 2),
                          "day_change_pct": round(a["day_pct"], 5), "drawdown_from_peak": round(a["drawdown"], 5)}
                         for a in d["accounts"]],
            "total_equity": round(d["total"], 2), "total_pnl_since_start": round(d["total_pnl"], 2),
            "sleeves": [{k: (round(v, 5) if isinstance(v, float) else v) for k, v in s.items()} for s in d["sleeves"]],
            "strategy_kpis": d["kpis"], "trades_open_or_closed_last_days": trips,
            "open_orders": [dict(r) for r in journal.open_orders()],
            "ai_signals_recent": signals.recent_signals(journal, 10),
            "ai_requests_pending": signals.pending_requests(journal),
            "warnings_recent": [dict(w) for w in d["warnings"]],
            "disabled_sleeves": [{"id": s.id, "name": s.name, "ai": s.ai} for s in settings.sleeves.values()
                                 if not s.enabled and s.id != "TEST"],
            "fx_reports_recent": IntradayWatcher(settings, journal, notifier=None, clock=lambda: now)
                .report_summary(now.date() - timedelta(days=args.days), now.date())
                if settings.intraday.enabled and settings.intraday.symbols else None,
        }
    finally:
        journal.close()
    print(json.dumps(out, indent=1, default=str))
    return 0


def cmd_candidates(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.signals import store as signals

    journal = Journal(settings.journal_path)
    try:
        pending = signals.pending_requests(journal, args.sleeve)
    finally:
        journal.close()
    if args.json:
        print(json.dumps({"learnings_version": signals.learnings_version(), "candidates": pending}, indent=1, default=str))
    else:
        print(f"{len(pending)} candidate(s) waiting for an AI verdict")
        for p in pending:
            print(f"  {p['sleeve_id']} {p['symbol']} (bar {p['bar_date']}): {p['context'].get('rule_reason')}")
    return 0


def cmd_technicals(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.research.technicals import snapshot

    snaps = [snapshot(s.strip()) for s in args.symbols.split(",")]
    print(json.dumps(snaps if len(snaps) > 1 else snaps[0], indent=1))
    return 0


def cmd_signal(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from trader.signals import store as signals

    journal = Journal(settings.journal_path)
    try:
        if args.action == "list":
            for s in signals.recent_signals(journal, 20):
                print(f"  {s['received_at'][:16]} {s['sleeve_id']:<4} {s['kind']:<15} bar {s['bar_date']} "
                      f"{s['status']:<9} {s['reason'] or ''}")
            return 0
        if not args.file:
            print("usage: trader signal submit FILE")
            return 2
        raw = json.loads(Path(args.file).read_text())
        if args.action == "validate":
            try:
                sig = signals.validate(raw, settings, journal)
            except signals.SignalRejected as exc:
                print(f"INVALID: {exc}")
                return 1
            print(f"VALID {sig.kind} for {sig.sleeve} (not recorded; use `signal submit` to record it)")
            return 0
        try:
            sig = signals.submit(raw, settings, journal)
        except signals.SignalRejected as exc:
            print(f"REJECTED: {exc}")
            return 1
        n = len(sig.verdicts) or len(sig.weights)
        print(f"ACCEPTED {sig.kind} {sig.id} for {sig.sleeve} ({n} item(s)). The engine acts on it within 15 minutes.")
        return 0
    finally:
        journal.close()


def cmd_notify(settings: Settings, args: argparse.Namespace) -> int:
    from trader.notify.telegram import NullNotifier, from_env

    load_secrets()  # loads .env (Telegram settings) into the environment
    text = Path(args.file).read_text() if args.file else " ".join(args.text)
    notifier = from_env()
    if isinstance(notifier, NullNotifier):
        print("Telegram is not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env).")
        return 1
    for i in range(0, len(text), 3800):  # Telegram's message size limit
        notifier.send(text[i:i + 3800])
    if args.digest:  # the AI digest was sent, so the engine's plain 7am summary is skipped today
        from zoneinfo import ZoneInfo

        journal = Journal(settings.journal_path)
        try:
            journal.kv_set("last_digest", str(datetime.now(ZoneInfo("Australia/Sydney")).date()))
        finally:
            journal.close()
    print("sent")
    return 0


def cmd_halt(settings: Settings, args: argparse.Namespace) -> int:
    KillSwitch(settings.kill_switch_path).engage(args.reason)
    print(f"Kill switch ON. No new orders will be sent. Reason: {args.reason}")
    return 0


def cmd_resume(settings: Settings, _: argparse.Namespace) -> int:
    KillSwitch(settings.kill_switch_path).release()
    print("Kill switch OFF. Orders can be sent again (still subject to every risk check).")
    return 0


def cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    """A verified copy of the journal, which is the only record of what this system has done.

    Until 2026-11-12 there was no backup of any kind: Time Machine was off, `data/` is gitignored, and
    the newest hand-made copy was 34 days old and sat in the same folder as the original. Gate 2 needs
    90 days of history that existed in exactly one file on one disk.

    Runs through the CLI rather than as a shell script because a launchd agent cannot execute a file
    inside ~/Documents -- macOS refuses with "Operation not permitted". Every other agent here works by
    invoking `uv` from outside that folder, so this uses the same proven path instead of a second one.
    """
    import gzip
    import shutil
    import sqlite3

    db = settings.journal_path
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    # Outside data/ on purpose: a backup in the folder it protects is lost to the same rm or disk.
    dest = Path(args.dest) if args.dest else (
        Path.home() / "Library" / "Application Support" / "PersonalTrader" / "backups")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"journal-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"

    # sqlite3's own backup API, not a file copy: the engine writes every 15 minutes and the forex
    # watcher every minute, so a plain copy can catch the file mid-transaction and produce something
    # that opens cleanly and is quietly wrong.
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with sqlite3.connect(out) as copy:
            src.backup(copy)
    finally:
        src.close()

    checked = sqlite3.connect(out)
    try:
        verdict = checked.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        checked.close()
    if verdict != "ok":
        # Kept, not deleted: a corrupt copy is evidence about the original.
        print(f"backup failed integrity_check ({verdict}); kept for inspection: {out}", file=sys.stderr)
        return 1

    with out.open("rb") as raw, gzip.open(f"{out}.gz", "wb") as gz:
        shutil.copyfileobj(raw, gz)
    out.unlink()

    # Pruned only after a verified good backup exists, so a run of failures cannot erode the history.
    keep = args.keep
    old = sorted(dest.glob("journal-*.db.gz"), key=lambda f: f.stat().st_mtime, reverse=True)[keep:]
    for f in old:
        f.unlink()
    size = (dest / f"{out.name}.gz").stat().st_size / 1e6
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} backup ok: {out}.gz "
          f"({size:.1f} MB), pruned {len(old)}, keeping {keep}")
    return 0


def cmd_watch_fx(settings, args) -> int:
    """One pass of the forex watcher and nothing else. Meant to run every minute.

    The engine's own fifteen-minute pass already calls this watcher, and fifteen minutes is too coarse
    for it: measured on 250 days of real bars, checking every 15 minutes rather than every 5 missed 18
    setups instead of 12 and earned $1,160 instead of $1,638. The engine itself cannot simply run more
    often -- it reconciles the broker, re-prices forty markets, rebuilds the dashboard and counts
    against the daily order budget. So the one part that needs to be quick runs on its own.

    It touches no broker and places nothing. The heaviest thing it does is ask Yahoo for one rate, and
    that is cached for 60 seconds, which is why a minute is the natural beat.

    A resting limit order at the level still beats every polling frequency -- it fills AT the price and
    misses nothing, which is +$1,779 against +$1,638 -- so this improves the push message rather than
    replacing the advice in the plan to leave an order sitting there.
    """
    from trader.engine.intraday_watch import IntradayWatcher
    from trader.notify.telegram import fx_from_env
    from trader.risk.kill_switch import KillSwitch

    if not settings.intraday.enabled:
        print("intraday watcher disabled in config/intraday.yaml")
        return 0
    # Kill-switch aware for the same reason the engine's pass is: a halted system that keeps texting
    # you trades to place by hand has not really halted.
    if KillSwitch(settings.kill_switch_path).active:
        return 0
    journal = Journal(settings.journal_path)
    try:
        notifier = fx_from_env()
        watcher = IntradayWatcher(settings, journal, notifier)
        # First, because it is the only thing that reports on the passes that did NOT happen, and it
        # must run whether or not the rest of this pass works.
        did = watcher.report_downtime()
        did += watcher.sweep_unfinished() + watcher.run_once() + watcher.close_out()
        for what in did:
            journal.log(what, level="info")
            print(f"{datetime.now():%Y-%m-%d %H:%M} {what}")
        return _fx_delivery_check(journal, notifier)
    finally:
        journal.close()


def _fx_delivery_check(journal, notifier) -> int:
    """Did this pass's messages reach the phone? Non-zero exit if not.

    The engine has `Coordinator._check_alerts_got_through` for exactly this, and this command had
    nothing: it built a notifier, sent into it and exited 0 regardless. Three ways that went wrong, all
    silent. A `NullNotifier` from missing credentials appended every message to a list and dropped it --
    which is what happened for weeks, see `telegram.from_env`. A send that timed out swallowed the
    exception. And launchd, which runs this every minute, had no way to know either had happened,
    because the exit code said fine.

    So the state goes where it can be read -- `alert_channel`, the same key the engine writes and the
    dashboard's health card shows -- and the exit code tells launchd. There is nothing to send the bad
    news over, which is the whole problem with a broken notifier, so it has to be recorded instead.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if getattr(notifier, "misconfigured", False):
        discarded = len(getattr(notifier, "sent", []) or [])
        reason = getattr(notifier, "reason", "Telegram is not set up")
        journal.kv_set("alert_channel", f"DISCARDING {reason}")
        journal.log(f"watch-fx: alerts going nowhere, {discarded} message(s) discarded: {reason}",
                    level="error")
        print(f"alerts going nowhere: {reason}")
        return 1
    failures = getattr(notifier, "failures", 0)
    if failures:
        error = getattr(notifier, "last_error", None) or "unknown"
        journal.kv_set("alert_channel", f"FAILING {error}")
        journal.log(f"watch-fx: {failures} message(s) did not reach Telegram: {error}", level="error")
        print(f"{failures} message(s) did not reach Telegram: {error}")
        return 1
    journal.kv_set("alert_channel", f"{type(notifier).__name__} ok {now}")
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
    p_bt.add_argument("strategy", choices=["S1", "S2", "S3", "S4", "S8"])
    p_g1 = sub.add_parser("gate1", help="full Gate 1 evaluation with HTML report")
    p_g1.add_argument("strategy", choices=["S1", "S2", "S3", "S4", "S8", "all"])
    p_g1.add_argument("--refresh", action="store_true", help="re-download price history first")
    p_g2 = sub.add_parser("gate2", help="forward test: is the live sleeve reproducing its backtest?")
    p_g2.add_argument("sleeve", help="a sleeve id, or 'all' for every sleeve with a live record")
    p_led = sub.add_parser("ledger", help="the trial ledger: how hard have we looked, and is it intact?")
    p_led.add_argument("action", nargs="?", default="stats", choices=["stats", "verify"])
    p_ho = sub.add_parser("holdout", help="spend a strategy's ONE read of the sealed holdout")
    p_ho.add_argument("strategy", help="strategy code, e.g. S8")
    p_ho.add_argument("--confirm", default="", help='exactly: "open holdout for S8"')
    p_ho.add_argument("--sleeve", default=None, help="sleeve id, when it differs from the strategy code")
    p_ho.add_argument("--list", action="store_true", help="show every receipt instead of opening one")
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
    p_tg = sub.add_parser("telegram-setup",
                          help="connect the Telegram bot in .env and send a test message")
    p_tg.add_argument("--fx", action="store_true",
                      help="give the forex tickets their own group chat, separate from engine alerts")
    sub.add_parser("ctrader-setup", help="store the cTrader Open API app credentials in .env")
    sub.add_parser("ctrader-auth", help="grant the app access to your cTrader demo account")
    sub.add_parser("menubar", help="account P&L in the Mac menu bar")
    sub.add_parser("app", help="the dashboard in a native Mac window")
    p_exc = sub.add_parser("excursions", help="MAE/MFE: how far trades ran against you and for you")
    p_exc.add_argument("strategy", nargs="?", default="all", help="strategy code, or all")
    p_ctx = sub.add_parser("context", help="state for the AI skills (JSON)")
    p_ctx.add_argument("--json", action="store_true", help="(always JSON)")
    p_ctx.add_argument("--days", type=int, default=7, help="include trades closed in the last N days")
    p_cand = sub.add_parser("candidates", help="entries waiting for an AI verdict")
    p_cand.add_argument("--sleeve")
    p_cand.add_argument("--json", action="store_true")
    p_tech = sub.add_parser("technicals", help="technical snapshot (JSON)")
    p_tech.add_argument("symbols", help="comma-separated")
    p_sig = sub.add_parser("signal", help="submit or list AI signals")
    p_sig.add_argument("action", choices=["submit", "validate", "list"])
    p_sig.add_argument("file", nargs="?")
    p_note = sub.add_parser("notify", help="send a Telegram message")
    p_note.add_argument("--file")
    p_note.add_argument("--digest", action="store_true", help="this is today's digest (engine skips its own)")
    p_note.add_argument("text", nargs="*")
    sub.add_parser("watch-fx", help="one pass of the forex watcher (meant to run every minute)")
    p_bk = sub.add_parser("backup", help="verified copy of the journal, gzipped, outside data/")
    p_bk.add_argument("--dest", help="where to write (default: ~/Library/Application Support/PersonalTrader/backups)")
    p_bk.add_argument("--keep", type=int, default=30, help="how many to retain (default 30)")
    p_run = sub.add_parser("run", help="one engine cycle")
    p_run.add_argument("--dry-run", action="store_true", help="show what would happen; send nothing")

    args = parser.parse_args(argv)
    handlers = {"status": cmd_status, "check-config": cmd_check_config, "sync": cmd_sync, "order": cmd_order,
                "halt": cmd_halt, "resume": cmd_resume, "data": cmd_data, "backtest": cmd_backtest,
                "gate1": cmd_gate1, "gate2": cmd_gate2, "holdout": cmd_holdout, "ledger": cmd_ledger, "run": cmd_run, "news": cmd_news, "fundamentals": cmd_fundamentals,
                "serve": cmd_serve, "telegram-setup": cmd_telegram_setup, "context": cmd_context,
                "ctrader-setup": cmd_ctrader_setup, "ctrader-auth": cmd_ctrader_auth,
                "menubar": cmd_menubar, "excursions": cmd_excursions, "app": cmd_app,
                "candidates": cmd_candidates, "technicals": cmd_technicals, "signal": cmd_signal, "notify": cmd_notify,
                "watch-fx": cmd_watch_fx, "backup": cmd_backup}
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
