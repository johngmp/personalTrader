"""`trader app`: the dashboard in a real Mac window instead of a browser tab.

A native WKWebView wrapper around the local dashboard, so it gets its own Dock icon, its own window and
the usual Mac shortcuts, and does not disappear among forty browser tabs. There is no second copy of the
UI here -- it is the same page `trader serve` renders, which is the point: one dashboard, two ways in.

It starts the server itself if nothing is listening, so double-clicking the app works from cold. If the
launchd agent is already serving, it just attaches to it.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8050
TITLE = "Personal Trader"
START_TIMEOUT_S = 30


def is_serving(host: str = HOST, port: int = PORT, timeout: float = 0.4) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def start_server(root: Path) -> subprocess.Popen | None:
    """Launch `trader serve` in the background. Returns the process, or None if one was already up."""
    if is_serving():
        return None
    process = subprocess.Popen(
        [sys.executable, "-m", "trader.cli", "serve"], cwd=str(root),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_serving():
            return process
        if process.poll() is not None:
            raise RuntimeError("the dashboard server exited while starting; try `trader serve` to see why")
        time.sleep(0.3)
    process.terminate()
    raise RuntimeError(f"the dashboard did not start within {START_TIMEOUT_S}s")


def name_the_app(title: str = TITLE) -> None:
    """Make the Dock and menu bar say the app's name instead of "python3".

    The .app launcher execs `uv run trader app`, so the process macOS actually sees is Python, and it
    takes its name from Python's own bundle. Patching the main bundle's info dictionary before any
    window exists is the supported way to correct that.
    """
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = title
            info["CFBundleDisplayName"] = title
    except Exception:
        pass        # cosmetic only; never stop the app opening over a label


def run(root: Path) -> int:
    import webview

    name_the_app()
    started = start_server(root)
    try:
        webview.create_window(TITLE, f"http://{HOST}:{PORT}", width=1280, height=900,
                              min_size=(900, 600), background_color="#141412")
        webview.start()          # blocks until the window closes
    finally:
        # Only stop the server if THIS process started it: a launchd-managed one must outlive the window.
        if started is not None:
            started.terminate()
    return 0
