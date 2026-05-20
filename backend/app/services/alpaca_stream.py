"""Real-time order/fill updates from Alpaca via TradingStream WebSocket.

Why this exists
---------------
Without this, fills only reach our DB when the frontend hits `POST
/api/trades/sync-fills` (which polls Alpaca's activities feed). That feed
lags minutes behind the actual execution and only runs when a user is
actively looking at the Trades page — so the calendar/P&L stays stale until
someone refreshes.

What this does
--------------
On app startup we open one TradingStream WebSocket per connected Alpaca
account and subscribe to `trade_updates`. When Alpaca pushes an event
(new / fill / partial_fill / canceled / expired / rejected / ...), we:

  1. Locate the local Order row (by broker_order_id, falling back to
     client_order_id which we set to our Order.id on submit).
  2. Update status / filled_quantity / filled_avg_price / closed_at.
  3. For fill/partial_fill events, insert a Fill row (dedup by Alpaca's
     execution_id) and recompute the order's VWAP.
  4. Publish an `order.updated` SSE event so the UI updates without a
     refresh.

`fills_sync.py` still exists as a reconciliation fallback (for periods
the stream was down).

Single-process design
---------------------
Streams run as asyncio tasks inside the FastAPI event loop. For multi-pod
deployment, only one pod should run streams (leader election) OR each
broker_account should be sharded; otherwise duplicate `Fill` rows would
race the broker_fill_id uniqueness check (which we'd add as a constraint).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers.alpaca import _STATUS_IN
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    Fill,
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.models.settings import TraderSettings
from app.models.user import User, UserRole
from app.services import audit, events, fanout_stream
from app.services.copy_engine import enumerate_fanout_targets
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# Order statuses Alpaca considers terminal — once seen, we stamp closed_at.
_TERMINAL_STATUSES = (
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
)

# Active stream tasks, keyed by broker_account_id (uuid). Holds an asyncio.Task
# wrapping the long-lived TradingStream.run() loop.
_streams: dict[uuid.UUID, asyncio.Task] = {}

# Reconnect backoff caps. The TradingStream SDK itself retries internally
# on transient WS drops; we add an outer guard for hard failures (auth,
# bad credentials, etc.) so a single bad account doesn't loop forever
# in 100ms-spin.
_RECONNECT_MIN_SECONDS = 5
_RECONNECT_MAX_SECONDS = 60


def _dec(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal(0)
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _resolve_local_order(db, account_id: uuid.UUID, broker_oid: str, client_oid: str | None) -> Order | None:
    """Find the local Order row this update belongs to.

    Prefer broker_order_id (always set after our submit() returns). Fall back
    to client_order_id — which we stamp with our local Order.id at submit
    time — so updates that race ahead of our submit() response still match.
    """
    if broker_oid:
        order = db.execute(
            select(Order).where(
                Order.broker_account_id == account_id,
                Order.broker_order_id == broker_oid,
            )
        ).scalar_one_or_none()
        if order is not None:
            return order
    if client_oid:
        try:
            local_id = uuid.UUID(client_oid)
        except (ValueError, TypeError):
            return None
        order = db.get(Order, local_id)
        if order is not None and order.broker_account_id == account_id:
            # First update for an order whose broker_order_id wasn't persisted
            # yet — backfill it.
            if not order.broker_order_id and broker_oid:
                order.broker_order_id = broker_oid
            return order
    return None


def _build_event(order: Order) -> dict[str, Any]:
    """Same payload shape as copy_engine._order_event so the frontend handler
    is identical to the one already wired for `order.placed` / `order.cancelled`.
    """
    return {
        "type": "order.updated",
        "order": {
            "id": str(order.id),
            "parent_order_id": str(order.parent_order_id) if order.parent_order_id else None,
            "broker_account_id": str(order.broker_account_id),
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_quantity or 0),
            "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            "status": order.status.value,
            "broker_order_id": order.broker_order_id,
            "instrument_type": order.instrument_type.value,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "reject_reason": order.reject_reason,
        },
    }


def _alpaca_side_to_ours(side: Any) -> OrderSide:
    """Map Alpaca's order.side string ("buy" / "sell") to our enum."""
    s = str(side or "").lower()
    return OrderSide.SELL if s == "sell" else OrderSide.BUY


def _alpaca_type_to_ours(order_type: Any) -> OrderType:
    """Map Alpaca's order_type field to our enum. Falls back to MARKET so
    we never crash on a new Alpaca order type we don't recognize."""
    t = str(order_type or "").lower()
    if t in ("limit",):
        return OrderType.LIMIT
    if t in ("stop",):
        return OrderType.STOP
    if t in ("stop_limit", "stop limit"):
        return OrderType.STOP_LIMIT
    return OrderType.MARKET


