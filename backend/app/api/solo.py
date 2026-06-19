"""Solo-trader toolset: 3-way Exit All, post-exit simulation, and Re-enter All.

A solo trader (TraderSettings.solo_mode) trades only for himself. These endpoints
let him flatten everything at a chosen price (market / bid / ask), watch a live
"what-if" simulation of what he exited, and re-enter the same set.

Bid/ask pricing and the simulation use the Alpaca market-data quotes; if the feed
isn't entitled they degrade gracefully (bid/ask falls back to market; the
simulation shows null marks).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_trader
from app.api.trades import _place_trader_order
from app.brokers import adapter_for
from app.brokers.alpaca import build_occ_symbol
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderType
from app.models.solo import SoloExitItem, SoloExitSnapshot
from app.models.user import User
from app.schemas.order import PlaceOrderIn
from app.services.crypto import decrypt_json

router = APIRouter(prefix="/api/solo", tags=["solo"])


class PositionRef(BaseModel):
    """Stable identifier for one open position to act on."""
    broker_account_id: uuid.UUID
    broker_symbol: str  # broker's canonical id (OCC for options, ticker for stocks)


class ExitAllIn(BaseModel):
    """Optional body for exit-all. When ``selections`` is omitted/null we exit
    EVERY open position (identical to the original one-click behavior, incl. the
    account-wide open-order cancel). When a list is supplied we close ONLY those
    positions and leave everything else — and its working orders — untouched."""
    selections: list[PositionRef] | None = None


class ReenterIn(BaseModel):
    """Optional body for reenter-all. When ``item_ids`` is omitted/null we
    re-enter every item in the latest snapshot; otherwise only the listed ones."""
    item_ids: list[uuid.UUID] | None = None


def _quote(adapter, instrument: str, symbol: str, occ: str | None) -> dict:
    """(bid, ask, mid) for a contract, or all-None if no market-data."""
    try:
        if instrument == "option" and occ:
            return adapter.get_option_quote(occ)
        return adapter.get_stock_quote(symbol)
    except Exception:  # noqa: BLE001
        return {"bid": None, "ask": None, "mid": None}


def _alpaca_adapter_for(db: Session, user_id):
    """Build an adapter on the user's first connected Alpaca account (for quotes)."""
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker == BrokerName.ALPACA,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return None
    try:
        return adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    except Exception:  # noqa: BLE001
        return None


