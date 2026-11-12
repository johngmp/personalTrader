from datetime import datetime, timezone

import pytest

from trader.config import CostModel
from trader.journal.db import trading_day_start
from trader.models import BrokerOrder, OrderRequest, OrderStatus, Side

NO_COST = CostModel(half_spread_bps=0, slippage_bps=0, fee_bps=0)


def _order(journal) -> OrderRequest:
    o = OrderRequest(sleeve_id="S1", symbol="SPY", side=Side.BUY, qty=10, stop_price=490)
    journal.record_order(o, "us_equity")
    return o


def _bo(o, status, filled_qty, avg) -> BrokerOrder:
    return BrokerOrder(broker_order_id="b1", client_order_id=o.client_order_id, symbol="SPY", side=Side.BUY,
                       status=status, qty=10, filled_qty=filled_qty, filled_avg_price=avg)


def test_partial_fills_are_recorded_as_separate_slices(journal):
    o = _order(journal)
    f1 = journal.record_fill_progress(_bo(o, OrderStatus.PARTIALLY_FILLED, 4, 100.0), "S1", NO_COST)
    f2 = journal.record_fill_progress(_bo(o, OrderStatus.FILLED, 10, 101.2), "S1", NO_COST)
    assert (f1.qty, f1.price) == (4, 100.0)
    assert f2.qty == pytest.approx(6)
    assert f2.price == pytest.approx(102.0)  # (10*101.2 - 4*100) / 6
    assert journal.sleeve_holdings("S1") == {"SPY": pytest.approx(10)}


def test_repeated_updates_do_not_double_count(journal):
    o = _order(journal)
    bo = _bo(o, OrderStatus.FILLED, 10, 100.0)
    assert journal.record_fill_progress(bo, "S1", NO_COST) is not None
    assert journal.record_fill_progress(bo, "S1", NO_COST) is None
    assert journal.sleeve_holdings("S1") == {"SPY": pytest.approx(10)}


def test_peak_equity_tracks_the_highest_snapshot(journal):
    for eq in (100_000, 104_000, 98_000):
        journal.snapshot_equity("alpaca", eq, eq)
    assert journal.peak_equity("alpaca") == 104_000
    assert journal.peak_equity("other") == 0.0


def test_trading_day_starts_at_new_york_midnight():
    # 03:00 UTC on the 18th is still the 17th in New York.
    start = trading_day_start(datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc))
    assert (start.year, start.month, start.day, start.hour) == (2026, 9, 17, 0)


def test_broker_rounding_leftovers_count_as_flat(journal):
    """Regression: the first live round trip left 3.3e-10 BTC in the journal after Alpaca rounded to 9 decimals."""
    buy = OrderRequest(sleeve_id="TEST", symbol="BTC/USD", side=Side.BUY, notional=10, stop_price=70_000)
    sell = OrderRequest(sleeve_id="TEST", symbol="BTC/USD", side=Side.SELL, qty=0.000127149)
    fee = CostModel(half_spread_bps=5, slippage_bps=10, fee_bps=0, broker_fee_bps=25)
    for o, qty in ((buy, 0.000127468), (sell, 0.000127149)):
        journal.record_order(o, "crypto")
        journal.record_fill_progress(
            BrokerOrder(broker_order_id=o.client_order_id, client_order_id=o.client_order_id, symbol="BTC/USD",
                        side=o.side, status=OrderStatus.FILLED, filled_qty=qty, filled_avg_price=76_900.0),
            "TEST", fee)
    assert journal.sleeve_holdings("TEST") == {}
    assert journal.all_holdings() == {}


# --- the journal is the only record there is --------------------------------------------------------

def test_a_corrupt_journal_is_refused_rather_than_half_read(tmp_path):
    """SQLite will happily serve rows from the parts of a damaged file that still read.

    So a corrupt journal does not announce itself: the engine would reconcile holdings against a
    partial picture, place orders on it, and write more rows into the damage. This file is the only
    record of every holding, stop, fill and the whole P&L history, and until 2026-11-12 there was no
    backup of it at all.
    """
    import sqlite3

    import pytest

    from trader.journal.db import Journal

    from trader.models import OrderRequest, Side

    good = tmp_path / "j.db"
    j = Journal(good)
    # Enough rows to span several pages: an empty database is a single page, and scribbling past the
    # end of it just extends the file instead of damaging anything.
    for i in range(400):
        j.record_order(OrderRequest(sleeve_id="S1", symbol=f"X{i}", side=Side.BUY, qty=1.0,
                                    reason="filler"), "us_equity")
    j.close()
    size = good.stat().st_size
    assert size > 40_000, f"test journal is only {size} bytes; corruption would not land on a page"
    with good.open("r+b") as f:                 # in the middle, past the header, as real damage is
        f.seek(size // 2)
        f.write(b"\xde\xad\xbe\xef" * 2000)
    Journal._integrity_cache.pop(str(good), None)

    with pytest.raises(RuntimeError, match="integrity check|unreadable"):
        Journal(good)
    try:
        Journal(good)
    except RuntimeError as exc:
        assert "backups" in str(exc), "the refusal must say where the backup is"


def test_a_healthy_journal_opens_and_the_check_is_not_paid_twice(tmp_path):
    """51 ms on the 9.5 MB production journal is cheap against a 60-second cycle, but the same path is
    opened several times per run (engine, dashboard, vault), and paying it each time is waste."""
    from trader.journal.db import Journal

    path = tmp_path / "j.db"
    Journal(path).close()
    assert Journal._integrity_cache.get(str(path)) == "ok"
    Journal(path).close()                        # must not raise, must not re-check


def test_the_backup_command_produces_something_that_restores(tmp_path):
    """An untested backup is not a backup. This asserts the copy opens, passes its own integrity
    check, and still contains the trading record -- not merely that a file appeared."""
    import argparse
    import gzip
    import shutil
    import sqlite3

    from trader.cli import cmd_backup
    from trader.config import PROJECT_ROOT, load_settings
    from trader.journal.db import Journal
    from trader.models import OrderRequest, Side

    settings = load_settings(PROJECT_ROOT).model_copy(update={"root": tmp_path})
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    j = Journal(settings.journal_path)
    j.record_order(OrderRequest(sleeve_id="S1", symbol="SPY", side=Side.BUY, qty=1.0, reason="t"),
                   "us_equity")
    j.close()

    dest = tmp_path / "backups"
    assert cmd_backup(settings, argparse.Namespace(dest=str(dest), keep=30)) == 0
    archives = list(dest.glob("journal-*.db.gz"))
    assert len(archives) == 1, f"expected one backup, found {archives}"

    restored = tmp_path / "restored.db"
    with gzip.open(archives[0], "rb") as gz, restored.open("wb") as out:
        shutil.copyfileobj(gz, out)
    conn = sqlite3.connect(restored)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 1, "the record did not survive"
    conn.close()


def test_backups_are_written_outside_the_folder_they_protect(tmp_path):
    """A backup inside data/ is lost to the same rm or disk failure as the original -- which is what
    the six .bak files sitting next to the journal in data/ were."""
    import inspect

    from trader import cli

    src = inspect.getsource(cli.cmd_backup)
    assert "Application Support" in src, "the default destination must be outside the project"
    assert "settings.journal_path.parent" not in src
