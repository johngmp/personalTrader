"""Daily backtester built to avoid the usual ways backtests flatter a strategy.

- Decisions are made at a day's close and filled at the NEXT trading day's open (no same-bar fills).
- Every fill pays half the spread + slippage in price, plus fees (estimated and broker-charged).
- Stops are checked against each day's low; a gap below the stop fills at the (worse) open price.
- Position sizes come from the same sizing rules and sleeve limits used for live trading.
- Buys can't spend more cash than the sleeve has (no hidden leverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trader.config import CostModel, SleeveLimits
from trader.risk.sizing import capped_weight, fit_to_leverage
from trader.strategies.base import Holding, Strategy

MIN_ORDER_USD = 1.0
REBALANCE_BAND = 0.10  # resize only when a position is >10% away from its target value


@dataclass
class TradeRecord:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float                    # after all costs
    initial_risk: float | None    # $ lost if the first stop had been hit
    bars_held: int
    exit_reason: str

    @property
    def r_multiple(self) -> float | None:
        return self.pnl / self.initial_risk if self.initial_risk else None


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    symbols: list[str]
    equity: pd.Series
    exposure: pd.Series
    trades: list[TradeRecord]
    symbol_pnl: dict[str, float]
    n_orders: int
    total_costs: float


@dataclass
class _Position:
    holding: Holding
    cost_basis: float           # $ paid for the current qty, including costs
    realized: float = 0.0       # profit already booked by partial sells
    initial_risk: float | None = None
    peak_qty: float = 0.0


@dataclass
class _Order:
    qty_delta: float            # + buy, - sell
    stop: float | None = None
    reason: str = ""


@dataclass
class _Book:
    cash: float
    positions: dict[str, _Position] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    symbol_cash: dict[str, float] = field(default_factory=dict)
    n_orders: int = 0
    total_costs: float = 0.0


def run_backtest(
    strategy: Strategy,
    bars: dict[str, pd.DataFrame],
    capital: float,
    limits: SleeveLimits,
    costs: dict[str, CostModel],
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> BacktestResult:
    strategy.setup({s: bars[s] for s in strategy.symbols})
    ohlc = {s: dict(zip(bars[s].index, bars[s][["open", "high", "low", "close"]].itertuples(index=False)))
            for s in strategy.symbols}
    calendar = sorted(set().union(*(bars[s].index for s in strategy.symbols)))
    if start is not None:
        calendar = [d for d in calendar if d >= start]
    if end is not None:
        calendar = [d for d in calendar if d <= end]

    book = _Book(cash=capital, symbol_cash={s: 0.0 for s in strategy.symbols})
    pending: dict[str, _Order] = {}
    last_close: dict[str, float] = {}
    equity_curve, exposure_curve = [], []

    for date in calendar:
        today = {s: ohlc[s][date] for s in strategy.symbols if date in ohlc[s]}

        # 1) Fill yesterday's decisions at today's open (sells first to free cash).
        for symbol in sorted(pending, key=lambda s: pending[s].qty_delta):
            if symbol in today:
                _execute(book, symbol, pending.pop(symbol), today[symbol].open, date, costs[symbol], "signal")

        # 2) Stops, checked against today's range.
        for symbol, pos in list(book.positions.items()):
            bar = today.get(symbol)
            stop = pos.holding.stop
            if bar is not None and stop is not None and bar.low <= stop:
                fill = min(bar.open, stop)  # gapped through the stop: take the open
                pending.pop(symbol, None)
                _execute(book, symbol, _Order(-pos.holding.qty), fill, date, costs[symbol], "stop")

        # 3) Mark to market at the close.
        for symbol, bar in today.items():
            last_close[symbol] = bar.close
            pos = book.positions.get(symbol)
            if pos:
                pos.holding.bars_held += 1
                pos.holding.high_since_entry = max(pos.holding.high_since_entry, bar.close)
        values = {s: p.holding.qty * last_close[s] for s, p in book.positions.items()}
        equity = book.cash + sum(values.values())
        equity_curve.append(equity)
        exposure_curve.append(sum(abs(v) for v in values.values()) / equity if equity > 0 else 0.0)

        # 4) Strategy decides at the close; orders fill tomorrow.
        if equity > 0:
            _plan_orders(strategy, date, today, book, values, equity, limits, pending)

    index = pd.DatetimeIndex(calendar)
    symbol_pnl = {s: book.symbol_cash[s] + (book.positions[s].holding.qty * last_close[s] if s in book.positions else 0.0)
                  for s in strategy.symbols}
    return BacktestResult(
        strategy=strategy.name, params=dict(strategy.params), symbols=list(strategy.symbols),
        equity=pd.Series(equity_curve, index=index, name="equity"),
        exposure=pd.Series(exposure_curve, index=index, name="exposure"),
        trades=book.trades, symbol_pnl=symbol_pnl, n_orders=book.n_orders, total_costs=book.total_costs,
    )


def _plan_orders(strategy: Strategy, date: pd.Timestamp, today: dict, book: _Book, values: dict[str, float],
                 equity: float, limits: SleeveLimits, pending: dict[str, _Order]) -> None:
    """Turn today's targets into orders, sizing all of them together so exits free room for entries."""
    holdings = {s: p.holding for s, p in book.positions.items()}
    targets = {s: t for s, t in strategy.on_bar(date, holdings).items() if s in today}

    planned = {s: v / equity for s, v in values.items()}   # weight each symbol will have after today
    changeable: set[str] = set()                           # new or resized positions (can be scaled down)
    for symbol, target in targets.items():
        pos = book.positions.get(symbol)
        if target.weight <= 0:
            planned[symbol] = 0.0
            continue
        if pos is not None and target.stop is not None:
            pos.holding.stop = target.stop
        if pos is None or target.resize:
            planned[symbol] = capped_weight(target.weight, today[symbol].close, target.stop, limits)
            changeable.add(symbol)

    fixed = sum(w for s, w in planned.items() if s not in changeable)
    room = fit_to_leverage(sum(planned[s] for s in changeable), fixed, limits)
    wanted = sum(planned[s] for s in changeable)
    scale = room / wanted if wanted > 0 else 0.0

    for symbol, target in targets.items():
        pos = book.positions.get(symbol)
        price = today[symbol].close
        if target.weight <= 0:
            if pos:
                pending[symbol] = _Order(-pos.holding.qty, reason=target.reason)
            continue
        if symbol not in changeable:
            continue
        desired_qty = planned[symbol] * scale * equity / price
        if pos is None:
            if desired_qty * price >= MIN_ORDER_USD:
                pending[symbol] = _Order(desired_qty, stop=target.stop, reason=target.reason)
            continue
        gap = desired_qty - pos.holding.qty
        if abs(gap) * price >= max(MIN_ORDER_USD, REBALANCE_BAND * desired_qty * price):
            pending[symbol] = _Order(gap, reason=target.reason)


