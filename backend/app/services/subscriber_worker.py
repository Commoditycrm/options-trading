"""Async worker pool that drains the ``pending_copies`` queue.

Architecture
------------
At startup we launch N (default 100) asyncio coroutines. Each loops
forever:

  1. Atomically grab one ``pending_copies`` row in status=QUEUED and
     flip it to PROCESSING (SELECT ... FOR UPDATE SKIP LOCKED).
  2. Read subscriber settings from the in-memory cache (~0ms).
  3. Run eligibility gates (copy_enabled, daily_loss_limit).
  4. Open a fresh DB session, write a child Order row, submit to broker
     in a thread executor (broker SDKs are sync), then commit.
  5. Update the pending_copies row with picked_up_at / submitted_at /
     queue_to_broker_ms / status so the demo dashboard can render the
     timeline bars.

Why asyncio and not threads: with 100 in-flight broker calls, ThreadPool
saturates connections; asyncio with run_in_executor for the broker call
lets us hold 100 concurrent in-flight requests on a small thread pool.

This is intentionally separate from ``services.copy_engine.fanout`` —
the existing serial path is left untouched so the demo dashboard can
run both side by side.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal, engine
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderStatus
from app.models.pending_copy import PendingCopy, PendingCopyStatus
from app.models.settings import SubscriberSettings
from app.services import audit, events, memory_cache
from app.services.copy_engine import _order_event
from app.services.crypto import decrypt_json
from app.services.order_retry import RecoverableOrderError, place_order_with_recovery
from app.services.pnl import get_account_equity, last_trade_pnl, today_realized_pnl
from decimal import Decimal as _D

log = logging.getLogger(__name__)

DEFAULT_WORKER_COUNT = 100

# Postgres NOTIFY channel queue_fanout fires after inserting pending_copies.
# A dedicated LISTEN thread wakes the worker pool the instant rows land, so
# pickup latency is ~0 (was up to the poll interval). This is the main lever
# for keeping *platform* latency under 50ms.
NOTIFY_CHANNEL = "pending_copies"

# Fallback idle wait. When there's nothing to claim a worker waits on the
# wake-up event for at most this long, then re-polls anyway. With NOTIFY this
# path is rarely hit (the event fires first); it's a safety net so a missed
# notification costs a little latency, never correctness. 250ms also keeps idle
# DB load LOWER than the old fixed 100ms poll once notifications carry the load.
POLL_FALLBACK_SEC = 0.25

# Set by the LISTEN thread (cross-thread via loop.call_soon_threadsafe) to wake
# idle workers. Created in start_workers once the event loop exists.
_wakeup: asyncio.Event | None = None
_listener_stop = threading.Event()
_listener_thread: threading.Thread | None = None
# Observability for /api/health — lets us prove the LISTEN wake-up is actually
# running on the box (vs silently falling back to the 250ms poll).
_listener_state: dict[str, Any] = {
    "listening": False,      # LISTEN connection currently established
    "notifies": 0,           # notifications received since boot
    "last_notify_at": None,  # datetime of the most recent notification
    "last_error": None,      # last listener error, for diagnostics
}


def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    raw = trader_qty * multiplier
    if fractional:
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw.to_integral_value(rounding=ROUND_DOWN)


def _claim_one(db: Session) -> PendingCopy | None:
    """Pop one QUEUED row, flip to PROCESSING, return it. Postgres
    SKIP LOCKED gives us exactly-once handoff between workers without
    a separate queue server."""
    row = db.execute(text("""
        UPDATE pending_copies
           SET status = 'processing',
               picked_up_at = now()
         WHERE id = (
             SELECT id FROM pending_copies
              WHERE status = 'queued'
              ORDER BY queued_at
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
         RETURNING id
    """)).first()
    if row is None:
        return None
    db.commit()
    return db.get(PendingCopy, row[0])


def _record_outcome(
    db: Session,
    pc: PendingCopy,
    status: PendingCopyStatus,
    detail: str | None = None,
    broker_ms: int | None = None,
) -> None:
    pc.status = status
    pc.detail = detail
    now = datetime.now(timezone.utc)
    if status in (PendingCopyStatus.SUBMITTED, PendingCopyStatus.FAILED):
        pc.submitted_at = now
        if pc.picked_up_at is not None:
            total_ms = int((now - pc.queued_at).total_seconds() * 1000)
            pc.queue_to_broker_ms = total_ms
            pc.pickup_ms = int((pc.picked_up_at - pc.queued_at).total_seconds() * 1000)
            # platform_ms = everything we control = total minus the broker call.
            # When there was no broker call (skipped/failed before submit), all
            # of the elapsed time is platform time.
            pc.platform_ms = max(0, total_ms - broker_ms) if broker_ms is not None else total_ms
    db.commit()


def _auto_pause(db: Session, pc: PendingCopy, user_id: uuid.UUID,
                reason: str, metadata: dict[str, str]) -> None:
    """A risk limit tripped: flip the subscriber's copy_enabled off, sync the
    in-memory cache so subsequent orders skip them instantly, audit + SSE,
    and mark this pending_copy FAILED with the reason."""
    sub = db.get(SubscriberSettings, user_id)
    if sub is not None:
        sub.copy_enabled = False
    audit.record(
        db, actor_user_id=user_id, action=f"copy.auto_paused_{reason}",
        entity_type="subscriber_settings", entity_id=user_id, metadata=metadata,
    )
    db.commit()
    memory_cache.invalidate_subscriber(user_id)
    events.publish(user_id, {"type": "copy.auto_paused", "reason": reason, **metadata})
    _record_outcome(db, pc, PendingCopyStatus.FAILED, reason)


def _process_one_sync(pc_id: uuid.UUID) -> str:
    """Synchronous body of one worker iteration — runs in a thread
    executor so the broker SDK's blocking HTTP calls don't block the
    event loop. Returns a short outcome tag for logging."""
    with SessionLocal() as db:
        pc = db.get(PendingCopy, pc_id)
        if pc is None:
            return "vanished"

        trader_order = db.get(Order, pc.parent_order_id)
        if trader_order is None:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "parent_order_missing")
            return "parent_missing"

        # Read settings from the in-memory cache — no DB round-trip on a hit.
        # On a miss, fall back to a DB load (self-healing cold/stale cache)
        # instead of wrongly failing the copy as disabled.
        entry = memory_cache.get_subscriber_or_load(pc.subscriber_user_id)
        if entry is None or not entry.copy_enabled:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "copy_disabled")
            return "copy_disabled"
        if entry.following_trader_id != trader_order.user_id:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "not_following")
            return "not_following"

        # ── Risk gates — all run BEFORE placing; any trip auto-pauses copy. ──
        # 1a. Legacy absolute daily-loss kill switch.
        if entry.daily_loss_limit is not None:
            todays_pnl = today_realized_pnl(db, entry.user_id)
            if todays_pnl <= -entry.daily_loss_limit:
                _auto_pause(db, pc, entry.user_id, "daily_loss_limit", {
                    "daily_loss_limit": str(entry.daily_loss_limit),
                    "todays_realized_pnl": str(todays_pnl),
                })
                return "daily_loss_limit"

        # 1b. Daily loss limit as % of account equity.
        if entry.daily_loss_limit_pct is not None:
            equity = get_account_equity(db, entry.user_id)
            if equity:
                dollar_limit = equity * entry.daily_loss_limit_pct / _D(100)
                todays_pnl = today_realized_pnl(db, entry.user_id)
                if todays_pnl <= -dollar_limit:
                    _auto_pause(db, pc, entry.user_id, "daily_loss_limit_pct", {
                        "daily_loss_limit_pct": str(entry.daily_loss_limit_pct),
                        "dollar_limit": str(dollar_limit.quantize(_D("0.01"))),
                        "todays_realized_pnl": str(todays_pnl),
                        "account_equity": str(equity),
                    })
                    return "daily_loss_limit_pct"

        # 1b-profit. Daily PROFIT target as % of equity — lock in gains and
        # pause copy for the rest of the day once today's realized P&L reaches
        # +this%. Mirror image of the daily-loss kill switch.
        if entry.daily_profit_limit_pct is not None:
            equity = get_account_equity(db, entry.user_id)
            if equity:
                dollar_target = equity * entry.daily_profit_limit_pct / _D(100)
                todays_pnl = today_realized_pnl(db, entry.user_id)
                if todays_pnl >= dollar_target:
                    _auto_pause(db, pc, entry.user_id, "daily_profit_limit", {
                        "daily_profit_limit_pct": str(entry.daily_profit_limit_pct),
                        "dollar_target": str(dollar_target.quantize(_D("0.01"))),
                        "todays_realized_pnl": str(todays_pnl),
                        "account_equity": str(equity),
                    })
                    return "daily_profit_limit"

        # 1c. Per-trade loss limit as % of equity (last closed round-trip).
        if entry.per_trade_loss_limit_pct is not None:
            equity = get_account_equity(db, entry.user_id)
            if equity:
                dollar_limit = equity * entry.per_trade_loss_limit_pct / _D(100)
                last_pnl = last_trade_pnl(db, entry.user_id)
                if last_pnl is not None and last_pnl <= -dollar_limit:
                    _auto_pause(db, pc, entry.user_id, "per_trade_loss_limit", {
                        "per_trade_loss_limit_pct": str(entry.per_trade_loss_limit_pct),
                        "dollar_limit": str(dollar_limit.quantize(_D("0.01"))),
                        "last_trade_pnl": str(last_pnl),
                        "account_equity": str(equity),
                    })
                    return "per_trade_loss_limit"

        # 1d. Max-drawdown protection vs the baseline captured when enabled.
        if entry.max_drawdown_pct is not None and entry.max_drawdown_equity_baseline is not None:
            equity = get_account_equity(db, entry.user_id)
            if equity is not None:
                min_equity = entry.max_drawdown_equity_baseline * (1 - entry.max_drawdown_pct / _D(100))
                if equity <= min_equity:
                    _auto_pause(db, pc, entry.user_id, "max_drawdown", {
                        "max_drawdown_pct": str(entry.max_drawdown_pct),
                        "equity_baseline": str(entry.max_drawdown_equity_baseline),
                        "current_equity": str(equity),
                        "min_equity_threshold": str(min_equity.quantize(_D("0.01"))),
                    })
                    return "max_drawdown"

        # ── Req #6: exclusion list (pure memory, zero DB) ──────────────────
        if entry.excluded_symbols and trader_order.symbol.upper() in entry.excluded_symbols:
            _record_outcome(db, pc, PendingCopyStatus.FAILED,
                            f"excluded_symbol:{trader_order.symbol.upper()}")
            return "excluded_symbol"

        # Subscriber opted out of mirroring the trader's exits — they manage
        # their own. Skip any CLOSING order fanned out from the trader (manual
        # close or SL/TP-triggered cascade).
        if trader_order.is_closing and not entry.follow_trader_exits:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "follow_trader_exits_off")
            return "follow_trader_exits_off"

        # ── Req #4 Replace mode: skip if trader is CLOSING a position that
        # this subscriber is managing themselves via TP/SL bracket. ─────────
        if trader_order.is_closing and (entry.take_profit_pct or entry.stop_loss_pct):
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "subscriber_managed_exit")
            return "subscriber_managed_exit"

        if not entry.broker_accounts:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "no_broker")
            return "no_broker"

        # v1: use the subscriber's first broker account. Multi-account
        # fanout is orthogonal to the queue demo.
        acct_snapshot = entry.broker_accounts[0]
        acct = db.get(BrokerAccount, acct_snapshot.id)
        if acct is None:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "broker_account_gone")
            return "broker_account_gone"

        scaled = _scale_quantity(
            trader_order.quantity, entry.multiplier, acct_snapshot.supports_fractional
        )
        if scaled <= 0:
            _record_outcome(db, pc, PendingCopyStatus.FAILED, "zero_qty")
            return "zero_qty"

        child = Order(
            user_id=entry.user_id,
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
            is_closing=trader_order.is_closing,
        )
        db.add(child)
        db.flush()

        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
        except Exception as exc:  # noqa: BLE001
            child.status = OrderStatus.REJECTED
            child.reject_reason = f"credentials_error: {exc}"[:480]
            child.closed_at = datetime.now(timezone.utc)
            _record_outcome(db, pc, PendingCopyStatus.FAILED, str(exc)[:200])
            return "credentials_error"

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

        # ── Req #4: auto TP/SL bracket (Replace mode) ──────────────────────
        # Compute TP/SL target prices from entry premium percentage.
        # Only attempted when:
        #   a) Subscriber has TP and/or SL configured
        #   b) This is an OPENING order (not a close — Replace mode skips closes
        #      via the earlier gate in the risk section)
        #   c) We have a reference price (limit_price from the trader's order)
        tp_price = None
        sl_price = None
        if not trader_order.is_closing and (entry.take_profit_pct or entry.stop_loss_pct):
            ref_price = trader_order.limit_price or trader_order.stop_price
            if ref_price is not None and ref_price > 0:
                from decimal import Decimal as _Decimal
                from app.models.order import OrderSide as _OS
                multiplier = _Decimal(1) if trader_order.side == _OS.BUY else _Decimal(-1)
                if entry.take_profit_pct:
                    # For a BUY, TP is above entry; for SELL, below.
                    tp_price = ref_price * (1 + multiplier * entry.take_profit_pct / _Decimal(100))
                    tp_price = tp_price.quantize(_Decimal("0.01"))
                if entry.stop_loss_pct:
                    # For a BUY, SL is below entry; for SELL, above.
                    sl_price = ref_price * (1 - multiplier * entry.stop_loss_pct / _Decimal(100))
                    sl_price = sl_price.quantize(_Decimal("0.01"))

        # Time the broker round-trip for the Performance page's per-subscriber
        # broker-latency column.
        _broker_t0 = time.monotonic()
        # Try bracket first (IBKR native OCA); fall back to plain entry if
        # the adapter doesn't support it (e.g. Alpaca, Webull, Mock).
        if tp_price is not None or sl_price is not None:
            try:
                resp = adapter.place_bracket_order(request, tp_price, sl_price)
            except NotImplementedError:
                log.info(
                    "subscriber_worker: %s does not support bracket orders — "
                    "falling back to plain entry for subscriber=%s",
                    acct.broker.value, entry.user_id,
                )
                try:
                    resp = place_order_with_recovery(adapter, request)
                except (RecoverableOrderError, Exception) as exc:  # noqa: BLE001
                    msg = (exc.friendly_message if isinstance(exc, RecoverableOrderError)
                           else str(exc))
                    child.status = OrderStatus.REJECTED
                    child.reject_reason = msg[:480]
                    child.closed_at = datetime.now(timezone.utc)
                    audit.record(db, actor_user_id=entry.user_id, action="copy.error",
                                 entity_type="order", entity_id=child.id,
                                 metadata={"parent_order_id": str(trader_order.id),
                                           "error": msg[:300], "path": "queue_bracket_fallback"})
                    _record_outcome(db, pc, PendingCopyStatus.FAILED, msg[:200])
                    events.publish(entry.user_id, _order_event("order.copy_failed", child))
                    return "broker_failed"
            except (RecoverableOrderError, Exception) as exc:  # noqa: BLE001
                msg = (exc.friendly_message if isinstance(exc, RecoverableOrderError)
                       else str(exc))
                child.status = OrderStatus.REJECTED
                child.reject_reason = msg[:480]
                child.closed_at = datetime.now(timezone.utc)
                audit.record(db, actor_user_id=entry.user_id, action="copy.error",
                             entity_type="order", entity_id=child.id,
                             metadata={"parent_order_id": str(trader_order.id),
                                       "error": msg[:300], "path": "queue_bracket"})
                _record_outcome(db, pc, PendingCopyStatus.FAILED, msg[:200])
                events.publish(entry.user_id, _order_event("order.copy_failed", child))
                return "broker_failed"
        else:
            # No bracket configured — plain entry path.
            pass  # falls through to the block below

        try:
            resp = place_order_with_recovery(adapter, request) if (tp_price is None and sl_price is None) else resp  # noqa: E501
        except (RecoverableOrderError, Exception) as exc:  # noqa: BLE001
            msg = (
                exc.friendly_message if isinstance(exc, RecoverableOrderError)
                else str(exc)
            )
            child.status = OrderStatus.REJECTED
            child.reject_reason = msg[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=entry.user_id, action="copy.error",
                entity_type="order", entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "error": msg[:300],
                    "path": "queue_demo",
                },
            )
            _record_outcome(db, pc, PendingCopyStatus.FAILED, msg[:200])
            events.publish(entry.user_id, _order_event("order.copy_failed", child))
            return "broker_failed"

        child.broker_ms = int((time.monotonic() - _broker_t0) * 1000)
        child.status = resp.status
        child.broker_order_id = resp.broker_order_id
        child.submitted_at = resp.submitted_at
        child.filled_quantity = resp.filled_quantity
        child.filled_avg_price = resp.filled_avg_price
        audit.record(
            db, actor_user_id=entry.user_id, action="copy.submitted",
            entity_type="order", entity_id=child.id,
            metadata={
                "parent_order_id": str(trader_order.id),
                "broker_order_id": resp.broker_order_id,
                "scaled_qty": str(child.quantity),
                "path": "queue_demo",
            },
        )
        _record_outcome(db, pc, PendingCopyStatus.SUBMITTED, None, broker_ms=child.broker_ms)
        events.publish(entry.user_id, _order_event("order.copy_submitted", child))
        return "submitted"


_LAST_HEARTBEAT: dict[str, Any] = {"at": None}


def heartbeat_status() -> dict[str, Any]:
    last = _LAST_HEARTBEAT.get("at")
    if last is None:
        return {"running": False, "last_run_at": None, "worker_count": len(_workers)}
    delta = (datetime.now(timezone.utc) - last).total_seconds()
    return {
        "running": True,
        "last_run_at": last.isoformat(),
        "seconds_since": round(delta, 1),
        # How many fan-out workers are actually running — confirms the
        # QUEUE_DEMO_WORKER_COUNT change took effect (50, not the old 16).
        "worker_count": len(_workers),
        "healthy": delta < 60,
    }


# Dedicated thread pool for the workers' blocking DB + broker calls. We must
# NOT use the default executor (run_in_executor(None, …)) — its size is
# min(32, cpu+4), which would throttle a 100-coroutine worker pool to ~12-32
# concurrent broker calls and silently serialise the fanout. Sized to the
# worker count in start_workers() so all N broker calls truly run at once.
_executor: ThreadPoolExecutor | None = None


async def _worker_loop(worker_id: int) -> None:
    """One coroutine. Claims rows one at a time; the blocking claim + broker
    call run in the dedicated executor so all N workers progress in parallel."""
    loop = asyncio.get_running_loop()
    log.info("subscriber_worker[%d]: starting", worker_id)
    while True:
        _LAST_HEARTBEAT["at"] = datetime.now(timezone.utc)
        try:
            # Claim happens inside a thread executor too — SQLAlchemy is
            # sync and the lock query needs its own connection.
            claimed_id = await loop.run_in_executor(_executor, _claim_one_id)
            if claimed_id is None:
                # Nothing to claim — sleep until the LISTEN thread signals new
                # rows (instant) or the fallback fires. Clearing AFTER the wait
                # (and re-claiming at the top of the loop) avoids lost wake-ups.
                if _wakeup is not None:
                    try:
                        await asyncio.wait_for(_wakeup.wait(), timeout=POLL_FALLBACK_SEC)
                    except asyncio.TimeoutError:
                        pass
                    _wakeup.clear()
                else:
                    await asyncio.sleep(POLL_FALLBACK_SEC)
                continue
            outcome = await loop.run_in_executor(_executor, _process_one_sync, claimed_id)
            log.debug("subscriber_worker[%d]: pc=%s outcome=%s",
                      worker_id, claimed_id, outcome)
        except asyncio.CancelledError:
            log.info("subscriber_worker[%d]: cancelled", worker_id)
            return
        except Exception:  # noqa: BLE001
            log.exception("subscriber_worker[%d]: iteration failed", worker_id)
            await asyncio.sleep(0.5)


def _claim_one_id() -> uuid.UUID | None:
    with SessionLocal() as db:
        pc = _claim_one(db)
        return pc.id if pc else None


_workers: list[asyncio.Task] = []


def _build_listen_dsn() -> str:
    # psycopg.connect wants a libpq URL WITHOUT the SQLAlchemy "+psycopg" suffix.
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _listen_loop(loop: asyncio.AbstractEventLoop, wakeup: asyncio.Event) -> None:
    """Dedicated thread: LISTEN on the NOTIFY channel and wake the worker pool
    the instant queue_fanout signals new rows. Reconnects on failure. Because
    the workers keep a fallback poll, a listener outage degrades latency, not
    correctness."""
    import psycopg

    dsn = _build_listen_dsn()
    while not _listener_stop.is_set():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                _listener_state["listening"] = True
                _listener_state["last_error"] = None
                log.info("subscriber_worker: LISTEN %s established", NOTIFY_CHANNEL)
                while not _listener_stop.is_set():
                    # Block up to 2s for the next notification (re-checking stop
                    # between waits). stop_after=1 returns on the first one so we
                    # wake immediately instead of batching for the full window.
                    if list(conn.notifies(timeout=2.0, stop_after=1)):
                        _listener_state["notifies"] += 1
                        _listener_state["last_notify_at"] = datetime.now(timezone.utc)
                        loop.call_soon_threadsafe(wakeup.set)
        except Exception as exc:  # noqa: BLE001
            _listener_state["listening"] = False
            if _listener_stop.is_set():
                break
            _listener_state["last_error"] = str(exc)[:200]
            log.exception("subscriber_worker: LISTEN failed; reconnecting in 1s")
            _listener_stop.wait(1.0)
    _listener_state["listening"] = False
    log.info("subscriber_worker: LISTEN thread exiting")


def listener_status() -> dict[str, Any]:
    """Health-endpoint view of the LISTEN/NOTIFY wake-up. healthy == the thread
    is alive AND the LISTEN connection is currently established (so pickup is
    instant). If unhealthy, workers still drain via the POLL_FALLBACK_SEC poll —
    slower, not broken."""
    thread_alive = _listener_thread is not None and _listener_thread.is_alive()
    last = _listener_state["last_notify_at"]
    return {
        "thread_alive": thread_alive,
        "listening": bool(_listener_state["listening"]),
        "notifies_received": _listener_state["notifies"],
        "last_notify_at": last.isoformat() if last else None,
        "fallback_poll_sec": POLL_FALLBACK_SEC,
        "last_error": _listener_state["last_error"],
        "healthy": thread_alive and bool(_listener_state["listening"]),
    }


async def start_workers(count: int = DEFAULT_WORKER_COUNT) -> None:
    """Launch ``count`` concurrent worker coroutines on the running loop,
    backed by a thread pool sized to ``count`` so all N blocking broker
    calls run in parallel (not throttled by the default executor). Also starts
    the LISTEN/NOTIFY wake-up thread so idle workers pick up new rows instantly."""
    global _executor, _wakeup, _listener_thread
    if _workers:
        log.warning("subscriber_worker: already started (%d), skipping",
                    len(_workers))
        return
    _wakeup = asyncio.Event()
    _listener_stop.clear()
    loop = asyncio.get_running_loop()
    _listener_thread = threading.Thread(
        target=_listen_loop, args=(loop, _wakeup),
        name="subworker-listen", daemon=True,
    )
    _listener_thread.start()
    _executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="subworker")
    for i in range(count):
        _workers.append(asyncio.create_task(_worker_loop(i)))
    log.info("subscriber_worker: started %d worker(s) (executor max_workers=%d) + LISTEN wake-up",
             count, count)


async def stop_workers() -> None:
    global _executor, _listener_thread
    _listener_stop.set()  # daemon LISTEN thread unblocks within its 2s window
    _listener_thread = None
    for t in _workers:
        t.cancel()
    _workers.clear()
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None
