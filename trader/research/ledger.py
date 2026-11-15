"""The trial ledger: every backtest ever run, counted once, in a file that cannot be quietly edited.

Every score this project reports depends on N -- how many variants were tried before the winner was
picked. The Deflated Sharpe Ratio exists to charge for that search, and it can only charge for searching
it is told about. Until now it was told by `log_result`, which is reachable only through `trader gate1`.
Everything else searched for free: all the intraday research, the post-earnings drift study, volatility
targeting, the calendar blackout, and every `trader backtest` run. Measured on this repo that is about
175 real trials against 216 counted -- the bar was set as though we had looked half as hard as we did.

The fix is the one both 2026 papers on agentic quant research insist on: completeness has to come
"by construction rather than by author diligence". So the recording happens inside `run_backtest`, which
is the single function every daily backtest in this project already goes through. A backtest that does
not record a trial is not discouraged; it is unreachable.

Three things make a trial distinct, and the third is the one that was missing:

1. the settings and the symbols -- the obvious part, and all the old counter looked at;
2. the data span -- a longer history is a different test;
3. the COST MODEL and the STRATEGY'S OWN SOURCE. This is what closes the hole the log recorded in
   plain sight: rows 8, 9 and 10 of `research_log.jsonl` are three S4 runs inside three minutes, same
   grid, same four symbols, the benchmark moving 0.0 -> 10.521 -> -0.088 as a bug was found and fixed,
   PASS -> FAIL -> FAIL. Three fresh looks at the same out-of-sample window, and the counter stayed at
   36. Changing what you charge, or what the strategy does, and looking again IS looking again.

The source fingerprint is taken from the parsed syntax tree with docstrings removed, so rewording a
comment or a docstring -- which this project does constantly -- is not mistaken for a new experiment,
while changing a single operator is.

The file is hash-chained: each row carries the previous row's hash, and its own hash covers both. Delete
a row, reorder two, or edit a number, and `verify_chain` fails. Gate 1 then refuses to score rather than
quoting a confidence built on a ledger someone has tidied up. A ledger you can edit is a ledger that
tells you what you want to hear.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import textwrap
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from trader.config import PROJECT_ROOT, CostModel

# Read at CALL time, never bound at import: tests redirect this to a temp file, and a default argument
# captured at import would quietly keep writing to the real ledger. Same reasoning as `RESEARCH_LOG`.
TRIAL_LEDGER = PROJECT_ROOT / "reports" / "trials.jsonl"

GENESIS = "0" * 64
ID_CHARS = 16


def _canonical(obj: Any) -> str:
    """One text form per value, so the same trial always hashes the same way."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Prose is not logic. Without this, rewording a docstring would read as a new experiment."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return tree


@lru_cache(maxsize=None)
def source_fingerprint(cls: type) -> str:
    """What the strategy actually does, ignoring comments, blank lines, formatting and docstrings.

    Returns "unknown" when the source cannot be read (a class defined in a REPL, say). That degrades
    to the old behaviour -- the code stops counting as part of the trial's identity -- rather than
    refusing to run, because an uncounted trial is better than no backtest at all.
    """
    try:
        source = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return "unknown"
    return _sha(ast.dump(_strip_docstrings(ast.parse(source))))[:ID_CHARS]


def _cost_key(costs: dict[str, CostModel]) -> dict[str, list[float]]:
    return {s: [c.half_spread_bps, c.slippage_bps, c.fee_bps, c.broker_fee_bps, c.carry_bps_per_day]
            for s, c in sorted(costs.items())}


def trial_id(strategy: str, params: dict, symbols: list[str], costs: dict[str, CostModel],
             span: tuple[Any, Any], source: str) -> str:
    """The identity of one look at the data. Two runs sharing it are the same test, run twice."""
    return _sha(_canonical({
        "strategy": strategy,
        "params": params,
        "symbols": sorted(symbols),
        "costs": _cost_key(costs),
        "span": [str(span[0])[:10], str(span[1])[:10]],
        "source": source,
    }))[:ID_CHARS]


def _rows(path: Path | None = None) -> list[dict]:
    path = path if path is not None else TRIAL_LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def last_hash(path: Path | None = None) -> str:
    """The head of the chain, read from the END of the file rather than by parsing all of it.

    `evaluate` records eighteen trials per run and the ledger only ever grows, so a full parse here
    would make recording quadratic in the history. A window off the end is enough: rows are a few
    hundred bytes, and the rare row that is not falls back to the slow path rather than guessing.
    """
    path = path if path is not None else TRIAL_LEDGER
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - 8192))
        lines = [x for x in f.read().decode(errors="replace").splitlines() if x.strip()]
    try:
        return json.loads(lines[-1])["hash"]
    except (IndexError, json.JSONDecodeError, KeyError):
        rows = _rows(path)
        return rows[-1]["hash"] if rows else GENESIS


def record_trial(*, strategy: str, params: dict, symbols: list[str], costs: dict[str, CostModel],
                 span: tuple[Any, Any], source: str, sharpe: float, note: str = "",
                 path: Path | None = None) -> str:
    """Append one row and return its trial id. Every run is recorded; `trial_count` counts the distinct.

    Keeping repeats rather than deduplicating on write is deliberate: the ledger is a record of what was
    done, and "this was run six times" is itself evidence worth having. Only the counting deduplicates.
    """
    path = path if path is not None else TRIAL_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    tid = trial_id(strategy, params, symbols, costs, span, source)
    prev = last_hash(path)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy": strategy, "trial": tid, "params": params, "symbols": sorted(symbols),
        "span": [str(span[0])[:10], str(span[1])[:10]], "source": source,
        "sharpe": None if sharpe is None else round(float(sharpe), 4), "note": note, "prev": prev,
    }
    row["hash"] = _sha(prev + _canonical(row))
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return tid


def verify_chain(path: Path | None = None) -> tuple[bool, str]:
    """(ok, reason). False means a row was edited, removed or reordered after it was written."""
    prev = GENESIS
    for i, row in enumerate(_rows(path), start=1):
        row = dict(row)
        stated = row.pop("hash", None)
        if row.get("prev") != prev:
            return False, f"row {i} does not follow row {i - 1}: the chain is broken"
        if _sha(prev + _canonical(row)) != stated:
            return False, f"row {i} has been edited since it was written"
        prev = stated
    return True, "ok"


def trials_for(strategy: str, path: Path | None = None) -> list[dict]:
    """One row per DISTINCT trial for this strategy, earliest first."""
    seen: dict[str, dict] = {}
    for row in _rows(path):
        if row.get("strategy") == strategy and row["trial"] not in seen:
            seen[row["trial"]] = row
    return list(seen.values())


def trial_count(strategy: str, path: Path | None = None) -> int:
    return len(trials_for(strategy, path))


def trial_sharpes(strategy: str, path: Path | None = None) -> list[float]:
    """Every distinct trial's Sharpe, which is what the Deflated Sharpe needs the SPREAD of.

    The old code passed the current run's 18 grid cells. Those are 18 moving-average pairs over the same
    prices -- they agree with each other almost by construction (S8's spread across them is 0.076), and a
    small spread means a low luck bar however large N is. The ledger's trials span different symbol
    baskets, cost models and strategy versions, so their spread is a far better estimate of how much
    result a search of this kind can move.
    """
    return [r["sharpe"] for r in trials_for(strategy, path) if r.get("sharpe") is not None]