def _execute(book: _Book, symbol: str, order: _Order, ref_price: float, date: pd.Timestamp,
             cost: CostModel, how: str) -> None:
    slip = (cost.half_spread_bps + cost.slippage_bps) / 10_000
    fee_rate = (cost.fee_bps + cost.broker_fee_bps) / 10_000
    pos = book.positions.get(symbol)

    if order.qty_delta > 0:  # buy
        fill = ref_price * (1 + slip)
        qty = min(order.qty_delta, book.cash / (fill * (1 + fee_rate)))
        if qty * fill < MIN_ORDER_USD:
            return
        paid = qty * fill * (1 + fee_rate)
        book.cash -= paid
        book.symbol_cash[symbol] -= paid
        book.total_costs += qty * ref_price * slip + qty * fill * fee_rate
        book.n_orders += 1
        if pos is None:
            holding = Holding(qty=qty, entry_price=fill, entry_date=date, stop=order.stop, high_since_entry=fill)
            risk = (fill - order.stop) * qty if order.stop is not None and order.stop < fill else None
            book.positions[symbol] = _Position(holding=holding, cost_basis=paid, initial_risk=risk, peak_qty=qty)
        else:
            pos.holding.qty += qty
            pos.cost_basis += paid
            pos.peak_qty = max(pos.peak_qty, pos.holding.qty)
        return

    if pos is None:  # sell with nothing held
        return
    qty = min(-order.qty_delta, pos.holding.qty)
    fill = ref_price * (1 - slip)
    received = qty * fill * (1 - fee_rate)
    book.cash += received
    book.symbol_cash[symbol] += received
    book.total_costs += qty * ref_price * slip + qty * fill * fee_rate
    book.n_orders += 1
    basis_sold = pos.cost_basis * qty / pos.holding.qty
    pos.realized += received - basis_sold
    pos.cost_basis -= basis_sold
    pos.holding.qty -= qty
    if pos.holding.qty <= 1e-12:
        h = pos.holding
        book.trades.append(TradeRecord(
            symbol=symbol, entry_date=h.entry_date, exit_date=date, entry_price=h.entry_price, exit_price=fill,
            qty=pos.peak_qty, pnl=pos.realized, initial_risk=pos.initial_risk, bars_held=h.bars_held,
            exit_reason=order.reason or how,
        ))
        del book.positions[symbol]
