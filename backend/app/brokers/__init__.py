from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.brokers.ibkr import IbkrAdapter
from app.brokers.mock import MockAdapter
from app.models.broker_account import BrokerAccount, BrokerName


def adapter_for(broker_account: BrokerAccount, credentials: dict) -> BrokerAdapter:
    """Construct an adapter for the broker_account using its decrypted credentials.

    Webull and SnapTrade are imported LAZILY: their adapter modules import
    the third-party ``webull`` / ``snaptrade_client`` SDKs at module top, and
    those packages are fragile (Webull) or only needed when actually used. A
    lazy import keeps ``import app.brokers`` working even if those SDKs aren't
    installed — only a user who connects that broker triggers the import.
    """
    if broker_account.broker == BrokerName.ALPACA:
        return AlpacaAdapter(credentials)
    if broker_account.broker == BrokerName.IBKR:
        return IbkrAdapter(credentials)
    if broker_account.broker == BrokerName.WEBULL:
        from app.brokers.webull import WebullAdapter
        return WebullAdapter(credentials)
    if broker_account.broker == BrokerName.SNAPTRADE:
        from app.brokers.snaptrade import SnapTradeAdapter
        return SnapTradeAdapter(credentials)
    if broker_account.broker == BrokerName.MOCK:
        return MockAdapter(credentials)
    raise ValueError(f"no adapter for {broker_account.broker}")


__all__ = [
    "AlpacaAdapter",
    "BrokerAdapter",
    "BrokerOrderRequest",
    "BrokerOrderResult",
    "BrokerPosition",
    "ConnectionInfo",
    "IbkrAdapter",
    "MockAdapter",
    "adapter_for",
    "build_occ_symbol",
]