@router.post("/exit-all")
def exit_all(
    request: Request,
    background: BackgroundTasks,
    mode: str = Query("market", pattern="^(market|bid|ask)$"),
    payload: ExitAllIn | None = None,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Flatten open positions across the trader's connected brokers at the chosen
    price (market / bid / ask) and record a snapshot for the simulation +
    re-enter. Bid/ask fall back to market when no quote is available.

    With no body (or selections=null) this exits EVERY position and cancels each
    account's open orders first (held quantity otherwise blocks the close) —
    identical to the original one-click Exit All. With a selections list it
    closes ONLY those positions and does NOT touch other positions or their
    working orders (a partial exit is a surgical action)."""
    selected: set[tuple[uuid.UUID, str]] | None = None
    if payload is not None and payload.selections is not None:
        selected = {(s.broker_account_id, s.broker_symbol.upper()) for s in payload.selections}

    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == trader.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    # Only ONE exit snapshot is the active simulation at a time. Close any prior
    # open snapshot so a fresh Exit All supersedes it (and Re-Enter All then
    # cleanly clears the table). reentered_at doubles as the "closed" marker.
    db.execute(
        update(SoloExitSnapshot)
        .where(SoloExitSnapshot.user_id == trader.id, SoloExitSnapshot.reentered_at.is_(None))
        .values(reentered_at=datetime.now(timezone.utc))
    )

    snapshot = SoloExitSnapshot(user_id=trader.id, exit_mode=mode)
    db.add(snapshot)
    db.flush()

    # Build adapters once. On a FULL exit (no selection) cancel each account's
    # open orders first — a pending order holds quantity ("held_for_orders"),
    # which otherwise makes the close fail with "insufficient qty available".
    # On a PARTIAL exit we skip the account-wide cancel so excluded positions'
    # working orders stay untouched (the adapter has no per-symbol cancel).
    adapters: dict = {}
    did_cancel = False
    for acct in accts:
        try:
            adapters[acct.id] = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
        except Exception:  # noqa: BLE001
            adapters[acct.id] = None
            continue
        adapter = adapters[acct.id]
        if selected is None:
            # Full exit: cancel everything on the account.
            try:
                adapter.cancel_all_orders()
                did_cancel = True
            except Exception:  # noqa: BLE001
                pass  # best-effort
        else:
            # Partial exit: cancel only the SELECTED positions' working orders so
            # their held quantity is freed (otherwise the close rejects with
            # "insufficient qty / held_for_orders"), while leaving the excluded
            # positions' orders untouched. Needs a per-symbol cancel (Alpaca).
            syms = [bsym for (aid, bsym) in selected if aid == acct.id]
            cancel_fn = getattr(adapter, "cancel_open_orders_for_symbols", None)
            if syms and callable(cancel_fn):
                try:
                    if cancel_fn(syms):
                        did_cancel = True
                except Exception:  # noqa: BLE001
                    pass  # best-effort
    if did_cancel:
        time.sleep(0.6)

    closed: list[dict] = []
    failed: list[dict] = []
    for acct in accts:
        adapter = adapters.get(acct.id)
        if adapter is None:
            failed.append({"broker_account_id": str(acct.id), "error": "adapter_unavailable"})
            continue
        try:
            positions = [p for p in adapter.get_positions() if p.quantity != 0]
        except Exception as exc:  # noqa: BLE001
            failed.append({"broker_account_id": str(acct.id), "error": str(exc)[:200]})
            continue

        # Honour a partial selection: only close the chosen positions.
        if selected is not None:
            positions = [p for p in positions if (acct.id, p.broker_symbol.upper()) in selected]

        for pos in positions:
            is_long = pos.quantity > 0
            reverse_side = OrderSide.SELL if is_long else OrderSide.BUY
            qty = abs(pos.quantity)
            is_option = pos.instrument_type == InstrumentType.OPTION
            occ = (
                build_occ_symbol(pos.symbol, pos.option_expiry, pos.option_strike, pos.option_right.value)
                if is_option and pos.option_expiry and pos.option_strike and pos.option_right
                else None
            )

            order_type = OrderType.MARKET
            limit_price = None
            exit_ref = None
            q = _quote(adapter, pos.instrument_type.value, pos.symbol, occ)
            if mode in ("bid", "ask"):
                px = q.get(mode)
                # A 0/None quote means no live data — fall back to MARKET rather
                # than build an invalid limit (limit_price must be > 0).
                if px is not None and px > 0:
                    order_type = OrderType.LIMIT
                    limit_price = px
                    exit_ref = px
            if exit_ref is None:
                m = q.get("mid")
                exit_ref = m if (m is not None and m > 0) else None

            try:
                payload = PlaceOrderIn(
                    instrument_type=pos.instrument_type,
                    symbol=pos.symbol,
                    side=reverse_side,
                    order_type=order_type,
                    quantity=qty,
                    limit_price=limit_price,
                    stop_price=None,
                    option_expiry=pos.option_expiry if is_option else None,
                    option_strike=pos.option_strike if is_option else None,
                    option_right=pos.option_right if is_option else None,
                )
                order = _place_trader_order(
                    db, trader, payload, acct.id, background, request,
                    skip_fanout=True, is_closing=True,
                )
                db.add(SoloExitItem(
                    snapshot_id=snapshot.id,
                    broker_account_id=acct.id,
                    order_id=order.id,
                    instrument_type=pos.instrument_type.value,
                    symbol=pos.symbol,
                    occ_symbol=occ,
                    original_side=(OrderSide.BUY if is_long else OrderSide.SELL).value,
                    quantity=qty,
                    entry_price=pos.avg_entry_price,
                    exit_price=exit_ref,
                ))
                closed.append({"symbol": pos.broker_symbol, "qty": str(qty), "order_id": str(order.id),
                               "order_type": order_type.value, "limit_price": str(limit_price) if limit_price else None})
            except Exception as exc:  # noqa: BLE001
                failed.append({"symbol": pos.broker_symbol, "error": str(exc)[:200]})

    db.commit()
    return {"snapshot_id": str(snapshot.id), "mode": mode,
            "closed_count": len(closed), "failed_count": len(failed),
            "closed": closed, "failed": failed}


def _latest_open_snapshot(db: Session, user_id) -> SoloExitSnapshot | None:
    return db.execute(
        select(SoloExitSnapshot)
        .options(selectinload(SoloExitSnapshot.items))
        .where(SoloExitSnapshot.user_id == user_id, SoloExitSnapshot.reentered_at.is_(None))
        .order_by(SoloExitSnapshot.created_at.desc())
        .limit(1)
    ).scalars().first()


@router.get("/simulation")
def simulation(db: Session = Depends(get_db), trader: User = Depends(require_trader)) -> dict:
    """Latest (not-yet-re-entered) exit snapshot with a live mark and the
    'P&L if I had held' for each exited contract."""
    snap = _latest_open_snapshot(db, trader.id)
    if snap is None:
        return {"snapshot_id": None, "items": []}

    adapter = _alpaca_adapter_for(db, trader.id)

    # Live status of each exit order, so the /solo page can show submitted /
    # filled / rejected inline instead of forcing a trip to Order History.
    order_ids = [it.order_id for it in snap.items if it.order_id is not None]
    orders_by_id: dict = {}
    if order_ids:
        for o in db.execute(select(Order).where(Order.id.in_(order_ids))).scalars():
            orders_by_id[o.id] = o

    items_out: list[dict] = []
    any_rejected = False
    for it in snap.items:
        mid = None
        if adapter is not None:
            mid = _quote(adapter, it.instrument_type, it.symbol, it.occ_symbol).get("mid")
        unit = Decimal(100) if it.instrument_type == "option" else Decimal(1)
        sign = Decimal(1) if it.original_side == "buy" else Decimal(-1)
        pnl_if_held = None
        if mid is not None and it.exit_price is not None:
            pnl_if_held = (Decimal(str(mid)) - it.exit_price) * it.quantity * unit * sign

        o = orders_by_id.get(it.order_id) if it.order_id else None
        order_status = o.status.value if o is not None else None
        if order_status in ("rejected", "canceled", "expired"):
            any_rejected = True
        items_out.append({
            "item_id": str(it.id),
            "order_id": str(it.order_id) if it.order_id else None,
            "order_status": order_status,
            "filled_avg_price": str(o.filled_avg_price) if (o and o.filled_avg_price is not None) else None,
            "reject_reason": o.reject_reason if (o and o.reject_reason) else None,
            "symbol": it.symbol,
            "occ_symbol": it.occ_symbol,
            "instrument_type": it.instrument_type,
            "side": it.original_side,
            "quantity": str(it.quantity),
            "entry_price": str(it.entry_price) if it.entry_price is not None else None,
            "exit_price": str(it.exit_price) if it.exit_price is not None else None,
            "current_mid": str(mid) if mid is not None else None,
            "pnl_if_held": str(pnl_if_held.quantize(Decimal("0.01"))) if pnl_if_held is not None else None,
        })
    return {
        "snapshot_id": str(snap.id),
        "exit_mode": snap.exit_mode,
        "created_at": snap.created_at.isoformat(),
        "reentered_at": snap.reentered_at.isoformat() if snap.reentered_at else None,
        "items": items_out,
        "any_rejected": any_rejected,
        "quotes_available": adapter is not None,
    }


@router.post("/reenter-all")
def reenter_all(
    request: Request,
    background: BackgroundTasks,
    mode: str = Query("market", pattern="^(market|bid|ask)$"),
    payload: ReenterIn | None = None,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Re-open positions from the latest exit snapshot (original side + qty) at
    the chosen price (market / bid / ask), then mark the snapshot re-entered.
    With no body it re-enters every item; with item_ids it re-enters only those.
    Bid/ask place limit orders at the live quote; fall back to market with none."""
    from app.brokers.alpaca import _parse_occ

    snap = _latest_open_snapshot(db, trader.id)
    if snap is None:
        raise HTTPException(404, "no_open_snapshot")

    wanted: set[uuid.UUID] | None = None
    if payload is not None and payload.item_ids is not None:
        wanted = set(payload.item_ids)
    items = [it for it in snap.items if wanted is None or it.id in wanted]

    # Cache one adapter per broker account (for quotes), built lazily.
    adapters: dict = {}
    def _adapter(acct_id):
        if acct_id not in adapters:
            acct = db.get(BrokerAccount, acct_id)
            a = None
            if acct is not None:
                try:
                    a = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
                except Exception:  # noqa: BLE001
                    a = None
            adapters[acct_id] = a
        return adapters[acct_id]

    placed: list[dict] = []
    failed: list[dict] = []
    for it in items:
        is_option = it.instrument_type == "option"
        expiry = strike = right = None
        if is_option and it.occ_symbol:
            parsed = _parse_occ(it.occ_symbol)
            if parsed:
                _, expiry, strike, right = parsed  # right is OptionRight

        order_type = OrderType.MARKET
        limit_price = None
        if mode in ("bid", "ask"):
            adapter = _adapter(it.broker_account_id)
            if adapter is not None:
                px = _quote(adapter, it.instrument_type, it.symbol, it.occ_symbol).get(mode)
                if px is not None and px > 0:
                    order_type = OrderType.LIMIT
                    limit_price = px

        try:
            order_payload = PlaceOrderIn(
                instrument_type=InstrumentType(it.instrument_type),
                symbol=it.symbol,
                side=OrderSide(it.original_side),
                order_type=order_type,
                quantity=it.quantity,
                limit_price=limit_price,
                stop_price=None,
                option_expiry=expiry,
                option_strike=strike,
                option_right=right,
            )
            order = _place_trader_order(
                db, trader, order_payload, it.broker_account_id, background, request,
                skip_fanout=True, is_closing=False,
            )
            placed.append({"symbol": it.symbol, "qty": str(it.quantity), "order_id": str(order.id),
                           "order_type": order_type.value})
            # Drop a re-entered item so the simulation stops showing it (and it
            # can't be re-entered twice). A partial re-enter keeps the rest live.
            db.delete(it)
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": it.symbol, "error": str(exc)[:200]})

    # Close the snapshot only when nothing exited remains (all items re-entered).
    remaining = db.execute(
        select(SoloExitItem).where(SoloExitItem.snapshot_id == snap.id)
    ).scalars().all()
    if not remaining:
        snap.reentered_at = datetime.now(timezone.utc)
    db.commit()
    return {"snapshot_id": str(snap.id), "mode": mode, "placed_count": len(placed),
            "failed_count": len(failed), "placed": placed, "failed": failed}
