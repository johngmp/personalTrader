"""`trader serve`: the dashboard as a local web page at http://localhost:8050.

Each page load is built fresh from Alpaca and the journal (the file version only updates every 15 minutes).
It listens on 127.0.0.1 only, so nothing outside this Mac can reach it, and it serves the dashboard and
nothing else: the journal, .env and other files are never exposed. It only reads; it never places orders.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

from trader.config import Settings
from trader.journal.db import Journal

log = logging.getLogger(__name__)
HOST = "127.0.0.1"


def make_handler(settings: Settings, build_engine, refresh_s: int = 60):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404, "Only the dashboard is served here")
                return
            from trader.engine.dashboard import dashboard_html

            journal = Journal(settings.journal_path)  # a fresh read-only use per request
            try:
                body = dashboard_html(settings, journal, build_engine(journal), refresh_s=refresh_s).encode()
                status = 200
            except Exception as exc:  # show the problem instead of a blank page
                log.exception("dashboard failed")
                body = f"<h1>Dashboard error</h1><pre>{type(exc).__name__}: {exc}</pre>".encode()
                status = 500
            finally:
                journal.close()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(settings: Settings, port: int = 8050) -> None:
    from trader.engine.live import Engine

    from trader.engine.brokers_setup import configured_brokers

    def build_engine(journal: Journal) -> list[Engine]:
        # Used only to read values; run_once is never called here.
        return [Engine(settings, broker, journal, bars) for broker, bars in configured_brokers(settings, journal)]

    server = HTTPServer((HOST, port), make_handler(settings, build_engine))
    print(f"Dashboard at http://localhost:{port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
