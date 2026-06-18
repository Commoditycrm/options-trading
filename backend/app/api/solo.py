"""Solo-trader toolset: 3-way Exit All, post-exit simulation, and Re-enter All.

A solo trader (TraderSettings.solo_mode) trades only for himself. These endpoints
let him flatten everything at a chosen price (market / bid / ask), watch a live
"what-if" simulation of what he exited, and re-enter the same set.

Bid/ask pricing and the simulation use the Alpaca market-data quotes; if the feed
isn't entitled they degrade gracefully (bid/ask falls back to market; the
simulation shows null marks).
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_trader
from app.api.trades import _place_trader_order
from app.brokers import adapter_for
from app.brokers.alpaca import build_occ_symbol
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.models.solo import SoloExitItem, SoloExitSnapshot
from app.models.user import User
from app.schemas.order import PlaceOrderIn
from app.services.crypto import decrypt_json

router = APIRouter(prefix="/api/solo", tags=["solo"])


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
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Flatten every open position across the trader's connected brokers at the
    chosen price (market / bid / ask) and record a snapshot for the simulation +
    re-enter. Bid/ask fall back to market when no quote is available."""
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == trader.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    snapshot = SoloExitSnapshot(user_id=trader.id, exit_mode=mode)
    db.add(snapshot)
    db.flush()

    closed: list[dict] = []
    failed: list[dict] = []
    for acct in accts:
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            positions = [p for p in adapter.get_positions() if p.quantity != 0]
        except Exception as exc:  # noqa: BLE001
            failed.append({"broker_account_id": str(acct.id), "error": str(exc)[:200]})
            continue

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
            if mode in ("bid", "ask"):
                q = _quote(adapter, pos.instrument_type.value, pos.symbol, occ)
                px = q.get(mode)
                if px is not None:
                    order_type = OrderType.LIMIT
                    limit_price = px
                    exit_ref = px
            if exit_ref is None:
                # market (or bid/ask with no quote): use mid/last as the sim reference
                q = _quote(adapter, pos.instrument_type.value, pos.symbol, occ)
                exit_ref = q.get("mid")

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
            try:
                order = _place_trader_order(
                    db, trader, payload, acct.id, background, request,
                    skip_fanout=True, is_closing=True,
                )
                db.add(SoloExitItem(
                    snapshot_id=snapshot.id,
                    broker_account_id=acct.id,
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
    items_out: list[dict] = []
    for it in snap.items:
        mid = None
        if adapter is not None:
            mid = _quote(adapter, it.instrument_type, it.symbol, it.occ_symbol).get("mid")
        unit = Decimal(100) if it.instrument_type == "option" else Decimal(1)
        sign = Decimal(1) if it.original_side == "buy" else Decimal(-1)
        pnl_if_held = None
        if mid is not None and it.exit_price is not None:
            pnl_if_held = (Decimal(str(mid)) - it.exit_price) * it.quantity * unit * sign
        items_out.append({
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
        "quotes_available": adapter is not None,
    }


@router.post("/reenter-all")
def reenter_all(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Re-open every position from the latest exit snapshot (market orders,
    original side + qty), then mark the snapshot re-entered."""
    from datetime import datetime, timezone

    snap = _latest_open_snapshot(db, trader.id)
    if snap is None:
        raise HTTPException(404, "no_open_snapshot")

    placed: list[dict] = []
    failed: list[dict] = []
    for it in snap.items:
        is_option = it.instrument_type == "option"
        expiry = strike = right = None
        if is_option and it.occ_symbol:
            from app.brokers.alpaca import _parse_occ
            parsed = _parse_occ(it.occ_symbol)
            if parsed:
                _, expiry, strike, right = parsed  # right is OptionRight
        payload = PlaceOrderIn(
            instrument_type=InstrumentType(it.instrument_type),
            symbol=it.symbol,
            side=OrderSide(it.original_side),
            order_type=OrderType.MARKET,
            quantity=it.quantity,
            limit_price=None,
            stop_price=None,
            option_expiry=expiry,
            option_strike=strike,
            option_right=right,
        )
        try:
            order = _place_trader_order(
                db, trader, payload, it.broker_account_id, background, request,
                skip_fanout=True, is_closing=False,
            )
            placed.append({"symbol": it.symbol, "qty": str(it.quantity), "order_id": str(order.id)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": it.symbol, "error": str(exc)[:200]})

    snap.reentered_at = datetime.now(timezone.utc)
    db.commit()
    return {"snapshot_id": str(snap.id), "placed_count": len(placed),
            "failed_count": len(failed), "placed": placed, "failed": failed}
