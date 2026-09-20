"""OAuth for the cTrader Open API: turn app credentials into account tokens, and keep them fresh.

The flow is the standard "sign in with X" dance. You approve the app in a browser, cTrader redirects to
http://localhost:8765 with a short-lived code, and that code is traded for an access token (~30 days) and
a refresh token (no expiry). Both are stored in `data/ctrader_tokens.json`, owner-readable only.

Tokens live in their own file rather than .env on purpose: .env holds what a human typed, this holds what
the program rotates. A refresh returns a NEW refresh token as well, so the file is rewritten each time --
lose that write and forex silently stops trading a month later.

Two quirks of their API, neither of them ours to fix:
- the token endpoint is a GET, so the client secret travels in a query string
- the response is camelCase (`accessToken`), and failures arrive as HTTP 200 with `errorCode` set
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
REDIRECT_URI = "http://localhost:8765"
SCOPE = "trading"          # "accounts" is read-only; we need to place orders
REFRESH_MARGIN = timedelta(days=3)  # refresh early, so a quiet weekend can't strand us


class CTraderAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: datetime

    @property
    def needs_refresh(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at - REFRESH_MARGIN

    def as_dict(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> Tokens:
        return cls(access_token=d["access_token"], refresh_token=d["refresh_token"],
                   expires_at=datetime.fromisoformat(d["expires_at"]))


def authorize_url(client_id: str, redirect_uri: str = REDIRECT_URI, scope: str = SCOPE) -> str:
    """The page the user opens to approve the app against their own accounts."""
    return AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                                       "scope": scope, "product": "web"})


def _token_request(params: dict, client: httpx.Client | None = None) -> Tokens:
    owned = client is None
    client = client or httpx.Client(timeout=20)
    try:
        response = client.get(TOKEN_URL, params=params)
    finally:
        if owned:
            client.close()
    if response.status_code >= 400:
        raise CTraderAuthError(f"cTrader token endpoint returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CTraderAuthError("cTrader token endpoint did not return JSON") from exc
    # Failures come back as HTTP 200 with errorCode set, so status code alone proves nothing.
    if data.get("errorCode"):
        raise CTraderAuthError(f"{data['errorCode']}: {data.get('description') or 'no description given'}")
    missing = [k for k in ("accessToken", "refreshToken", "expiresIn") if not data.get(k)]
    if missing:
        raise CTraderAuthError(f"cTrader token response is missing {', '.join(missing)}")
    return Tokens(access_token=data["accessToken"], refresh_token=data["refreshToken"],
                  expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(data["expiresIn"])))


def exchange_code(code: str, client_id: str, client_secret: str,
                  redirect_uri: str = REDIRECT_URI, client: httpx.Client | None = None) -> Tokens:
    """Trade the one-minute authorization code for tokens. Do this immediately; the code expires fast."""
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                           "client_id": client_id, "client_secret": client_secret}, client)


def refresh_tokens(refresh_token: str, client_id: str, client_secret: str,
                   client: httpx.Client | None = None) -> Tokens:
    """Swap a refresh token for a fresh pair. The refresh token itself is rotated, so store the result."""
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token,
                           "client_id": client_id, "client_secret": client_secret}, client)


# --- storage --------------------------------------------------------------------------------------

def save_tokens(path: Path, tokens: Tokens) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.as_dict(), indent=2) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only


def load_tokens(path: Path) -> Tokens:
    if not path.exists():
        raise CTraderAuthError(f"No cTrader tokens at {path}. Run `trader ctrader-auth` first.")
    try:
        return Tokens.from_dict(json.loads(path.read_text()))
    except (ValueError, KeyError) as exc:
        raise CTraderAuthError(f"cTrader token file {path} is unreadable: {exc}") from exc


def current_tokens(path: Path, client_id: str, client_secret: str,
                   client: httpx.Client | None = None) -> Tokens:
    """Tokens that are good to use now, refreshing and re-saving them if they are close to expiry."""
    tokens = load_tokens(path)
    if not tokens.needs_refresh:
        return tokens
    refreshed = refresh_tokens(tokens.refresh_token, client_id, client_secret, client)
    save_tokens(path, refreshed)
    return refreshed
