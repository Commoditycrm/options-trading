import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import client_ip, current_user, require_trader
from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal, get_db
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderSide, OrderStatus
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.schemas.order import CloseOrderIn, DailyPnL, OrderOut, PlaceOrderIn
from app.services import audit, copy_engine, events, fanout_stream, fills_sync
from app.services.crypto import decrypt_json
from app.services.order_retry import RecoverableOrderError, place_order_with_recovery
from app.services.pnl import realized_pnl_by_day

router = APIRouter(prefix="/api", tags=["trades"])


@router.get("/trades", response_model=list[OrderOut])
def list_trades(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
) -> list[Order]:
    q = (
        select(Order)
        .options(selectinload(Order.fills))
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if from_:
        q = q.where(Order.created_at >= datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc))
    if to:
        q = q.where(Order.created_at < datetime.combine(to, datetime.min.time(), tzinfo=timezone.utc))
    return list(db.execute(q).scalars())


@router.get("/trades/{order_id}", response_model=OrderOut)
def get_trade(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    order = db.execute(
        select(Order).options(selectinload(Order.fills)).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not order or order.user_id != user.id:
        raise HTTPException(404, "not_found")
    return order


_CANCELLABLE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _run_cancel_fanout_in_background(trader_order_id: uuid.UUID) -> None:
    """When a trader cancels their root order, cascade-cancel every still-open
    subscriber mirror at the subscriber's broker. Runs after the trader's HTTP
    response is sent. Per-mirror failures are audited, not raised."""
    with SessionLocal() as db:
        children = list(db.execute(
            select(Order).where(
                Order.parent_order_id == trader_order_id,
                Order.status.in_(_CANCELLABLE_STATUSES),
            )
        ).scalars())
        if not children:
            return

        pending: list[tuple[Order, object]] = []  # (child, adapter)
        for child in children:
            if not child.broker_order_id:
                # Never made it to the broker — just mark cancelled locally.
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            acct = db.get(BrokerAccount, child.broker_account_id)
            if acct is None:
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            try:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=child.user_id, action="order.mirror_cancel_creds_error",
                    entity_type="order", entity_id=child.id,
                    metadata={"parent_order_id": str(trader_order_id), "error": str(exc)[:300]},
                )
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            pending.append((child, adapter))

        def _cancel(item: tuple[Order, object]) -> tuple[Order, str | None]:
            ch, ad = item
            try:
                ad.cancel_order(ch.broker_order_id)  # type: ignore[attr-defined]
                return ch, None
            except Exception as exc:  # noqa: BLE001
                return ch, str(exc)[:300]

        if pending:
            with ThreadPoolExecutor(max_workers=min(32, len(pending))) as pool:
                results = list(pool.map(_cancel, pending))
            for child, err in results:
                # Re-fetch through the session in case SQLAlchemy needs it.
                ch = db.get(Order, child.id)
                if ch is None:
                    continue
                if err is None:
                    ch.status = OrderStatus.CANCELED
                    ch.closed_at = datetime.now(timezone.utc)
                    audit.record(
                        db, actor_user_id=ch.user_id, action="order.mirror_cancelled",
                        entity_type="order", entity_id=ch.id,
                        metadata={
                            "parent_order_id": str(trader_order_id),
                            "broker_order_id": ch.broker_order_id,
                        },
                    )
                    events.publish(ch.user_id, copy_engine._order_event("order.cancelled", ch))
                else:
                    # Broker rejected (e.g. mirror already filled before we got
                    # to it). Don't mutate status — sync-fills will reconcile.
                    audit.record(
                        db, actor_user_id=ch.user_id, action="order.mirror_cancel_failed",
                        entity_type="order", entity_id=ch.id,
                        metadata={
                            "parent_order_id": str(trader_order_id),
                            "broker_order_id": ch.broker_order_id,
                            "error": err,
                        },
                    )
        db.commit()


def _submit_to_broker_in_background(
    order_id: uuid.UUID,
    actor_id: uuid.UUID,
    will_fanout: bool,
    ip_address: str | None,
) -> None:
    """Runs after the HTTP response. Sends the already-persisted order to the
    broker, updates its status from the broker response, publishes
    order.updated SSE so the UI flips from pending → submitted/filled/rejected,
    and (only if the broker accepted) fans out to subscribers.

    Why this lives off the request:
      The synchronous adapter.place_order() call to a broker takes 100-600ms
      typical. Doing it inline made the trader's BUY/SELL click feel sluggish.
      With the trade-update stream already pushing fills via order.updated,
      we can return the PENDING row instantly and let the UI heal itself.

    Fanout sequencing:
      Subscribers should NOT mirror a trader order that the broker rejected.
      Running fanout inside the same task — after a successful broker submit —
      guarantees the rejection path skips fanout entirely.
    """
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if order is None:
            return
        actor = db.get(User, actor_id)
        if actor is None:
            return
        acct = db.get(BrokerAccount, order.broker_account_id)
        if acct is None:
            # Account was deleted between the request and this background task
            # firing. Mark rejected so the UI doesn't keep the row in "pending".
            order.status = OrderStatus.REJECTED
            order.reject_reason = "broker_account_missing"
            order.closed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(order)
            events.publish(actor.id, copy_engine._order_event("order.updated", order))
            return

        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            result = place_order_with_recovery(
                adapter,
                BrokerOrderRequest(
                    instrument_type=order.instrument_type,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    stop_price=order.stop_price,
                    option_expiry=order.option_expiry,
                    option_strike=order.option_strike,
                    option_right=order.option_right,
                    client_order_id=str(order.id),
                ),
            )
        except RecoverableOrderError as rec:
            # Broker rejected with a well-known cause — give the user the
            # cleaned-up message but log the raw error for support diagnostics.
            order.status = OrderStatus.REJECTED
            order.reject_reason = rec.friendly_message[:480]
            order.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=actor.id, action="trader.order_rejected_at_broker",
                entity_type="order", entity_id=order.id,
                metadata={
                    "friendly": rec.friendly_message,
                    "raw_error": str(rec.original)[:480],
                    "classification": "user_fixable",
                },
                ip_address=ip_address,
            )
            db.commit()
            db.refresh(order)
            events.publish(actor.id, copy_engine._order_event("order.updated", order))
            return
        except Exception as exc:  # noqa: BLE001
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)[:480]
            order.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=actor.id, action="trader.order_rejected_at_broker",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(exc)[:480]}, ip_address=ip_address,
            )
            db.commit()
            db.refresh(order)
            events.publish(actor.id, copy_engine._order_event("order.updated", order))
            return

        order.broker_order_id = result.broker_order_id
        order.status = result.status
        order.submitted_at = result.submitted_at
        order.filled_quantity = result.filled_quantity
        order.filled_avg_price = result.filled_avg_price
        audit.record(
            db, actor_user_id=actor.id, action="trader.order_placed",
            entity_type="order", entity_id=order.id,
            metadata={
                "broker": acct.broker.value, "symbol": order.symbol, "side": order.side.value,
                "qty": str(order.quantity), "broker_order_id": result.broker_order_id,
            },
            ip_address=ip_address,
        )
        db.commit()
        db.refresh(order)
        events.publish(actor.id, copy_engine._order_event("order.updated", order))

        if will_fanout:
            # Two dispatch paths:
            #
            #   1. Redis Streams (production / scale): enumerate targets +
            #      XADD one message per (subscriber, broker_account). The
            #      worker pool consumes and places mirror orders in parallel.
            #      Returns immediately — broker calls happen in workers, the
            #      subscriber's UI updates via order.copy_submitted SSE
            #      published from inside process_one_fanout.
            #
            #   2. In-process (no Redis configured, e.g. early dev):
            #      copy_engine.fanout runs the ThreadPoolExecutor path
            #      inline. Same observable outcome.
            #
            # Either way, audit "trader.fanout_dispatched" tracks the
            # decision; the per-target submitted/error/skipped audit lives
            # in process_one_fanout itself (so we don't double-count).
            if fanout_stream.is_configured():
                targets = copy_engine.enumerate_fanout_targets(db, actor.id)
                count = fanout_stream.publish_targets(order.id, targets)
                audit.record(
                    db,
                    actor_user_id=actor.id,
                    action="trader.fanout_dispatched",
                    entity_type="order",
                    entity_id=order.id,
                    metadata={
                        "dispatch": "redis_stream",
                        "target_count": count,
                    },
                )
                db.commit()
            else:
                fan_results = copy_engine.fanout(db, order, actor)
                audit.record(
                    db,
                    actor_user_id=actor.id,
                    action="trader.fanout_complete",
                    entity_type="order",
                    entity_id=order.id,
                    metadata={
                        "dispatch": "in_process",
                        "subscriber_count": len({r.subscriber_user_id for r in fan_results}),
                        "submitted": sum(1 for r in fan_results if r.status == "submitted"),
                        "errors": sum(1 for r in fan_results if r.status == "error"),
                        "skipped": sum(1 for r in fan_results if r.status.startswith("skipped")),
                    },
                )
                db.commit()


