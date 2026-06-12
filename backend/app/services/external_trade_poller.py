"""REST-polling fallback for external-trade detection.

Why this exists
---------------
The WebSocket-based detection in alpaca_stream.py relies on alpaca-py's
TradingStream pushing trade_update events to a registered handler. In
alpaca-py 0.33, that stream silently fails to deliver events in our
background-thread context (no exception, no error log — just nothing
arrives). We may upgrade the SDK later, but the platform can't wait.

This module is the bulletproof alternative: every POLL_INTERVAL_SEC,
hit Alpaca's REST `GET /v2/orders` for each trader account that has
opted into mirror_external_trades. Anything not already in our DB is
an externally-placed order — we create the local Order row and
dispatch fanout via the same Redis Streams path the WebSocket would
have used.

Trade-off vs. WebSocket
-----------------------
  Latency:     ~1-2s (vs ~100ms for the WebSocket when it worked)
  Reliability: 100% — REST always works
  API cost:    1 request per trader-account per POLL_INTERVAL_SEC.
               At 2-sec interval with 5 trader accounts: 150 req/min.
               Alpaca's rate limit is 200 req/min per account; we're
               well under.

Coexistence with the WebSocket
------------------------------
Runs ALONGSIDE alpaca_stream.py. If the WebSocket ever starts
delivering events, no conflict: both paths dedupe by broker_order_id
before inserting, so whichever sees the trade first wins; the other
sees the row already exists and silently skips. Eventual consistency,
no double-fanout.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers.alpaca import AlpacaAdapter
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType,
    Order,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.models.settings import TraderSettings
from app.models.user import User, UserRole
from app.services import audit, fanout_stream
from app.services.copy_engine import enumerate_fanout_targets
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 2
ORDERS_PER_POLL = 20

_LAST_HEARTBEAT: dict[str, Any] = {"at": None, "accounts": 0}


def heartbeat_status() -> dict[str, Any]:
    """Exposed via /api/health so operators can confirm external-trade
    detection is actually polling (and how many accounts it watches)."""
    last = _LAST_HEARTBEAT.get("at")
    if last is None:
        return {"running": False, "last_run_at": None}
    delta = (datetime.now(timezone.utc) - last).total_seconds()
    return {
        "running": True,
        "last_run_at": last.isoformat(),
        "seconds_since": round(delta, 1),
        "accounts_polled": _LAST_HEARTBEAT.get("accounts", 0),
        "healthy": delta < POLL_INTERVAL_SEC * 5,
    }


# ── helpers (parallel to alpaca_stream.py) ──────────────────────────────────

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


def _alpaca_side_to_ours(side: Any) -> OrderSide:
    s = str(side or "").lower()
    return OrderSide.SELL if s == "sell" else OrderSide.BUY


def _alpaca_type_to_ours(order_type: Any) -> OrderType:
    t = str(order_type or "").lower()
    if t == "limit":
        return OrderType.LIMIT
    if t == "stop":
        return OrderType.STOP
    if t in ("stop_limit", "stop limit"):
        return OrderType.STOP_LIMIT
    return OrderType.MARKET


# ── core: turn one externally-placed Alpaca order into a local Order + fanout

def _handle_external_order(
    db,
    acct: BrokerAccount,
    owner: User,
    raw_order: Any,
    broker_oid: str,
    do_fanout: bool,
) -> Order | None:
    """Mirror of alpaca_stream._maybe_handle_external_order — but called
    from a sync poll loop rather than an async WebSocket handler. Same
    gate checks already done by the caller (mirror_external_trades=True,
    trading_enabled=True, not in DB).

    Stores Alpaca's reported submitted_at (when the broker accepted the
    order, not our detection time) so the trader's Fanout Performance UI
    can compute "detection lag" = our_row.created_at - submitted_at.
    """
    symbol = str(getattr(raw_order, "symbol", "") or "").upper()
    if not symbol:
        return None
    qty = _dec(getattr(raw_order, "qty", None))
    if qty <= 0:
        return None

    # Parse Alpaca's submitted_at — the authoritative "when broker
    # accepted" timestamp. Falls back to our clock if parsing fails.
    alpaca_submitted = getattr(raw_order, "submitted_at", None)
    submitted_dt: datetime | None = None
    if alpaca_submitted is not None:
        try:
            if isinstance(alpaca_submitted, datetime):
                submitted_dt = alpaca_submitted
            else:
                submitted_dt = datetime.fromisoformat(
                    str(alpaca_submitted).replace("Z", "+00:00"),
                )
            if submitted_dt.tzinfo is None:
                submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            submitted_dt = None
    if submitted_dt is None:
        submitted_dt = datetime.now(timezone.utc)

    # OCC option symbol heuristic (same as the WebSocket path).
    instrument = InstrumentType.STOCK
    option_expiry = option_strike = option_right = None
    display_symbol = symbol
    if len(symbol) >= 18 and symbol[-9] in ("C", "P"):
        instrument = InstrumentType.OPTION
        try:
            from datetime import date as _date
            display_symbol = symbol[:-15].strip()
            yymmdd = symbol[-15:-9]
            cp = symbol[-9]
            strike_str = symbol[-8:]
            option_expiry = _date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
            option_strike = Decimal(int(strike_str)) / Decimal(1000)
            option_right = OptionRight.CALL if cp == "C" else OptionRight.PUT
        except (ValueError, IndexError):
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
        # Alpaca's timestamp, not ours — lets the UI compute detection lag
        # as (created_at - submitted_at). created_at is auto-set by the
        # TimestampMixin to the moment we INSERT, which is essentially
        # the moment we detected the trade.
        submitted_at=submitted_dt,
        fanned_out_to_subscribers=do_fanout,
    )
    db.add(order)
    db.flush()

    audit.record(
        db, actor_user_id=owner.id, action="trader.external_order_detected",
        entity_type="order", entity_id=order.id,
        metadata={
            "source": "rest_poller",        # distinguish from "websocket" later
            "broker_order_id": broker_oid,
            "symbol": display_symbol,
            "side": order.side.value,
            "qty": str(qty),
            "instrument": instrument.value,
        },
    )

    # Only fan out to subscribers when the trader opted in. Otherwise the
    # order is still recorded (above) so it shows in the trader's own history.
    if do_fanout:
        order.fanout_published_at = datetime.now(timezone.utc)
        from app.services.copy_engine import dispatch_detected_order
        result = dispatch_detected_order(db, order, owner)
        audit.record(
            db, actor_user_id=owner.id, action="trader.fanout_dispatched",
            entity_type="order", entity_id=order.id,
            metadata={"source": "rest_poller_external", **result},
        )
    db.commit()

    return order


# ── per-account poll ────────────────────────────────────────────────────────

def _poll_one_account(account_id: uuid.UUID) -> int:
    """Pull recent orders for one Alpaca account and dispatch fanout for any
    that are new + externally placed. Returns the count dispatched.

    Gate logic mirrors alpaca_stream._maybe_handle_external_order:
      - account exists + is Alpaca
      - owner is a trader
      - trader has mirror_external_trades=True AND trading_enabled=True
    """
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, account_id)
        if acct is None or acct.broker != BrokerName.ALPACA:
            return 0
        owner = db.get(User, acct.user_id)
        if owner is None or owner.role != UserRole.TRADER:
            return 0
        ts = db.get(TraderSettings, owner.id)
        # Record the external order for the trader's own visibility regardless;
        # only FAN OUT to subscribers when they opted in and trading is enabled.
        do_fanout = bool(ts and ts.mirror_external_trades and ts.trading_enabled)
        creds = decrypt_json(acct.encrypted_credentials)

    # Fetch recent orders from Alpaca REST API (outside the DB session
    # to avoid holding a connection during the HTTPS round-trip).
    try:
        adapter = AlpacaAdapter(creds)
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=ORDERS_PER_POLL)
        orders_raw = adapter._c().get_orders(filter=req)
    except Exception:  # noqa: BLE001
        log.exception("external_trade_poller: failed to fetch orders for acct=%s", account_id)
        return 0

    if not orders_raw:
        return 0

    # Re-open session for the inserts/dedup
    processed = 0
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, account_id)
        owner = db.get(User, acct.user_id)
        for raw_order in orders_raw:
            broker_oid = str(getattr(raw_order, "id", "") or "")
            if not broker_oid:
                continue

            # Dedup: already in our DB?
            existing = db.execute(
                select(Order.id).where(
                    Order.broker_account_id == account_id,
                    Order.broker_order_id == broker_oid,
                )
            ).first()
            if existing:
                continue

            # Dedup: client_order_id is a local Order UUID we placed ourselves?
            client_oid = str(getattr(raw_order, "client_order_id", "") or "")
            if client_oid:
                try:
                    local_id = uuid.UUID(client_oid)
                    if db.get(Order, local_id):
                        continue
                except (ValueError, TypeError):
                    pass

            # Genuinely external — handle it
            try:
                order = _handle_external_order(db, acct, owner, raw_order, broker_oid, do_fanout)
                if order is not None:
                    processed += 1

                    # Latency measurement: time from Alpaca accepting the
                    # order (submitted_at on the order object) to us
                    # detecting + dispatching it. This is the headline
                    # number we quote to the client — "trade in Alpaca
                    # → fanout dispatched in X seconds".
                    submitted_at = getattr(raw_order, "submitted_at", None)
                    lag_str = "?"
                    if submitted_at is not None:
                        try:
                            if isinstance(submitted_at, str):
                                submitted_dt = datetime.fromisoformat(
                                    submitted_at.replace("Z", "+00:00"),
                                )
                            else:
                                submitted_dt = submitted_at
                            if submitted_dt.tzinfo is None:
                                submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
                            lag = (datetime.now(timezone.utc) - submitted_dt).total_seconds()
                            lag_str = f"{lag:.2f}s"
                        except Exception:  # noqa: BLE001
                            lag_str = "?"

                    log.info(
                        "external_trade_poller: handled external order acct=%s symbol=%s broker_oid=%s lag=%s",
                        account_id,
                        str(getattr(raw_order, "symbol", "?")),
                        broker_oid,
                        lag_str,
                    )
            except Exception:  # noqa: BLE001
                log.exception("external_trade_poller: handler error acct=%s broker_oid=%s",
                              account_id, broker_oid)
                db.rollback()

    return processed


# ── poll loop ───────────────────────────────────────────────────────────────

def poll_loop(shutdown_check=None) -> None:
    """Long-running loop. Runs as a thread via run_in_executor from main.py
    startup.

    shutdown_check: optional callable returning True when the loop should
    exit. Default None means run forever (until process dies)."""
    log.info("external_trade_poller: starting (interval=%ss)", POLL_INTERVAL_SEC)

    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("external_trade_poller: shutdown requested, exiting")
            return

        loop_start = time.time()
        try:
            # Pick up the set of trader accounts to poll fresh on every iteration
            # so newly-connected brokers join the loop without a restart.
            with SessionLocal() as db:
                # Poll EVERY connected trader Alpaca account. External orders
                # are recorded for the trader's own visibility regardless of
                # settings; fan-out is separately gated on mirror_external_trades
                # inside _poll_one_account. Gate on User.role (NOT a join to
                # TraderSettings): a trader promoted via admin role-change may
                # lack a TraderSettings row, and an inner join would silently
                # drop them from polling so their Alpaca trades never reflect.
                account_ids = list(db.execute(
                    select(BrokerAccount.id).join(
                        User, User.id == BrokerAccount.user_id,
                    ).where(
                        User.role == UserRole.TRADER,
                        BrokerAccount.broker == BrokerName.ALPACA,
                        BrokerAccount.connection_status == "connected",
                    )
                ).scalars())

            _LAST_HEARTBEAT["at"] = datetime.now(timezone.utc)
            _LAST_HEARTBEAT["accounts"] = len(account_ids)

            for account_id in account_ids:
                try:
                    _poll_one_account(account_id)
                except Exception:  # noqa: BLE001
                    log.exception("external_trade_poller: error polling acct=%s", account_id)

        except Exception:  # noqa: BLE001
            log.exception("external_trade_poller: unexpected error in poll loop")

        # Sleep up to POLL_INTERVAL_SEC (accounting for time spent polling).
        elapsed = time.time() - loop_start
        sleep_for = max(0.0, POLL_INTERVAL_SEC - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)
