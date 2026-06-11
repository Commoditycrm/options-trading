"""Open positions — currently held shares/contracts across the trader's broker
accounts.

GET  /api/positions               aggregates positions across every connected
                                  broker account for the caller.
POST /api/positions/{symbol}/close
                                  places a reverse-side order to flatten the
                                  named position. Routes through the same
                                  _place_trader_order flow as a regular order
                                  so it audits, fans out to subscribers, and
                                  publishes an SSE event.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.api.trades import _place_trader_order
from app.brokers import adapter_for
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType, Order, OrderSide, OrderType
from app.models.position_rule import PositionRule, PositionRuleStatus
from app.models.user import User
from app.schemas.order import OrderOut, PlaceOrderIn
from app.schemas.position import ClosePositionIn, PositionOut, PositionRuleOut, SetSLTPIn
from app.services import audit
from app.services.crypto import decrypt_json

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
def list_positions(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[PositionOut]:
    """Return positions across every connected broker account for the caller.

    A position appears once per (broker_account, symbol). Disconnected accounts
    are skipped silently. Per-account broker failures are skipped silently too —
    we don't want one flaky broker to break the whole list.
    """
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    out: list[PositionOut] = []
    for acct in accts:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            for p in adapter.get_positions():
                out.append(PositionOut(
                    broker_account_id=acct.id,
                    broker_symbol=p.broker_symbol,
                    symbol=p.symbol,
                    instrument_type=p.instrument_type,
                    quantity=p.quantity,
                    avg_entry_price=p.avg_entry_price,
                    current_price=p.current_price,
                    market_value=p.market_value,
                    unrealized_pnl=p.unrealized_pnl,
                    cost_basis=p.cost_basis,
                    option_expiry=p.option_expiry,
                    option_strike=p.option_strike,
                    option_right=p.option_right,
                ))
        except Exception:  # noqa: BLE001
            # Best-effort: one broker's outage shouldn't blank the whole table.
            continue
    return out


@router.post("/close-all")
def close_all_positions(
    request: Request,
    background: BackgroundTasks,
    include_subscribers: bool = Query(
        default=True,
        description="When false, suppress the trader→subscriber fanout. Only the caller's own positions are closed. No-op semantic when caller is a subscriber.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Flatten every open position across the caller's connected broker
    accounts by placing a market reverse order for each. For traders this
    normally fans out to subscribers; pass `include_subscribers=false` to
    close only the trader's own positions without propagating. Per-position
    failures don't abort the rest — we return a per-position result list.
    """
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    closed: list[dict] = []
    failed: list[dict] = []
    skip_fanout = not include_subscribers

    for acct in accts:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            positions = adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            failed.append({
                "broker_account_id": str(acct.id),
                "symbol": None,
                "error": f"could not list positions: {exc}"[:300],
            })
            continue

        for pos in positions:
            if pos.quantity == 0:
                continue
            reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
            qty = abs(pos.quantity)
            payload = PlaceOrderIn(
                instrument_type=pos.instrument_type,
                symbol=pos.symbol,
                side=reverse_side,
                order_type=OrderType.MARKET,
                quantity=qty,
                limit_price=None,
                stop_price=None,
                option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
                option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
                option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
            )
            try:
                order = _place_trader_order(
                    db, user, payload, acct.id, background, request,
                    skip_fanout=skip_fanout,
                    is_closing=True,   # close-all is closing, always
                )
                closed.append({
                    "broker_account_id": str(acct.id),
                    "symbol": pos.symbol,
                    "qty": str(qty),
                    "side": reverse_side.value,
                    "order_id": str(order.id),
                })
            except Exception as exc:  # noqa: BLE001
                failed.append({
                    "broker_account_id": str(acct.id),
                    "symbol": pos.symbol,
                    "error": str(exc)[:300],
                })

    return {"closed": closed, "failed": failed, "closed_count": len(closed), "failed_count": len(failed)}


