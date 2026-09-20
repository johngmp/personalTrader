"""cTrader OAuth: code exchange, refresh rotation and token storage (against a mock endpoint)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trader.brokers.ctrader_auth import (
    REDIRECT_URI,
    SCOPE,
    CTraderAuthError,
    Tokens,
    authorize_url,
    current_tokens,
    exchange_code,
    load_tokens,
    refresh_tokens,
    save_tokens,
)


def client_returning(payload: dict, status: int = 200) -> httpx.Client:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


GOOD = {"accessToken": "access-1", "refreshToken": "refresh-1", "expiresIn": 2_628_000,
        "tokenType": "bearer", "errorCode": None}


# --- the authorization page -----------------------------------------------------------------------

def test_authorize_url_carries_the_app_and_the_trading_scope():
    url = authorize_url("client-abc")
    assert "client_id=client-abc" in url
    assert f"scope={SCOPE}" in url and SCOPE == "trading"       # "accounts" could not place orders
    assert "localhost%3A8765" in url or "localhost:8765" in url


# --- exchanging the code --------------------------------------------------------------------------

def test_code_is_exchanged_for_tokens():
    client = client_returning(GOOD)
    tokens = exchange_code("the-code", "id", "secret", client=client)
    assert (tokens.access_token, tokens.refresh_token) == ("access-1", "refresh-1")
    assert tokens.expires_at > datetime.now(timezone.utc) + timedelta(days=29)


def test_the_exchange_sends_what_ctrader_expects():
    client = client_returning(GOOD)
    exchange_code("the-code", "id", "secret", client=client)
    sent = dict(client.seen[0].url.params)  # type: ignore[attr-defined]
    assert sent == {"grant_type": "authorization_code", "code": "the-code", "redirect_uri": REDIRECT_URI,
                    "client_id": "id", "client_secret": "secret"}


def test_a_failure_arriving_as_http_200_still_raises():
    """cTrader reports errors with a 200 and an errorCode, so the status code alone proves nothing."""
    client = client_returning({"errorCode": "INVALID_REQUEST", "description": "code expired"})
    with pytest.raises(CTraderAuthError, match="INVALID_REQUEST: code expired"):
        exchange_code("stale", "id", "secret", client=client)


def test_an_http_error_raises():
    with pytest.raises(CTraderAuthError, match="HTTP 401"):
        exchange_code("x", "id", "secret", client=client_returning({}, status=401))


def test_a_response_missing_fields_raises_rather_than_storing_half_a_token():
    client = client_returning({"accessToken": "a", "expiresIn": 100})
    with pytest.raises(CTraderAuthError, match="refreshToken"):
        exchange_code("x", "id", "secret", client=client)


# --- refreshing -----------------------------------------------------------------------------------

def test_refresh_uses_the_refresh_grant():
    client = client_returning({**GOOD, "accessToken": "access-2", "refreshToken": "refresh-2"})
    tokens = refresh_tokens("refresh-1", "id", "secret", client=client)
    sent = dict(client.seen[0].url.params)  # type: ignore[attr-defined]
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "refresh-1"
    assert tokens.refresh_token == "refresh-2"   # rotated: storing the old one would break the next refresh


# --- storage --------------------------------------------------------------------------------------

def test_tokens_survive_a_save_and_load(tmp_path):
    path = tmp_path / "data" / "ctrader_tokens.json"
    tokens = Tokens("a", "r", datetime.now(timezone.utc) + timedelta(days=30))
    save_tokens(path, tokens)
    assert load_tokens(path) == tokens


def test_the_token_file_is_not_readable_by_anyone_else(tmp_path):
    path = tmp_path / "ctrader_tokens.json"
    save_tokens(path, Tokens("a", "r", datetime.now(timezone.utc)))
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_missing_token_file_says_what_to_run(tmp_path):
    with pytest.raises(CTraderAuthError, match="ctrader-auth"):
        load_tokens(tmp_path / "nope.json")


def test_a_corrupt_token_file_raises_clearly(tmp_path):
    path = tmp_path / "ctrader_tokens.json"
    path.write_text("{not json")
    with pytest.raises(CTraderAuthError, match="unreadable"):
        load_tokens(path)


# --- refreshing on use ----------------------------------------------------------------------------

def test_fresh_tokens_are_used_as_they_are(tmp_path):
    path = tmp_path / "t.json"
    save_tokens(path, Tokens("access-1", "refresh-1", datetime.now(timezone.utc) + timedelta(days=30)))
    client = client_returning(GOOD)
    assert current_tokens(path, "id", "secret", client=client).access_token == "access-1"
    assert not client.seen  # type: ignore[attr-defined]  -- no needless call to cTrader


def test_tokens_near_expiry_are_refreshed_and_written_back(tmp_path):
    """The refresh token rotates, so a refresh that isn't saved strands the engine at the next one."""
    path = tmp_path / "t.json"
    save_tokens(path, Tokens("access-1", "refresh-1", datetime.now(timezone.utc) + timedelta(days=1)))
    client = client_returning({**GOOD, "accessToken": "access-2", "refreshToken": "refresh-2"})
    assert current_tokens(path, "id", "secret", client=client).access_token == "access-2"
    assert json.loads(path.read_text())["refresh_token"] == "refresh-2"


def test_an_already_expired_token_is_refreshed_rather_than_used(tmp_path):
    path = tmp_path / "t.json"
    save_tokens(path, Tokens("old", "refresh-1", datetime.now(timezone.utc) - timedelta(days=1)))
    client = client_returning({**GOOD, "accessToken": "access-2"})
    assert current_tokens(path, "id", "secret", client=client).access_token == "access-2"
