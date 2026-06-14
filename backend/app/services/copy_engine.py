"""Copy-trade fan-out — orchestration over a per-target unit of work.

Two surfaces:

1. ``fanout(db, trader_order, trader)`` — in-process orchestrator. Enumerates
   every subscriber × broker_account that should receive a mirror of this
   trader's order and dispatches them through a ThreadPoolExecutor.

2. ``process_one_fanout(trader_order_id, target)`` — atomic unit of work
   for ONE (subscriber, broker_account). Opens its own DB session so it's
   thread-safe AND can be called from a Redis-Streams worker process in a
   separate container. Runs every gate (master pause, subscriber
   copy_enabled, daily-loss kill switch, zero-quantity skip) so the
   decision is made on fresh state at execution time — important for
   queue-replay semantics where the dispatch happened seconds or minutes
   before the worker picks the message up.

The quantity-scaling rule
-------------------------
  - If broker supports fractional shares: keep raw multiplied quantity
    (truncated to 6dp).
  - Otherwise: floor to whole shares. If result is 0, skip + audit.

A failure on one target must NOT block the others — handled by per-task
exception capture in the orchestrator + a try/except shell inside
process_one_fanout that swallows broker errors into a FanoutResult.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderStatus
from app.models.pending_copy import PendingCopy, PendingCopyStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services import audit, events
from app.services.crypto import decrypt_json
from app.services.order_retry import RecoverableOrderError, place_order_with_recovery
from app.services.pnl import today_realized_pnl

log = logging.getLogger(__name__)

MAX_PARALLEL = 32

# Grace window for listener-detected orders: an order whose broker-side
# placement time is older than (broker_account.created_at - this) is treated
# as pre-connection history and NOT mirrored. See order_predates_connection.
FANOUT_HISTORICAL_GRACE_S = 120


def order_predates_connection(
    broker_account: "BrokerAccount | None",
    order_placed_at: "datetime | None",
) -> bool:
    """True if a listener-detected order was placed before we began watching
    the trader's broker (so it's history and must NOT be mirrored). Compares
    the order's broker-side placement time against broker_account.created_at
    minus a grace window. Fail-open (False → allow) when a timestamp is
    missing — dropping a real just-placed trade is worse than occasionally
    mirroring one borderline historical order."""
    if order_placed_at is None or broker_account is None or broker_account.created_at is None:
        return False
    placed = order_placed_at if order_placed_at.tzinfo else order_placed_at.replace(tzinfo=timezone.utc)
    created = broker_account.created_at
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return placed < created - timedelta(seconds=FANOUT_HISTORICAL_GRACE_S)


@dataclass
class FanoutResult:
    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID
    order_id: uuid.UUID | None
    # "submitted" | "skipped_zero_qty" | "skipped_no_broker"
    # "skipped_copy_disabled" | "skipped_daily_loss_limit"
    # "skipped_master_paused" | "skipped_not_following"
    # "skipped_trader_order_missing" | "error"
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class FanoutTarget:
    """One unit of fanout work — mirror a trader order to one subscriber on
    one specific broker account. Serializable so a Redis Stream worker can
    reconstruct it from the message payload."""

    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID


# ─── Helpers ────────────────────────────────────────────────────────────────

def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    raw = trader_qty * multiplier
    if fractional:
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw.to_integral_value(rounding=ROUND_DOWN)


def trader_can_trade(db: Session, trader: User) -> bool:
    if trader.role != UserRole.TRADER:
        return False
    settings = db.get(TraderSettings, trader.id)
    return bool(settings and settings.trading_enabled)


def enumerate_fanout_targets(db: Session, trader_id: uuid.UUID) -> list[FanoutTarget]:
    """Return every (subscriber, broker_account) pair that should receive
    a mirror of an order from this trader, AT THIS MOMENT.

    - Honours trader master pause (returns [] when paused).
    - Honours subscriber copy_enabled.
    - Excludes subscribers with no broker account (they'll be audit-logged
      by process_one_fanout when called against an empty target list, but
      we don't manufacture phantom targets here).

    Does NOT check daily-loss limit — that check is done at processing time
    so the freshest realized-P&L number is used. Same for zero-qty skip.
    """
    ts = db.get(TraderSettings, trader_id)
    if ts is not None and ts.copy_paused:
        return []

    sub_rows = (
        db.execute(
            select(SubscriberSettings).where(
                SubscriberSettings.following_trader_id == trader_id,
                SubscriberSettings.copy_enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )

    targets: list[FanoutTarget] = []
    for sub_settings in sub_rows:
        sub_accounts = (
            db.execute(
                select(BrokerAccount.id).where(BrokerAccount.user_id == sub_settings.user_id)
            )
            .scalars()
            .all()
        )
        if not sub_accounts:
            # The subscriber is following + has copy enabled but has no broker
            # connected. Surface as a result via process_one_fanout when its
            # session sees the same empty state. We emit one synthetic target
            # with a sentinel broker_account_id so the caller still gets a
            # FanoutResult row for visibility.
            targets.append(FanoutTarget(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=uuid.UUID(int=0),
            ))
            continue
        for acct_id in sub_accounts:
            targets.append(FanoutTarget(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=acct_id,
            ))
    return targets


# ─── The atomic unit of work ────────────────────────────────────────────────

def process_one_fanout(
    trader_order_id: uuid.UUID,
    target: FanoutTarget,
) -> FanoutResult:
    """Mirror one trader order to one subscriber's one broker account.

    Self-contained: opens its own DB session, commits before returning, and
    captures all broker errors into a FanoutResult so callers (whether
    in-process ThreadPoolExecutor or a Redis Streams worker) never have to
    catch exceptions from it.

    Gate order (matches the audit story):
      1. trader_order exists                  → skipped_trader_order_missing
      2. subscriber has settings              → skipped_copy_disabled
      3. subscriber.copy_enabled is True      → skipped_copy_disabled
      4. subscriber still follows the trader  → skipped_not_following
      5. trader.copy_paused is False          → skipped_master_paused
      6. daily_loss_limit not yet breached    → skipped_daily_loss_limit
         (and auto-flips copy_enabled to False as a side effect)
      7. subscriber has the broker_account    → skipped_no_broker
      8. scaled quantity > 0                  → skipped_zero_qty

    Then: place order via broker (with retry/recovery), update child Order
    row, audit + SSE publish.
    """
    with SessionLocal() as db:
        try:
            return _process_one_fanout_inner(db, trader_order_id, target)
        except Exception as exc:  # noqa: BLE001
            # Last-ditch safety net — should be unreachable because the inner
            # function captures broker errors itself. If it fires, the worker
            # still gets a FanoutResult instead of an exception propagating.
            log.exception(
                "copy_engine: unhandled exception in process_one_fanout "
                "trader_order=%s subscriber=%s broker_account=%s",
                trader_order_id, target.subscriber_user_id, target.broker_account_id,
            )
            db.rollback()
            return FanoutResult(
                subscriber_user_id=target.subscriber_user_id,
                broker_account_id=target.broker_account_id,
                order_id=None,
                status="error",
                detail=str(exc)[:200],
            )


def _process_one_fanout_inner(
    db: Session,
    trader_order_id: uuid.UUID,
    target: FanoutTarget,
) -> FanoutResult:
    """Real body of process_one_fanout. Split out so the outer wrapper can
    do the exception-safety + session-lifecycle bookkeeping."""

    trader_order = db.get(Order, trader_order_id)
    if trader_order is None:
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_trader_order_missing",
        )

    sub_settings = db.get(SubscriberSettings, target.subscriber_user_id)
    if sub_settings is None or not sub_settings.copy_enabled:
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_copy_disabled",
        )

    # Subscriber may have unfollowed between dispatch and processing.
    if sub_settings.following_trader_id != trader_order.user_id:
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_not_following",
        )

    # Trader's master pause flag (may have flipped between dispatch and now).
    ts = db.get(TraderSettings, trader_order.user_id)
    if ts is not None and ts.copy_paused:
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_master_paused",
        )

    # Daily-loss kill switch. We check BEFORE placing so we never blow past
    # the limit by one trade. On trip: flip copy_enabled off + SSE event so
    # the subscriber's UI reflects the auto-pause immediately.
    if sub_settings.daily_loss_limit is not None:
        todays_pnl = today_realized_pnl(db, sub_settings.user_id)
        if todays_pnl <= -sub_settings.daily_loss_limit:
            sub_settings.copy_enabled = False
            audit.record(
                db,
                actor_user_id=sub_settings.user_id,
                action="copy.auto_paused_daily_loss",
                entity_type="subscriber_settings",
                entity_id=sub_settings.user_id,
                metadata={
                    "daily_loss_limit": str(sub_settings.daily_loss_limit),
                    "todays_realized_pnl": str(todays_pnl),
                    "trigger_order_id": str(trader_order.id),
                },
            )
            events.publish(sub_settings.user_id, {
                "type": "copy.auto_paused",
                "reason": "daily_loss_limit",
                "daily_loss_limit": str(sub_settings.daily_loss_limit),
                "todays_realized_pnl": str(todays_pnl),
            })
            db.commit()
            return FanoutResult(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=target.broker_account_id,
                order_id=None,
                status="skipped_daily_loss_limit",
            )

    # Sentinel from enumerate_fanout_targets when the subscriber has no
    # broker — surface cleanly without trying to load a UUID(int=0) row.
    if target.broker_account_id == uuid.UUID(int=0):
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_no_broker",
        )

    acct = db.get(BrokerAccount, target.broker_account_id)
    if acct is None or acct.user_id != target.subscriber_user_id:
        return FanoutResult(
            subscriber_user_id=target.subscriber_user_id,
            broker_account_id=target.broker_account_id,
            order_id=None,
            status="skipped_no_broker",
        )

    scaled = _scale_quantity(
        trader_order.quantity, sub_settings.multiplier, acct.supports_fractional
    )
    if scaled <= 0:
        audit.record(
            db,
            actor_user_id=sub_settings.user_id,
            action="copy.skipped_zero_qty",
            entity_type="order",
            entity_id=trader_order.id,
            metadata={
                "trader_qty": str(trader_order.quantity),
                "multiplier": str(sub_settings.multiplier),
                "broker_account_id": str(acct.id),
            },
        )
        db.commit()
        return FanoutResult(
            subscriber_user_id=sub_settings.user_id,
            broker_account_id=acct.id,
            order_id=None,
            status="skipped_zero_qty",
        )

    # ── Create child Order row ────────────────────────────────────────────
    child = Order(
        user_id=sub_settings.user_id,
        broker_account_id=acct.id,
        parent_order_id=trader_order.id,
        instrument_type=trader_order.instrument_type,
        symbol=trader_order.symbol,
        option_expiry=trader_order.option_expiry,
        option_strike=trader_order.option_strike,
        option_right=trader_order.option_right,
        side=trader_order.side,
        order_type=trader_order.order_type,
        quantity=scaled,
        limit_price=trader_order.limit_price,
        stop_price=trader_order.stop_price,
        status=OrderStatus.PENDING,
        # Propagate the open/close intent from the parent so the retry
        # scheduler picks the matching interval from subscriber's settings.
        is_closing=trader_order.is_closing,
    )
    db.add(child)
    db.flush()

    # ── Place at broker (with retry/recovery) ─────────────────────────────
    try:
        sub_creds = decrypt_json(acct.encrypted_credentials)
        sub_adapter = adapter_for(acct, sub_creds)
    except Exception as exc:  # noqa: BLE001
        child.status = OrderStatus.REJECTED
        child.reject_reason = f"credentials_error: {exc}"[:480]
        child.closed_at = datetime.now(timezone.utc)
        audit.record(
            db,
            actor_user_id=sub_settings.user_id,
            action="copy.error",
            entity_type="order",
            entity_id=child.id,
            metadata={"parent_order_id": str(trader_order.id), "error": str(exc)[:300]},
        )
        db.commit()
        events.publish(sub_settings.user_id, _order_event("order.copy_failed", child))
        return FanoutResult(
            subscriber_user_id=sub_settings.user_id,
            broker_account_id=acct.id,
            order_id=child.id,
            status="error",
            detail=str(exc)[:200],
        )

    request = BrokerOrderRequest(
        instrument_type=child.instrument_type,
        symbol=child.symbol,
        side=child.side,
        order_type=child.order_type,
        quantity=child.quantity,
        limit_price=child.limit_price,
        stop_price=child.stop_price,
        option_expiry=child.option_expiry,
        option_strike=child.option_strike,
        option_right=child.option_right,
        client_order_id=str(child.id),
    )

    try:
        resp = place_order_with_recovery(sub_adapter, request)
    except RecoverableOrderError as rec:
        child.status = OrderStatus.REJECTED
        child.reject_reason = rec.friendly_message[:480]
        child.closed_at = datetime.now(timezone.utc)
        audit.record(
            db,
            actor_user_id=sub_settings.user_id,
            action="copy.error",
            entity_type="order",
            entity_id=child.id,
            metadata={
                "parent_order_id": str(trader_order.id),
                "friendly": rec.friendly_message,
                "raw_error": str(rec.original)[:300],
                "classification": "user_fixable",
            },
        )
        db.commit()
        events.publish(sub_settings.user_id, _order_event("order.copy_failed", child))
        return FanoutResult(
            subscriber_user_id=sub_settings.user_id,
            broker_account_id=acct.id,
            order_id=child.id,
            status="error",
            detail=rec.friendly_message[:200],
        )
    except Exception as exc:  # noqa: BLE001
        # Classify the error. If it's a transient broker-disconnect
        # (5xx / timeout / connection reset / 429) AND the subscriber
        # has opted into retry for this open/close direction, schedule
        # a single retry instead of rejecting immediately. The
        # retry_scheduler picks this up at retry_at and tries again.
        from app.services.order_retry import classify_error
        from app.models.settings import RetryInterval

        cls = classify_error(exc)
        retry_seconds: int | None = None
        if cls.transient and not child.retry_attempted:
            interval: RetryInterval = (
                sub_settings.retry_interval_close if trader_order.is_closing
                else sub_settings.retry_interval_open
            )
            retry_seconds = interval.seconds()

        if retry_seconds is not None:
            # Schedule the retry. 0-3s jitter spreads retries across
            # subscribers so a broker outage doesn't trigger a 100-call
            # thundering herd at the same second.
            import random
            jitter = random.uniform(0, 3)
            child.status = OrderStatus.RETRY_PENDING
            child.retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_seconds + jitter)
            child.reject_reason = f"transient: {exc}"[:480]
            audit.record(
                db,
                actor_user_id=sub_settings.user_id,
                action="copy.retry_scheduled",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "error": str(exc)[:300],
                    "retry_at": child.retry_at.isoformat(),
                    "interval_seconds": retry_seconds,
                    "is_closing": child.is_closing,
                },
            )
            db.commit()
            events.publish(sub_settings.user_id, _order_event("order.copy_failed", child))
            return FanoutResult(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=acct.id,
                order_id=child.id,
                status="retry_scheduled",
                detail=f"retry in {retry_seconds}s",
            )

        # No retry — either non-transient OR subscriber didn't opt in.
        # Existing behaviour: mark REJECTED, audit, publish SSE.
        child.status = OrderStatus.REJECTED
        child.reject_reason = str(exc)[:480]
        child.closed_at = datetime.now(timezone.utc)
        audit.record(
            db,
            actor_user_id=sub_settings.user_id,
            action="copy.error",
            entity_type="order",
            entity_id=child.id,
            metadata={"parent_order_id": str(trader_order.id), "error": str(exc)[:480]},
        )
        db.commit()
        events.publish(sub_settings.user_id, _order_event("order.copy_failed", child))
        return FanoutResult(
            subscriber_user_id=sub_settings.user_id,
            broker_account_id=acct.id,
            order_id=child.id,
            status="error",
            detail=str(exc)[:200],
        )

    # Happy path: broker accepted.
    child.status = resp.status
    child.broker_order_id = resp.broker_order_id
    child.submitted_at = resp.submitted_at
    child.filled_quantity = resp.filled_quantity
    child.filled_avg_price = resp.filled_avg_price
    audit.record(
        db,
        actor_user_id=sub_settings.user_id,
        action="copy.submitted",
        entity_type="order",
        entity_id=child.id,
        metadata={
            "parent_order_id": str(trader_order.id),
            "broker_order_id": resp.broker_order_id,
            "scaled_qty": str(child.quantity),
        },
    )
    db.commit()
    events.publish(sub_settings.user_id, _order_event("order.copy_submitted", child))
    return FanoutResult(
        subscriber_user_id=sub_settings.user_id,
        broker_account_id=acct.id,
        order_id=child.id,
        status="submitted",
    )


# ─── In-process orchestrator (the existing public surface) ──────────────────

def fanout(db: Session, trader_order: Order, trader: User) -> list[FanoutResult]:
    """Mirror ``trader_order`` to every subscriber following ``trader``.

    Default in-process implementation: builds the target list, then runs
    ``process_one_fanout`` for each target in a ThreadPoolExecutor. Each
    worker thread opens its own DB session so this is connection-pool-bound
    rather than session-thread-bound.

    Equivalent behaviour to what we had before this refactor — same gates,
    same audit log entries, same SSE events, same overall latency profile.
    """
    targets = enumerate_fanout_targets(db, trader.id)
    if not targets:
        return []

    # Commit so each worker session sees the trader_order + any state mutated
    # during enumeration (none today, but future-proof).
    db.commit()

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(targets))) as pool:
        return list(pool.map(
            lambda t: process_one_fanout(trader_order.id, t),
            targets,
        ))


# ─── Queue-based fanout (demo) ──────────────────────────────────────────────

def queue_fanout(db: Session, trader_order: Order, trader: User) -> int:
    """Demo replacement for ``fanout``: enumerates eligible subscribers
    from the in-memory cache and batch-inserts one ``pending_copies`` row
    per subscriber-broker pair, then returns. NO eligibility checks, NO
    broker calls in this code path — those happen in the worker pool
    (services.subscriber_worker).

    Returns the number of rows queued. Target latency: <10ms for 100 subs.
    """
    from app.services import memory_cache

    if trader.role != UserRole.TRADER:
        return 0
    ts = db.get(TraderSettings, trader.id)
    if ts is None or not ts.trading_enabled or ts.copy_paused:
        return 0

    subs = memory_cache.subscribers_for_trader(trader.id)
    if not subs:
        return 0

    rows: list[dict[str, Any]] = []
    for entry in subs:
        # We still enqueue subscribers with no broker so the worker can
        # surface a "skipped_no_broker" row in the dashboard. Cheap.
        if not entry.copy_enabled:
            continue
        if entry.following_trader_id != trader.id:
            continue
        rows.append({
            "id": uuid.uuid4(),
            "parent_order_id": trader_order.id,
            "subscriber_user_id": entry.user_id,
            "status": PendingCopyStatus.QUEUED.value,
        })

    if not rows:
        return 0

    # Single batch insert. Postgres can chew through 100 rows in ~5ms.
    db.execute(PendingCopy.__table__.insert(), rows)
    # Wake the worker pool immediately via LISTEN/NOTIFY instead of letting them
    # discover the rows on their next poll tick. Delivered on COMMIT, so the
    # workers see the committed rows the instant they wake. (Workers also keep a
    # short fallback poll, so a missed NOTIFY only costs a little latency, never
    # correctness.)
    from sqlalchemy import text as _text
    db.execute(_text("NOTIFY pending_copies"))
    db.commit()
    return len(rows)


def dispatch_detected_order(db: Session, trader_order: Order, trader: User) -> dict[str, Any]:
    """Single dispatch entrypoint for a freshly-detected trader order.

    App 2 default = the queue-based fast path (``queue_fanout``): the
    detection handler returns in ~8ms and the async worker pool places the
    mirror orders. Falls back to Redis Streams or the in-process serial
    ``fanout`` when ``use_queue_fanout`` is disabled (legacy / comparison).

    Req #3: if the trader has mirror_only_filled=True, non-filled orders are
    silently skipped (not dispatched). The listener calls this on every status
    update — we'll see the FILLED event eventually and dispatch then.

    Returns a small metadata dict the caller folds into its audit record.
    """
    from app.config import get_settings

    # Req #3 — mirror-only-filled gate
    ts = db.get(TraderSettings, trader.id)
    if ts is not None and ts.mirror_only_filled:
        if trader_order.status != OrderStatus.FILLED:
            return {"dispatch": "skipped_not_filled", "status": trader_order.status.value}

    if getattr(get_settings(), "use_queue_fanout", True):
        # Commit so the trader Order row is visible to worker sessions before
        # we enqueue pending_copies rows that FK-reference it.
        db.commit()
        queued = queue_fanout(db, trader_order, trader)
        return {"dispatch": "queue", "queued": queued}

    # ── Legacy dispatch paths (only when use_queue_fanout=False) ──────────
    from app.services import fanout_stream
    targets = enumerate_fanout_targets(db, trader.id)
    if fanout_stream.is_configured():
        count = fanout_stream.publish_targets(trader_order.id, targets)
        db.commit()
        return {"dispatch": "redis_stream", "target_count": count}
    db.commit()
    fanout(db, trader_order, trader)
    return {"dispatch": "in_process", "target_count": len(targets)}


def _order_event(event_type: str, order: Order) -> dict[str, Any]:
    """Compact payload — frontend can use it directly to prepend a row."""
    return {
        "type": event_type,
        "order": {
            "id": str(order.id),
            "parent_order_id": str(order.parent_order_id) if order.parent_order_id else None,
            "broker_account_id": str(order.broker_account_id) if order.broker_account_id else None,
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
