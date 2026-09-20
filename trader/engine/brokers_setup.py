"""Which brokers to run.

- Alpaca always.
- OANDA once OANDA_API_TOKEN and OANDA_ACCOUNT_ID are in .env and a forex sleeve is enabled.
- cTrader (Fusion Markets demo) is not connectable yet — enabling one of its sleeves raises.
- Shadow (live Alpaca prices, no orders sent) when a sleeve with `broker: shadow` is enabled.
"""

from __future__ import annotations

import os

from trader.brokers.base import Broker
from trader.config import Settings, load_secrets
from trader.journal.db import Journal


def configured_brokers(settings: Settings, journal: Journal | None = None) -> list[tuple[Broker, object]]:
    """(broker, daily bar source) pairs for every broker with credentials and at least one enabled sleeve."""
    from trader.brokers.alpaca import AlpacaBroker
    from trader.data.live_bars import AlpacaDailyBars

    secrets = load_secrets()  # also loads .env into the environment
    alpaca = AlpacaBroker(secrets.alpaca_api_key, secrets.alpaca_secret_key)
    alpaca_bars = AlpacaDailyBars(secrets.alpaca_api_key, secrets.alpaca_secret_key)
    out: list[tuple[Broker, object]] = [(alpaca, alpaca_bars)]

    def wants(broker: str) -> bool:
        return any(s.enabled and s.broker == broker for s in settings.sleeves.values())

    token = os.environ.get("OANDA_API_TOKEN", "").strip()
    account = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    if token and account and wants("oanda"):
        from trader.brokers.oanda import OandaBroker
        from trader.data.oanda_bars import OandaDailyBars

        out.append((OandaBroker(token, account), OandaDailyBars(token)))
    if wants("ctrader"):
        # The conversions and the demo-only guard exist (trader/brokers/ctrader.py); the connection does
        # not, because it needs an app Spotware has to approve first. Fail loudly: a sleeve that is
        # switched on but silently never trades is the kind of thing you notice a month late.
        raise RuntimeError(
            "A sleeve is set to broker 'ctrader', but the cTrader connection isn't built yet — only the "
            "conversions and the demo-only guard are. Disable that sleeve, or finish the adapter once "
            "Spotware approves the Open API application."
        )
    if journal is not None and wants("shadow"):
        from trader.brokers.shadow import ShadowBroker

        out.append((ShadowBroker(alpaca, journal, settings), alpaca_bars))
    if journal is not None and wants("shadow_fx"):
        # Forex with no broker at all: Yahoo prices, shadow execution. Needs no credentials, so it runs
        # whether or not cTrader is ever approved.
        from trader.brokers.shadow import ShadowBroker
        from trader.data.fx_quotes import YahooFXFeed
        from trader.data.yahoo_bars import YahooDailyBars

        feed = YahooFXFeed()
        out.append((ShadowBroker(feed, journal, settings, name="shadow_fx"), YahooDailyBars()))
    return out
