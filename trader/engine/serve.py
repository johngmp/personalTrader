"""`trader serve`: the dashboard as a local web page at http://localhost:8050.

Each page load is built fresh from Alpaca and the journal (the file version only updates every 15 minutes).
It listens on 127.0.0.1 only, so nothing outside this Mac can reach it, and it serves the dashboard and
nothing else: the journal, .env and other files are never exposed. It only reads; it never places orders.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trader.config import Settings
from trader.journal.db import Journal

log = logging.getLogger(__name__)
HOST = "127.0.0.1"
QUOTE_TTL_S = 60


class CachedQuotes:
    """A broker whose quotes are memoised briefly, for the DASHBOARD ONLY.

    Building a page prices every symbol, and several code paths ask independently, so one load made 28
    Alpaca quote calls at ~0.8s each: 18-23 seconds a page. The cache is process-wide because `serve`
    rebuilds its brokers on every request, which threw away any per-instance caching.

    Deliberately not used by the engine. A dashboard showing a price a minute old is fine -- it already
    says when the engine last ran -- but the engine SIZES POSITIONS from the quote, and the 1% risk rule
    is only as good as the price it is computed from.
    """

    _cache: dict[tuple[str, str], tuple[float, object]] = {}

    def __init__(self, broker, ttl_s: float = QUOTE_TTL_S) -> None:
        self._broker = broker
        self._ttl_s = ttl_s

    def __getattr__(self, item):
        return getattr(self._broker, item)        # everything else passes straight through

    def get_quote(self, symbol: str, asset_class):
        import time

        key = (self._broker.name, symbol)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl_s:
            return hit[1]
        quote = self._broker.get_quote(symbol, asset_class)
        self._cache[key] = (time.monotonic(), quote)
        return quote


def make_handler(settings: Settings, build_engine, refresh_s: int = 60):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            """Ask Claude about the account. Streams back as server-sent events.

            Everything the agent may do is decided by trader.chat's PreToolUse hook, not here.
            """
            if self.path.split("?")[0] != "/ask":
                self.send_error(404, "Only /ask accepts POST")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                question = str(body.get("question", "")).strip()
                resume = body.get("session_id") or None
            except (ValueError, TypeError):
                self.send_error(400, "Expected JSON with a 'question'")
                return
            if not question:
                self.send_error(400, "Nothing was asked")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._stream_answer(question, resume)

        def _intraday(self) -> None:
            """Intraday candles for one symbol. Fetched on demand: pulling these for every symbol on
            every page load would put the 18-second page build straight back."""
            from urllib.parse import parse_qs, urlparse

            from trader.data.intraday import IntradayError, candles

            params = parse_qs(urlparse(self.path).query)
            symbol = (params.get("symbol") or [""])[0]
            window = (params.get("range") or ["1D"])[0]
            known = set(settings.universe.symbols)
            if symbol not in known:          # never let a query string choose what gets fetched
                self.send_error(400, "Unknown symbol")
                return
            try:
                body = json.dumps({"candles": candles(symbol, window)}).encode()
                status = 200
            except IntradayError as exc:
                body = json.dumps({"error": str(exc)}).encode()
                status = 200                  # a shut market is an answer, not a server fault
            except Exception as exc:
                log.exception("intraday failed")
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream_answer(self, question: str, resume: str | None) -> None:
            import anyio

            from trader.chat import ask_stream

            def send(kind: str, payload) -> None:
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, 'payload': payload})}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise KeyboardInterrupt from None   # the tab closed; stop working

            async def pump() -> None:
                async for kind, payload in ask_stream(question, settings.root, resume):
                    send(kind, payload)

            try:
                anyio.run(pump)
            except KeyboardInterrupt:
                pass
            except Exception as exc:
                log.exception("ask failed")
                try:
                    send("error", f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?")[0]
            if path == "/intraday":
                self._intraday()
                return
            if path not in ("/", "/index.html"):
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
        return [Engine(settings, CachedQuotes(broker), journal, bars)
                for broker, bars in configured_brokers(settings, journal)]

    # Threaded: a question can take a minute, and the dashboard must stay usable while it runs.
    server = ThreadingHTTPServer((HOST, port), make_handler(settings, build_engine))
    print(f"Dashboard at http://localhost:{port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
