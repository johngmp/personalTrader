"""`trader menubar`: account P&L in the Mac menu bar, so you can see how things are without opening anything.

Read-mostly. It shows numbers and opens the dashboard; the only things it can change are the kill switch
(which only ever makes the system safer or restores it, and asks first) and nothing else. It never places
an order: like everything else here, only the engine does that, through the risk gate.

Numbers come from the journal on disk, not from a broker, so this costs no API calls and keeps working
while the network is down. It is therefore as fresh as the last engine run, which the menu states plainly.
"""

from __future__ import annotations

import subprocess
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import rumps

from trader.config import Settings, load_settings
from trader.journal.db import Journal
from trader.risk.kill_switch import KillSwitch

REFRESH_S = 60
DASHBOARD_URL = "http://localhost:8050"
STALE_AFTER_MIN = 45       # the engine runs every 15 minutes; three misses is worth flagging


def _money(x: float, signed: bool = False) -> str:
    s = f"{x:+,.2f}" if signed else f"{x:,.2f}"
    return s.replace("+", "+$").replace("-", "−$") if s[0] in "+-" else f"${s}"


class TraderBar(rumps.App):
    def __init__(self) -> None:
        super().__init__("PT", quit_button=None)
        self.settings: Settings = load_settings()
        self.menu = ["(loading)"]
        self.refresh(None)

    # --- data ---------------------------------------------------------------------------------

    def _snapshot(self) -> dict:
        journal = Journal(self.settings.journal_path)
        try:
            rows = journal.conn.execute(
                "SELECT ts, equity FROM equity_snapshots WHERE broker='alpaca' ORDER BY ts").fetchall()
            sleeves = []
            for sid, cfg in self.settings.sleeves.items():
                if not cfg.enabled or sid == "TEST":
                    continue
                row = journal.conn.execute(
                    "SELECT equity FROM sleeve_equity WHERE sleeve_id=? ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
                if row:
                    sleeves.append((sid, cfg.name, row["equity"] / cfg.capital_usd - 1,
                                    cfg.broker.startswith("shadow")))
            last_run = journal.kv_get("last_run")
            return {"rows": rows, "sleeves": sleeves, "last_run": last_run,
                    "summary": journal.kv_get("last_run_summary", "")}
        finally:
            journal.close()

    # --- menu ---------------------------------------------------------------------------------

    @rumps.timer(REFRESH_S)
    def refresh(self, _sender) -> None:
        try:
            self._render(self._snapshot())
        except Exception as exc:                      # a broken menu bar must not be a silent one
            self.title = "PT ⚠️"
            self.menu.clear()
            self.menu = [rumps.MenuItem(f"Could not read the journal: {exc}"), None, *self._actions()]

    def _render(self, snap: dict) -> None:
        rows = snap["rows"]
        kill = KillSwitch(self.settings.kill_switch_path)
        items: list = []

        if not rows:
            self.title = "PT –"
            items.append(rumps.MenuItem("No account history yet"))
        else:
            start, now_eq = rows[0]["equity"], rows[-1]["equity"]
            pnl = now_eq - start
            self.title = f"PT {'+' if pnl >= 0 else '−'}${abs(pnl):,.0f}"
            items.append(rumps.MenuItem(f"Account  {_money(now_eq)}"))
            items.append(rumps.MenuItem(f"P&L      {_money(pnl, signed=True)}  ({pnl / start:+.2%})"))

        # Staleness is the thing most worth knowing at a glance: a flat number from a dead engine
        # looks exactly like a flat number from a quiet market.
        age = None
        if snap["last_run"]:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["last_run"])).total_seconds() / 60
            when = f"{age:.0f} min ago" if age < 90 else f"{age / 60:.1f} hours ago"
            items.append(rumps.MenuItem(f"Last run {when}"))
            if age > STALE_AFTER_MIN:
                self.title += " ⚠️"
        if kill.active:
            self.title = "PT 🛑"
            items.append(rumps.MenuItem(f"KILL SWITCH ON — {kill.reason}"))

        if snap["sleeves"]:
            items += [None, rumps.MenuItem("Strategies")]
            for sid, name, ret, shadow in snap["sleeves"]:
                tag = " (shadow)" if shadow else ""
                items.append(rumps.MenuItem(f"   {sid}  {ret:+.2%}   {name[:28]}{tag}"))

        items += [None, *self._actions()]
        self.menu.clear()
        self.menu = items

    def _actions(self) -> list:
        dash = rumps.MenuItem("Open dashboard", callback=self.open_dashboard)
        logs = rumps.MenuItem("Open log folder", callback=self.open_logs)
        kill = KillSwitch(self.settings.kill_switch_path)
        toggle = rumps.MenuItem("Resume trading" if kill.active else "Halt trading…",
                                callback=self.toggle_kill)
        return [dash, logs, None, toggle, rumps.MenuItem("Quit", callback=rumps.quit_application)]

    # --- actions ------------------------------------------------------------------------------

    def open_dashboard(self, _sender) -> None:
        webbrowser.open(DASHBOARD_URL)

    def open_logs(self, _sender) -> None:
        subprocess.run(["open", str(self.settings.root / "data" / "logs")], check=False)

    def toggle_kill(self, _sender) -> None:
        kill = KillSwitch(self.settings.kill_switch_path)
        if kill.active:
            kill.release()
            rumps.notification("Personal Trader", "Kill switch off", "Trading resumes on the next run.")
        else:
            # Halting is the one destructive-ish action here, so it asks rather than firing on a stray click.
            if rumps.alert("Halt trading?", "No new orders will be placed. Stops already resting at the "
                           "broker stay where they are.", ok="Halt", cancel="Cancel") != 1:
                return
            kill.engage("halted from the menu bar")
            rumps.notification("Personal Trader", "Kill switch ON", "No new orders until you resume.")
        self.refresh(None)


def run() -> int:
    TraderBar().run()
    return 0