def _maybe_handle_external_order(
    db,
    account_id: uuid.UUID,
    raw_order: Any,
    broker_oid: str,
    event_name: str,
) -> Order | None:
    """If the trade-update is for an order placed OUTSIDE our platform
    (trader used Alpaca's own UI, mobile app, etc.) AND the account
    owner is a trader who's opted in to mirror_external_trades, create
    a local Order row representing the trade and dispatch fanout.

    Returns the created Order (so the caller can continue applying
    status / fill updates to it as more events arrive), or None when
    we shouldn't act on the message.

    Gating logic (any miss returns None):
      - Account must exist and belong to a trader
      - Trader must have TraderSettings.mirror_external_trades = True
      - Event must be one of: "new", "accepted" (we mirror at order
        acceptance — fastest fanout. Later fill events follow the
        normal in-place update path via broker_order_id lookup)
      - We only create on first sighting; subsequent events for the
        same broker_oid find the Order via _resolve_local_order
    """
    if event_name not in ("new", "accepted"):
        return None

    acct = db.get(BrokerAccount, account_id)
    if acct is None:
        return None
    owner = db.get(User, acct.user_id)
    if owner is None or owner.role != UserRole.TRADER:
        return None

    ts = db.get(TraderSettings, owner.id)
    if ts is None or not ts.mirror_external_trades:
        # Trader hasn't opted in — log once for visibility so the audit
        # trail shows the platform saw an external trade and chose not
        # to act. Useful when client asks "why didn't my trade fan out?"
        audit.record(
            db, actor_user_id=owner.id, action="trader.external_trade_ignored",
            entity_type="broker_account", entity_id=acct.id,
            metadata={
                "broker_order_id": broker_oid,
                "reason": "mirror_external_trades=false",
                "symbol": str(getattr(raw_order, "symbol", "") or ""),
            },
        )
        db.commit()
        return None

    if not ts.trading_enabled:
        # Master kill switch is off — surface that as a clear audit so
        # the trader can find it later.
        audit.record(
            db, actor_user_id=owner.id, action="trader.external_trade_ignored",
            entity_type="broker_account", entity_id=acct.id,
            metadata={
                "broker_order_id": broker_oid,
                "reason": "trading_enabled=false",
                "symbol": str(getattr(raw_order, "symbol", "") or ""),
            },
        )
        db.commit()
        return None

    # Extract trade details from the broker's order representation.
    symbol = str(getattr(raw_order, "symbol", "") or "").upper()
    if not symbol:
        return None
    qty = _dec(getattr(raw_order, "qty", None))
    if qty <= 0:
        return None

    # OCC option symbol detection (same heuristic as fills_sync.py).
    instrument = InstrumentType.STOCK
    option_expiry = option_strike = option_right = None
    display_symbol = symbol
    if len(symbol) >= 18 and symbol[-9] in ("C", "P"):
        instrument = InstrumentType.OPTION
        from app.models.order import OptionRight
        try:
            display_symbol = symbol[:-15].strip()
            yymmdd = symbol[-15:-9]
            cp = symbol[-9]
            strike_str = symbol[-8:]
            from datetime import date as _date
            option_expiry = _date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
            option_strike = Decimal(int(strike_str)) / Decimal(1000)
            option_right = OptionRight.CALL if cp == "C" else OptionRight.PUT
        except (ValueError, IndexError):
            # Couldn't parse OCC — fall back to treating it as a stock so
            # we don't drop the trade entirely.
            instrument = InstrumentType.STOCK
            display_symbol = symbol

    order = Order(
        user_id=owner.id,
        broker_account_id=acct.id,
        instrument_type=instrument,
        symbol=display_symbol,
        option_expiry=option_expiry,
        option_strike=option_strike,
        option_right=option_right,
        side=_alpaca_side_to_ours(getattr(raw_order, "side", None)),
        order_type=_alpaca_type_to_ours(getattr(raw_order, "order_type", None)),
        quantity=qty,
        limit_price=_dec_or_none(getattr(raw_order, "limit_price", None)),
        stop_price=_dec_or_none(getattr(raw_order, "stop_price", None)),
        status=OrderStatus.SUBMITTED,
        broker_order_id=broker_oid,
        submitted_at=datetime.now(timezone.utc),
        fanned_out_to_subscribers=True,    # opt-in flag forced us here
    )
    db.add(order)
    db.flush()

    audit.record(
        db, actor_user_id=owner.id, action="trader.external_order_detected",
        entity_type="order", entity_id=order.id,
        metadata={
            "broker_order_id": broker_oid,
            "symbol": display_symbol,
            "side": order.side.value,
            "qty": str(qty),
            "instrument": instrument.value,
            "event": event_name,
        },
    )

    # Dispatch fanout. Prefer Redis Streams when configured (parallel
    # processing across worker pods); fall back to in-process if not.
    targets = enumerate_fanout_targets(db, owner.id)
    if fanout_stream.is_configured():
        count = fanout_stream.publish_targets(order.id, targets)
        audit.record(
            db, actor_user_id=owner.id, action="trader.fanout_dispatched",
            entity_type="order", entity_id=order.id,
            metadata={
                "dispatch": "redis_stream",
                "source": "external",
                "target_count": count,
            },
        )
    else:
        # Defer in-process fanout to AFTER we commit so the trader Order
        # is visible to worker sessions. We don't have a great mechanism
        # for that here (this is a sync function inside the stream
        # handler), so we just commit + call fanout synchronously.
        db.commit()
        from app.services.copy_engine import fanout as _fanout_in_process
        _fanout_in_process(db, order, owner)
    db.commit()

    return order


