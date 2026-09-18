"""Which brokers to run: Alpaca always; OANDA once OANDA_API_TOKEN and OANDA_ACCOUNT_ID are in .env."""

from __future__ import annotations

import os

from trader.brokers.base import Broker
from trader.config import Settings, load_secrets


def configured_brokers(settings: Settings) -> list[tuple[Broker, object]]:
    """(broker, daily bar source) pairs for every broker with credentials and at least one enabled sleeve."""
    from trader.brokers.alpaca import AlpacaBroker
    from trader.data.live_bars import AlpacaDailyBars

    secrets = load_secrets()  # also loads .env into the environment
    out: list[tuple[Broker, object]] = [(AlpacaBroker(secrets.alpaca_api_key, secrets.alpaca_secret_key),
                                         AlpacaDailyBars(secrets.alpaca_api_key, secrets.alpaca_secret_key))]
    token = os.environ.get("OANDA_API_TOKEN", "").strip()
    account = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    wants_oanda = any(s.enabled and s.broker == "oanda" for s in settings.sleeves.values())
    if token and account and wants_oanda:
        from trader.brokers.oanda import OandaBroker
        from trader.data.oanda_bars import OandaDailyBars

        out.append((OandaBroker(token, account), OandaDailyBars(token)))
    return out
