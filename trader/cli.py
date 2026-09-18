"""Command line: `uv run trader <command>`.

  status          account, positions, kill switch, recent orders
  order --test    send a tiny order through risk gate -> Alpaca paper -> journal
  sync            pick up fills for orders that were still open
  halt / resume   turn the kill switch on / off
  check-config    validate the files in config/
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

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

    args = parser.parse_args(argv)
    handlers = {"status": cmd_status, "check-config": cmd_check_config, "sync": cmd_sync, "order": cmd_order,
                "halt": cmd_halt, "resume": cmd_resume}
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