def _apply_trade_update(account_id: uuid.UUID, data: Any) -> None:
    """Sync DB write. Called from inside an async handler — runs fast enough
    (single-row lookups + at most 1 insert) that blocking the loop is fine
    for normal fill volume. If volumes grow, wrap the call site in
    asyncio.to_thread().
    """
    # alpaca-py's TradeUpdate has `.event` (str) and `.order` (Order) attributes,
    # plus `.price`, `.qty`, `.execution_id`, `.timestamp` on fill events.
    raw_order = getattr(data, "order", None)
    if raw_order is None:
        return

    event_name = str(getattr(data, "event", "") or "").lower()
    broker_oid = str(getattr(raw_order, "id", "") or "")
    client_oid = str(getattr(raw_order, "client_order_id", "") or "") or None

    with SessionLocal() as db:
        order = _resolve_local_order(db, account_id, broker_oid, client_oid)
        if order is None:
            # Externally-placed trade (the trader placed it via Alpaca's
            # own UI, the Alpaca mobile app, an algo running outside our
            # platform, etc.). If the account owner is a trader who's
            # opted in to mirror_external_trades, materialize a local
            # Order and fan out. Otherwise log + return.
            order = _maybe_handle_external_order(db, account_id, raw_order, broker_oid, event_name)
            if order is None:
                log.debug(
                    "alpaca_stream: ignoring unknown order acct=%s broker_oid=%s event=%s",
                    account_id, broker_oid, event_name,
                )
                return

        # Update status / filled qty / VWAP from the broker's view of the order.
        new_status = _STATUS_IN.get(getattr(raw_order, "status", None), order.status)
        if new_status != order.status:
            order.status = new_status

        broker_filled_qty = _dec_or_none(getattr(raw_order, "filled_qty", None))
        if broker_filled_qty is not None and broker_filled_qty != _dec(order.filled_quantity):
            order.filled_quantity = broker_filled_qty

        broker_avg = _dec_or_none(getattr(raw_order, "filled_avg_price", None))
        if broker_avg is not None and broker_avg != _dec(order.filled_avg_price):
            order.filled_avg_price = broker_avg

        if order.status in _TERMINAL_STATUSES and order.closed_at is None:
            order.closed_at = datetime.now(timezone.utc)

        if order.status == OrderStatus.REJECTED:
            reason = getattr(raw_order, "reject_reason", None) or getattr(data, "reject_reason", None)
            if reason and not order.reject_reason:
                order.reject_reason = str(reason)[:480]

        # For execution events, insert a Fill row (dedup by execution_id).
        if event_name in ("fill", "partial_fill"):
            exec_id = (
                str(getattr(data, "execution_id", "") or "")
                or f"{broker_oid}:{getattr(data, 'timestamp', '')}"
            )
            already = db.execute(
                select(Fill.id).join(Order, Fill.order_id == Order.id).where(
                    Order.broker_account_id == account_id,
                    Fill.broker_fill_id == exec_id,
                ).limit(1)
            ).first()
            if not already:
                qty = _dec(getattr(data, "qty", None))
                price = _dec(getattr(data, "price", None))
                if qty > 0 and price > 0:
                    fill_ts = getattr(data, "timestamp", None) or datetime.now(timezone.utc)
                    if isinstance(fill_ts, str):
                        try:
                            fill_ts = datetime.fromisoformat(fill_ts.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            fill_ts = datetime.now(timezone.utc)
                    db.add(Fill(
                        order_id=order.id,
                        quantity=qty,
                        price=price,
                        fee=Decimal(0),
                        filled_at=fill_ts,
                        broker_fill_id=exec_id,
                    ))

        db.commit()
        db.refresh(order)

        audit.record(
            db, actor_user_id=order.user_id, action="order.stream_update",
            entity_type="order", entity_id=order.id,
            metadata={
                "event": event_name,
                "status": order.status.value,
                "filled_quantity": str(order.filled_quantity),
                "broker_order_id": broker_oid,
            },
        )
        db.commit()

        events.publish(order.user_id, _build_event(order))


# Wrap the SDK import at usage time so missing/optional deps don't crash app boot.
def _make_stream(api_key: str, api_secret: str, paper: bool):
    from alpaca.trading.stream import TradingStream  # local import — lazy
    return TradingStream(api_key, api_secret, paper=paper)


async def _run_stream_for_account(account_id: uuid.UUID) -> None:
    """Long-lived task: pull credentials, open the WebSocket, subscribe.
    Reconnects with exponential backoff on hard failure. Cancellation-safe.
    """
    backoff = _RECONNECT_MIN_SECONDS
    while True:
        try:
            with SessionLocal() as db:
                acct = db.get(BrokerAccount, account_id)
                if acct is None or acct.broker != BrokerName.ALPACA:
                    log.info("alpaca_stream: account %s no longer Alpaca-connected, stopping", account_id)
                    return
                creds = decrypt_json(acct.encrypted_credentials)

            stream = _make_stream(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                paper=bool(creds.get("paper", True)),
            )

            # The handler must be async; we keep the DB work sync inside it.
            async def _handler(data: Any) -> None:
                try:
                    _apply_trade_update(account_id, data)
                except Exception:  # noqa: BLE001
                    log.exception("alpaca_stream: handler error acct=%s", account_id)

            stream.subscribe_trade_updates(_handler)
            log.info("alpaca_stream: connected acct=%s paper=%s", account_id, creds.get("paper", True))

            # _run_forever is the awaitable inner loop; .run() blocks via asyncio.run()
            # which we can't call from inside an existing loop. Use the SDK's
            # internal coroutine when available, otherwise fall back to to_thread.
            if hasattr(stream, "_run_forever"):
                await stream._run_forever()
            else:
                await asyncio.to_thread(stream.run)

            # If _run_forever returns cleanly, treat as graceful disconnect and reconnect.
            log.info("alpaca_stream: disconnected acct=%s, reconnecting", account_id)
            backoff = _RECONNECT_MIN_SECONDS

        except asyncio.CancelledError:
            log.info("alpaca_stream: cancelled acct=%s", account_id)
            raise
        except Exception:  # noqa: BLE001
            log.exception("alpaca_stream: error acct=%s, reconnecting in %ss", account_id, backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)


def start_stream(account_id: uuid.UUID) -> None:
    """Launch the stream task for one account. No-op if already running."""
    if account_id in _streams and not _streams[account_id].done():
        return
    task = asyncio.create_task(
        _run_stream_for_account(account_id),
        name=f"alpaca_stream:{account_id}",
    )
    _streams[account_id] = task


def stop_stream(account_id: uuid.UUID) -> None:
    """Cancel the stream task for one account. Safe to call if not running."""
    task = _streams.pop(account_id, None)
    if task and not task.done():
        task.cancel()


async def start_all_streams() -> None:
    """Called at app startup. Queries all currently-connected Alpaca accounts
    and starts a stream for each. Errors per-account are logged, not raised —
    one bad account shouldn't block the others."""
    with SessionLocal() as db:
        accts = list(db.execute(
            select(BrokerAccount.id).where(
                BrokerAccount.broker == BrokerName.ALPACA,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())
    for account_id in accts:
        try:
            start_stream(account_id)
        except Exception:  # noqa: BLE001
            log.exception("alpaca_stream: failed to start stream for acct=%s", account_id)
    log.info("alpaca_stream: started %d stream(s)", len(_streams))


async def stop_all_streams() -> None:
    """Called at app shutdown. Cancels every running stream task and waits
    briefly for graceful exit."""
    tasks = list(_streams.values())
    _streams.clear()
    for t in tasks:
        if not t.done():
            t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("alpaca_stream: stopped %d stream(s)", len(tasks))
