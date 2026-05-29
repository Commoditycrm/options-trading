"""Broker-agnostic listener dispatcher (App 2).

Routes trade-detection by ``BrokerName``:
  - ALPACA     → the existing ``alpaca_stream`` (WebSocket) — unchanged
  - WEBULL     → ``webull_listener`` (2s polling)
  - SNAPTRADE  → ``snaptrade_listener`` (polling + webhook)

The webull/snaptrade listener modules import the fragile ``webull`` /
``snaptrade_client`` SDKs at module top, so they're imported LAZILY here and
any ImportError is swallowed — the app boots (and Alpaca detection works)
even when those SDKs aren't installed. Detected orders from every backend
flow into ``copy_engine.dispatch_detected_order`` → the queue fast path.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.services import alpaca_stream, listener_state

log = logging.getLogger(__name__)


def _webull():
    from app.services import webull_listener
    return webull_listener


def _snaptrade():
    from app.services import snaptrade_listener
    return snaptrade_listener


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Forward the running loop to the polling backends. Best-effort: skip a
    backend whose SDK isn't installed."""
    for getter, name in ((_webull, "webull"), (_snaptrade, "snaptrade")):
        try:
            getter().bind_loop(loop)
        except ImportError:
            log.info("listeners.bind_loop: %s SDK not installed; skipping", name)
        except Exception:  # noqa: BLE001
            log.exception("listeners.bind_loop: %s failed", name)


async def start_all_listeners() -> None:
    """Spawn Webull + SnapTrade listeners for every connected trader account.
    Alpaca is started separately via alpaca_stream.start_all_streams() in the
    main startup hook."""
    for getter, name in ((_webull, "webull"), (_snaptrade, "snaptrade")):
        try:
            await getter().start_all_listeners()
        except ImportError:
            log.info("listeners.start_all: %s SDK not installed; skipping", name)
        except Exception:  # noqa: BLE001
            log.exception("listeners.start_all: %s failed", name)


async def stop_all_listeners() -> None:
    for getter, name in ((_webull, "webull"), (_snaptrade, "snaptrade")):
        try:
            await getter().stop_all_listeners()
        except ImportError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("listeners.stop_all: %s failed", name)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Route to the right backend based on the account's broker."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
    if acct is None:
        log.warning("listeners.start_listener: account %s not found", broker_account_id)
        return
    try:
        if acct.broker == BrokerName.ALPACA:
            alpaca_stream.start_stream(broker_account_id)
        elif acct.broker == BrokerName.WEBULL:
            _webull().start_listener(trader_user_id, broker_account_id)
        elif acct.broker == BrokerName.SNAPTRADE:
            _snaptrade().start_listener(trader_user_id, broker_account_id)
        else:
            log.info("listeners.start_listener: no listener for broker %s", acct.broker.value)
    except ImportError:
        log.info("listeners.start_listener: %s SDK not installed; skipping", acct.broker.value)
    except Exception:  # noqa: BLE001
        log.exception("listeners.start_listener: %s failed", acct.broker.value)


def stop_listener(trader_user_id: uuid.UUID) -> None:
    """Stop whichever backend is servicing this trader. One-broker-per-user
    means at most one is active, but we try all so transitions are clean.
    (Alpaca is also stopped directly by the caller via alpaca_stream.)"""
    for getter, name in ((_webull, "webull"), (_snaptrade, "snaptrade")):
        try:
            getter().stop_listener(trader_user_id)
        except ImportError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("listeners.stop_listener: %s failed", name)
    listener_state.clear(trader_user_id)


# Status is unified in listener_state.
get_status = listener_state.get_status