def _place_trader_order(
    db: Session,
    trader: User,
    payload: PlaceOrderIn,
    broker_account_id: uuid.UUID,
    background: BackgroundTasks,
    request: Request,
    skip_fanout: bool = False,
) -> Order:
    """Core order-placement flow. Used by /api/trades for trader-originated
    orders (which fan out to subscribers) and by close endpoints. Also reused
    for subscriber-originated closes — in that case we skip the trader
    kill-switch check and don't fan anything out.

    Returns the persisted Order at status=PENDING. The actual broker submission
    runs as a background task — the trade-update SSE stream pushes the result
    (order.updated) so the UI flips to SUBMITTED/FILLED/REJECTED on its own
    within ~100-600ms. This keeps the HTTP response ~10ms instead of blocking
    on the broker round-trip.

    Pre-flight validation (kill switch, broker account exists / connected) still
    runs synchronously so misconfiguration raises 4xx immediately — only the
    happy path is deferred.

    Caller commits nothing — this function commits before returning.
    """
    is_trader = trader.role == UserRole.TRADER
    # Trader kill switch only applies to traders. Subscribers can always
    # manage (close/cancel) their own broker accounts.
    if is_trader and not copy_engine.trader_can_trade(db, trader):
        raise HTTPException(409, "trading_disabled")

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != trader.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")

    # Will this order be broadcast to subscribers? Pre-compute so we can
    # stamp the flag on the row at creation time (immutable record of intent).
    from app.models.settings import TraderSettings  # local import — avoid cycle
    ts = db.get(TraderSettings, trader.id) if is_trader else None
    will_fanout = is_trader and not skip_fanout and not (ts and ts.copy_paused)

    order = Order(
        user_id=trader.id,
        broker_account_id=acct.id,
        instrument_type=payload.instrument_type,
        symbol=payload.symbol.upper(),
        option_expiry=payload.option_expiry,
        option_strike=payload.option_strike,
        option_right=payload.option_right,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        stop_price=payload.stop_price,
        status=OrderStatus.PENDING,
        fanned_out_to_subscribers=will_fanout,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    events.publish(trader.id, copy_engine._order_event("order.placed", order))

    # Defer the broker submission AND the fanout to the background task. Fanout
    # is chained inside the task so subscribers don't mirror a trader order the
    # broker rejected.
    background.add_task(
        _submit_to_broker_in_background,
        order.id,
        trader.id,
        will_fanout,
        client_ip(request),
    )
    return order


@router.post("/trades", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def place_trade(
    payload: PlaceOrderIn,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Trader's broker account to place on"),
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> Order:
    return _place_trader_order(db, trader, payload, broker_account_id, background, request)


@router.post("/trades/{order_id}/cancel", response_model=OrderOut)
def cancel_trade(
    order_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Cancel an open order at the broker. Any user can cancel their own
    orders (subscriber's mirror or trader's own). Cancellable statuses:
    PENDING, SUBMITTED, ACCEPTED, PARTIALLY_FILLED."""
    order = db.execute(
        select(Order).options(selectinload(Order.fills)).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not order or order.user_id != user.id:
        raise HTTPException(404, "not_found")
    if order.status not in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED
    ):
        raise HTTPException(409, f"not_cancellable: status is {order.status.value}")

    acct = db.get(BrokerAccount, order.broker_account_id)
    if acct is None:
        raise HTTPException(404, "broker_account_missing")

    # Best-effort broker call. If the broker rejects (e.g. order already filled),
    # surface the error but DON'T mutate local state — DB stays accurate.
    if order.broker_order_id:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter_for(acct, creds).cancel_order(order.broker_order_id)
        except Exception as exc:  # noqa: BLE001
            audit.record(
                db, actor_user_id=user.id, action="order.cancel_failed",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(exc)[:480]}, ip_address=client_ip(request),
            )
            db.commit()
            raise HTTPException(502, f"broker_error: {exc}")

    order.status = OrderStatus.CANCELED
    order.closed_at = datetime.now(timezone.utc)
    audit.record(
        db, actor_user_id=user.id, action="order.cancelled",
        entity_type="order", entity_id=order.id,
        metadata={"broker_order_id": order.broker_order_id},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(order)
    events.publish(user.id, copy_engine._order_event("order.cancelled", order))

    # If a trader cancels their own root order, cascade the cancel to every
    # open subscriber mirror. Subscribers cancelling their own mirror skip
    # this — there are no children to propagate to.
    if order.parent_order_id is None and user.role == UserRole.TRADER:
        background.add_task(_run_cancel_fanout_in_background, order.id)

    return order


@router.post("/trades/{order_id}/close", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def close_trade(
    order_id: uuid.UUID,
    payload: CloseOrderIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Close a filled order by placing a reverse-side order of the same size
    (or smaller, if `quantity` is given). The reverse is itself a normal
    order — for a trader it fans out to subscribers; for a subscriber it
    just executes against their own broker.
    """
    original = db.execute(
        select(Order).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not original or original.user_id != user.id:
        raise HTTPException(404, "not_found")
    if original.status != OrderStatus.FILLED:
        raise HTTPException(409, f"not_closeable: original status is {original.status.value}")

    # Reverse the side; default qty to whatever filled on the original.
    close_qty = payload.quantity if payload.quantity is not None else original.filled_quantity
    if close_qty <= 0:
        raise HTTPException(422, "quantity_must_be_positive")
    if close_qty > original.filled_quantity:
        raise HTTPException(422, "quantity_exceeds_original_filled")

    reverse_side = OrderSide.SELL if original.side == OrderSide.BUY else OrderSide.BUY

    new_payload = PlaceOrderIn(
        instrument_type=original.instrument_type,
        symbol=original.symbol,
        side=reverse_side,
        order_type=payload.order_type,
        quantity=close_qty,
        limit_price=payload.limit_price,
        stop_price=None,
        option_expiry=original.option_expiry,
        option_strike=original.option_strike,
        option_right=original.option_right,
    )

    new_order = _place_trader_order(
        db, user, new_payload, original.broker_account_id, background, request
    )

    audit.record(
        db, actor_user_id=user.id, action="order.closed",
        entity_type="order", entity_id=original.id,
        metadata={
            "closed_with_order_id": str(new_order.id),
            "close_qty": str(close_qty),
            "close_type": payload.order_type.value,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    return new_order


@router.get("/calendar/pnl", response_model=list[DailyPnL])
def calendar_pnl(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    tz: str | None = Query(
        default=None,
        description="IANA timezone (e.g. 'Asia/Calcutta'). Fills are bucketed by this TZ so the calendar matches what the user sees as 'today'. Defaults to US/Eastern when omitted.",
    ),
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Trader-only: view another user's P&L (must be a subscriber following you).",
    ),
) -> list[DailyPnL]:
    if from_ > to:
        raise HTTPException(422, "from must be <= to")

    # View-as: trader can request a subscriber's calendar. Subscribers can
    # only view their own.
    target_user_id = user.id
    if user_id is not None and user_id != user.id:
        if user.role != UserRole.TRADER:
            raise HTTPException(403, "trader_only")
        sub = db.get(SubscriberSettings, user_id)
        if not sub or sub.following_trader_id != user.id:
            raise HTTPException(404, "not_a_subscriber")
        target_user_id = user_id

    # Pull the latest fills for the target user before computing P&L. The
    # frontend already runs sync-fills for the caller on mount, but when a
    # trader views a *subscriber's* P&L the subscriber's mirror orders may
    # still be at status=submitted with filled_quantity=0 — they'd be
    # excluded from the P&L query and the day would look empty. Sync first
    # so freshly-filled mirrors land on the right day.
    try:
        fills_sync.sync_user_fills(db, target_user_id)
        db.commit()
    except Exception:  # noqa: BLE001
        # Sync failures are non-fatal; we still return whatever P&L exists.
        db.rollback()

    daily = realized_pnl_by_day(db, target_user_id, start=from_, end=to, tz_name=tz)
    return [DailyPnL(day=d, realized_pnl=p, trade_count=n) for d, (p, n) in sorted(daily.items())]


@router.post("/trades/sync-fills")
def sync_fills(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Pull activities from every connected broker and upsert fills locally.
    The Calendar + Trades pages call this on load so realized P&L stays fresh.
    """
    result = fills_sync.sync_user_fills(db, user.id)
    if result["fills_added"] or result["orders_added"]:
        audit.record(
            db,
            actor_user_id=user.id,
            action="fills.synced",
            metadata=result,
            ip_address=client_ip(request),
        )
    db.commit()
    return result


@router.get("/trader/fanout-performance")
def fanout_performance(
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
    limit: int = Query(default=20, le=100),
) -> list[dict]:
    """Trader-only endpoint that powers the Fanout Performance page.

    For each of the trader's most recent fanned-out orders, returns the
    timing breakdown the client wants to see in the demo:
      - submitted_at         when Alpaca accepted the trader's order
      - detected_at          when our backend first recorded the row
                             (= when the poller saw it; or for UI-placed
                             orders, when the trader hit the button)
      - fanout_completed_at  when the last subscriber's order was
                             submitted at their broker
      - detection_lag_ms     Alpaca-accept → our detection
      - fanout_duration_ms   our detection → last subscriber submitted
      - total_ms             Alpaca-accept → last subscriber submitted
      - subscribers_targeted count of child orders we tried to place
      - subscribers_accepted count where the subscriber's broker
                             accepted the mirror (FILLED / SUBMITTED /
                             ACCEPTED / PARTIALLY_FILLED)
      - subscribers_rejected count where the subscriber's broker rejected

    Powers the "Fanout Performance" UI in the trader's account — no extra
    DB writes, purely a read of existing columns.
    """
    # Fetch this trader's most recent orders that were broadcast to
    # subscribers. parent_order_id IS NULL filters out subscriber mirrors;
    # fanned_out_to_subscribers=True means we attempted fanout.
    parents = list(
        db.execute(
            select(Order)
            .options(selectinload(Order.children))
            .where(
                Order.user_id == trader.id,
                Order.parent_order_id.is_(None),
                Order.fanned_out_to_subscribers.is_(True),
            )
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        .scalars()
    )

    out: list[dict] = []
    for p in parents:
        children = list(p.children or [])
        # Latest child submitted_at = when fanout "finished" for the
        # purposes of the demo. NULL submitted_at means that child never
        # made it to the broker (skipped / errored before submit).
        child_submits = [c.submitted_at for c in children if c.submitted_at is not None]
        fanout_completed_at = max(child_submits) if child_submits else None

        # detection_lag = how long between Alpaca accepting the trader's
        # order and us seeing it (the poll-cycle delay). For external
        # trades, submitted_at is Alpaca's clock (after the fix); for
        # UI-placed orders submitted_at and created_at are essentially
        # the same so detection_lag will be ~0.
        det_lag_ms = None
        if p.submitted_at and p.created_at:
            det_lag_ms = max(0, int((p.created_at - p.submitted_at).total_seconds() * 1000))

        # fanout_duration = our detection -> last subscriber submitted
        fanout_ms = None
        if fanout_completed_at and p.created_at:
            fanout_ms = max(0, int((fanout_completed_at - p.created_at).total_seconds() * 1000))

        total_ms = None
        if fanout_completed_at and p.submitted_at:
            total_ms = max(0, int((fanout_completed_at - p.submitted_at).total_seconds() * 1000))

        accepted_statuses = {
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        }
        sub_accepted = sum(1 for c in children if c.status in accepted_statuses)
        sub_rejected = sum(1 for c in children if c.status == OrderStatus.REJECTED)

        # Per-subscriber breakdown for the expandable row. Sort by
        # submitted_at so the demo timeline reads in order.
        sub_user_ids = {c.user_id for c in children}
        subs_by_id: dict = {}
        if sub_user_ids:
            for u in db.execute(
                select(User.id, User.email, User.display_name).where(User.id.in_(sub_user_ids))
            ).all():
                subs_by_id[u.id] = {"email": u.email, "display_name": u.display_name}

        # Sort by submitted_at (then created_at) so the demo timeline reads
        # in order. Use a tz-aware sentinel for None so we don't mix naive
        # and aware datetimes during comparison.
        _SENTINEL = datetime.max.replace(tzinfo=timezone.utc)
        subscribers_rows: list[dict] = []
        for c in sorted(children, key=lambda x: (x.submitted_at or x.created_at or _SENTINEL)):
            u_info = subs_by_id.get(c.user_id, {})
            child_lag_ms = None
            if c.submitted_at and p.created_at:
                child_lag_ms = max(0, int((c.submitted_at - p.created_at).total_seconds() * 1000))
            subscribers_rows.append({
                "child_order_id": str(c.id),
                "user_id": str(c.user_id),
                "email": u_info.get("email"),
                "display_name": u_info.get("display_name"),
                "status": c.status.value,
                "broker_order_id": c.broker_order_id,
                "quantity": str(c.quantity),
                "filled_quantity": str(c.filled_quantity or 0),
                "child_created_at": c.created_at.isoformat() if c.created_at else None,
                "child_submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
                "subscriber_lag_ms": child_lag_ms,
                "reject_reason": c.reject_reason,
            })

        out.append({
            "order_id": str(p.id),
            "symbol": p.symbol,
            "side": p.side.value,
            "quantity": str(p.quantity),
            "instrument_type": p.instrument_type.value,
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "detected_at": p.created_at.isoformat() if p.created_at else None,
            "fanout_completed_at": fanout_completed_at.isoformat() if fanout_completed_at else None,
            "detection_lag_ms": det_lag_ms,
            "fanout_duration_ms": fanout_ms,
            "total_ms": total_ms,
            "subscribers_targeted": len(children),
            "subscribers_accepted": sub_accepted,
            "subscribers_rejected": sub_rejected,
            "subscribers": subscribers_rows,
        })
    return out
