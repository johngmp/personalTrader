"""The Mac app launcher. No GUI is opened here; only the start/attach logic is exercised."""

from __future__ import annotations

import inspect
import socket
import threading

import pytest

from trader import app as app_module
from trader.app import TITLE, is_serving, start_server


@pytest.fixture
def open_port():
    """A real listening socket, so is_serving() is tested against a port rather than a mock."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    yield port
    server.close()


def test_a_listening_port_is_detected(open_port):
    assert is_serving("127.0.0.1", open_port)


def test_a_closed_port_is_detected():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                      # nothing is listening now
    assert not is_serving("127.0.0.1", port, timeout=0.2)


def test_it_attaches_to_a_running_server_instead_of_starting_a_second_one(monkeypatch, tmp_path):
    """Two servers on one port would mean one of them silently fails to bind."""
    monkeypatch.setattr(app_module, "is_serving", lambda *a, **k: True)
    spawned = []
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: spawned.append(a) or object())
    assert start_server(tmp_path) is None
    assert not spawned


def test_a_server_it_did_not_start_is_left_running():
    """The launchd agent must outlive the window: closing the app must not take the dashboard down."""
    source = inspect.getsource(app_module.run)
    assert "if started is not None:" in source
    assert "started.terminate()" in source


def test_the_window_shows_the_same_dashboard_rather_than_a_second_copy():
    source = inspect.getsource(app_module)
    assert "create_window" in source
    assert 'f"http://{HOST}:{PORT}"' in source, "the window must point at the running dashboard"
    assert "dashboard_html" not in source, "the app must not render its own copy of the UI"


def test_renaming_the_app_never_stops_it_opening():
    """The Dock label is cosmetic; a failure there must not keep the window shut."""
    source = inspect.getsource(app_module.name_the_app)
    assert "except Exception" in source and "pass" in source


def test_it_gives_up_rather_than_hanging_if_the_server_never_comes_up(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "is_serving", lambda *a, **k: False)
    monkeypatch.setattr(app_module, "START_TIMEOUT_S", 0.5)

    class NeverReady:
        def poll(self): return None
        def terminate(self): self.terminated = True

    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: NeverReady())
    with pytest.raises(RuntimeError, match="did not start"):
        start_server(tmp_path)


def test_a_server_that_dies_on_startup_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "is_serving", lambda *a, **k: False)

    class DiesImmediately:
        def poll(self): return 1
        def terminate(self): pass

    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: DiesImmediately())
    with pytest.raises(RuntimeError, match="exited while starting"):
        start_server(tmp_path)


def test_the_window_is_named():
    assert TITLE == "Personal Trader"