@router.post("/{broker_symbol}/close", response_model=OrderOut)
def close_position(
    broker_symbol: str,
    payload: ClosePositionIn,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Place a reverse-side order to close the position on the given account.

    `broker_symbol` is the broker's canonical id — OCC for options, plain
    ticker for stocks — which uniquely identifies a position even when the
    same root (e.g. AAPL stock + AAPL option) is held simultaneously.

    Re-reads the live position from the broker so the close size and side are
    based on what actually exists right now, not stale client data. For a
    trader this fans out to subscribers; for a subscriber it just runs
    against their own broker.
    """
    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")

    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    positions = adapter.get_positions()

    target = broker_symbol.upper()
    pos = next((p for p in positions if p.broker_symbol.upper() == target), None)
    if pos is None or pos.quantity == 0:
        raise HTTPException(404, "position_not_found")

    # Reverse the side based on the current holding (long → sell, short → buy).
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    full_qty = abs(pos.quantity)
    close_qty = payload.quantity if payload.quantity is not None else full_qty
    if close_qty <= 0:
        raise HTTPException(422, "quantity_must_be_positive")
    if close_qty > full_qty:
        raise HTTPException(422, "quantity_exceeds_position")

    # For options, _place_trader_order rebuilds the OCC symbol from
    # (expiry, strike, right), so we pass the bare root in `symbol`.
    new_payload = PlaceOrderIn(
        instrument_type=pos.instrument_type,
        symbol=pos.symbol,
        side=reverse_side,
        order_type=payload.order_type,
        quantity=close_qty,
        limit_price=payload.limit_price,
        stop_price=None,
        option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
        option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
        option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
    )

    # Single-position close — same is_closing semantics as close-all.
    return _place_trader_order(
        db, user, new_payload, acct.id, background, request,
        is_closing=True,
    )


# ─── Position-level stop-loss / take-profit ──────────────────────────────────

@router.get("/sl-tp", response_model=list[PositionRuleOut])
def list_sl_tp(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[PositionRule]:
    """List the caller's stop-loss / take-profit rules (active + triggered)."""
    return list(db.execute(
        select(PositionRule).where(
            PositionRule.user_id == user.id,
            PositionRule.status != PositionRuleStatus.CANCELLED,
        ).order_by(PositionRule.created_at.desc())
    ).scalars())


@router.post("/sl-tp", response_model=PositionRuleOut)
def set_sl_tp(
    payload: SetSLTPIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> PositionRule:
    """Set (upsert) a stop-loss / take-profit rule on one open position the
    caller owns. The position_monitor poller auto-closes the position when a
    threshold price is crossed. Percentage inputs are resolved to prices using
    the position's live entry price and side."""
    acct = db.get(BrokerAccount, payload.broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")

    # Validate the position exists and capture entry price + side.
    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    target = payload.broker_symbol.upper()
    pos = next(
        (p for p in adapter.get_positions() if p.broker_symbol.upper() == target),
        None,
    )
    if pos is None or pos.quantity == 0:
        raise HTTPException(404, "position_not_found")

    entry = pos.avg_entry_price
    is_long = pos.quantity > 0

    tp = payload.take_profit_price
    sl = payload.stop_loss_price
    # Resolve percentage inputs to absolute prices. TP is favourable, SL adverse:
    # long → TP above / SL below entry; short → TP below / SL above.
    if tp is None and payload.take_profit_pct is not None:
        if entry is None:
            raise HTTPException(422, "entry_price_unavailable_for_pct")
        tp = entry * (1 + (payload.take_profit_pct / 100) * (1 if is_long else -1))
    if sl is None and payload.stop_loss_pct is not None:
        if entry is None:
            raise HTTPException(422, "entry_price_unavailable_for_pct")
        sl = entry * (1 - (payload.stop_loss_pct / 100) * (1 if is_long else -1))
    if tp is not None:
        tp = tp.quantize(Decimal("0.000001"))
    if sl is not None:
        sl = sl.quantize(Decimal("0.000001"))

    existing = db.execute(
        select(PositionRule).where(
            PositionRule.broker_account_id == acct.id,
            PositionRule.broker_symbol == target,
            PositionRule.status == PositionRuleStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.take_profit_price = tp
        existing.stop_loss_price = sl
        existing.entry_price = entry
        rule = existing
    else:
        rule = PositionRule(
            user_id=user.id,
            broker_account_id=acct.id,
            broker_symbol=target,
            take_profit_price=tp,
            stop_loss_price=sl,
            entry_price=entry,
            status=PositionRuleStatus.ACTIVE,
        )
        db.add(rule)
    db.flush()
    audit.record(
        db, actor_user_id=user.id, action="position.sl_tp_set",
        entity_type="position_rule", entity_id=rule.id,
        metadata={
            "broker_symbol": target,
            "take_profit_price": str(tp) if tp is not None else None,
            "stop_loss_price": str(sl) if sl is not None else None,
        },
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/sl-tp/{rule_id}")
def cancel_sl_tp(
    rule_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Cancel a stop-loss / take-profit rule the caller owns."""
    rule = db.get(PositionRule, rule_id)
    if not rule or rule.user_id != user.id:
        raise HTTPException(404, "rule_not_found")
    rule.status = PositionRuleStatus.CANCELLED
    audit.record(
        db, actor_user_id=user.id, action="position.sl_tp_cancelled",
        entity_type="position_rule", entity_id=rule.id,
    )
    db.commit()
    return {"ok": True}
