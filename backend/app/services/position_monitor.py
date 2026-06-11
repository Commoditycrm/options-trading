"""Background monitor for position-level stop-loss / take-profit.

Reuses the existing poller pattern (external_trade_poller / retry_scheduler):
a sync loop started from main.py via ``run_in_executor``. Every
POLL_INTERVAL_SEC it loads the broker accounts that have ACTIVE
``position_rules``, fetches their live positions ONCE per account (the same
``adapter.get_positions()`` snapshot the Positions page uses — current_price
included), and for each rule whose take-profit or stop-loss price has been
crossed, places a reverse MARKET order to flatten the position on the OWNER's
account, flips the rule to TRIGGERED, audits, pushes an SSE event, and drops a
persistent notification.

No new market-data feed and no extra listeners: current price comes from the
broker position snapshot, and the only accounts polled are those with an
active rule. Each user manages their own position exits, so there is no
trader→subscriber fanout here (unlike copy_engine).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.position_rule import PositionRule, PositionRuleStatus
from app.services import audit, events
from app.services.copy_engine import _order_event
from app.services.crypto import decrypt_json
from app.services.notifications import create_notification
from app.services.order_retry import place_order_with_recovery

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10


def _crossed(rule: PositionRule, pos: Any) -> str | None:
    """Return 'take_profit' / 'stop_loss' if the position's current price has
    crossed the corresponding threshold, else None. For a long position TP is
    above and SL below entry; for a short the comparisons invert."""
    price = pos.current_price
    if price is None:
        return None
    is_long = pos.quantity > 0
    if is_long:
        if rule.take_profit_price is not None and price >= rule.take_profit_price:
            return "take_profit"
        if rule.stop_loss_price is not None and price <= rule.stop_loss_price:
            return "stop_loss"
    else:
        if rule.take_profit_price is not None and price <= rule.take_profit_price:
            return "take_profit"
        if rule.stop_loss_price is not None and price >= rule.stop_loss_price:
            return "stop_loss"
    return None


def _place_exit(db, acct: BrokerAccount, adapter, pos: Any, rule: PositionRule, reason: str) -> str:
    """Place a reverse MARKET order to flatten the position, record an Order
    row, flip the rule to TRIGGERED, audit + notify. Returns an outcome tag."""
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    qty = abs(pos.quantity)

    order = Order(
        user_id=acct.user_id,
        broker_account_id=acct.id,
        instrument_type=pos.instrument_type,
        symbol=pos.symbol,
        option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
        option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
        option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
        side=reverse_side,
        order_type=OrderType.MARKET,
        quantity=qty,
        status=OrderStatus.PENDING,
        is_closing=True,
    )
    db.add(order)
    db.flush()

    request = BrokerOrderRequest(
        instrument_type=order.instrument_type,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        quantity=order.quantity,
        limit_price=None,
        stop_price=None,
        option_expiry=order.option_expiry,
        option_strike=order.option_strike,
        option_right=order.option_right,
        client_order_id=str(order.id),
    )

    now = datetime.now(timezone.utc)
    try:
        resp = place_order_with_recovery(adapter, request)
    except Exception as exc:  # noqa: BLE001
        order.status = OrderStatus.REJECTED
        order.reject_reason = str(exc)[:480]
        order.closed_at = now
        rule.status = PositionRuleStatus.TRIGGERED
        rule.triggered_at = now
        rule.detail = f"{reason}: exit order failed: {str(exc)[:200]}"
        audit.record(
            db, actor_user_id=acct.user_id, action="position.sl_tp_exit_failed",
            entity_type="position_rule", entity_id=rule.id,
            metadata={"reason": reason, "error": str(exc)[:300], "broker_symbol": rule.broker_symbol},
        )
        db.commit()
        events.publish(acct.user_id, _order_event("order.updated", order))
        return "exit_failed"

    order.status = resp.status
    order.broker_order_id = resp.broker_order_id
    order.submitted_at = resp.submitted_at
    order.filled_quantity = resp.filled_quantity
    order.filled_avg_price = resp.filled_avg_price
    rule.status = PositionRuleStatus.TRIGGERED
    rule.triggered_at = now
    rule.detail = f"{reason} hit; closed {qty} {rule.broker_symbol}"
    audit.record(
        db, actor_user_id=acct.user_id, action="position.sl_tp_triggered",
        entity_type="position_rule", entity_id=rule.id,
        metadata={
            "reason": reason, "broker_symbol": rule.broker_symbol, "qty": str(qty),
            "order_id": str(order.id), "broker_order_id": resp.broker_order_id,
        },
    )
    label = "Take-profit" if reason == "take_profit" else "Stop-loss"
    create_notification(
        db, user_id=acct.user_id, type="position.sl_tp_triggered",
        message=f"{label} hit on {rule.broker_symbol} — closed {qty} at market.",
        metadata={
            "broker_symbol": rule.broker_symbol, "reason": reason,
            "qty": str(qty), "order_id": str(order.id),
        },
    )
    db.commit()
    events.publish(acct.user_id, _order_event("order.updated", order))
    return "triggered"


def _check_account(account_id) -> None:
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, account_id)
        if acct is None or acct.connection_status != "connected":
            return
        rules = list(db.execute(
            select(PositionRule).where(
                PositionRule.broker_account_id == account_id,
                PositionRule.status == PositionRuleStatus.ACTIVE,
            )
        ).scalars())
        if not rules:
            return

        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            positions = adapter.get_positions()
        except Exception:  # noqa: BLE001
            log.exception("position_monitor: failed to fetch positions for acct=%s", account_id)
            return

        by_symbol = {p.broker_symbol.upper(): p for p in positions}
        for rule in rules:
            pos = by_symbol.get(rule.broker_symbol.upper())
            if pos is None or pos.quantity == 0:
                # Position closed elsewhere — retire the rule.
                rule.status = PositionRuleStatus.CANCELLED
                rule.detail = "position no longer open"
                db.commit()
                continue
            reason = _crossed(rule, pos)
            if reason is None:
                continue
            try:
                outcome = _place_exit(db, acct, adapter, pos, rule, reason)
                log.info("position_monitor: rule=%s %s outcome=%s", rule.id, reason, outcome)
            except Exception:  # noqa: BLE001
                log.exception("position_monitor: exit failed for rule=%s", rule.id)
                db.rollback()


# ── monitor loop ─────────────────────────────────────────────────────────────

_LAST_HEARTBEAT: dict[str, Any] = {"at": None}


def heartbeat_status() -> dict[str, Any]:
    last = _LAST_HEARTBEAT.get("at")
    if last is None:
        return {"running": False, "last_run_at": None, "seconds_since": None}
    delta = (datetime.now(timezone.utc) - last).total_seconds()
    return {
        "running": True,
        "last_run_at": last.isoformat(),
        "seconds_since": round(delta, 1),
        "healthy": delta < POLL_INTERVAL_SEC * 3,
    }


def poll_loop(shutdown_check=None) -> None:
    """Long-running loop. Every POLL_INTERVAL_SEC, polls only the broker
    accounts that currently have an ACTIVE position rule."""
    log.info("position_monitor: starting (interval=%ss)", POLL_INTERVAL_SEC)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("position_monitor: shutdown requested, exiting")
            return

        _LAST_HEARTBEAT["at"] = datetime.now(timezone.utc)
        try:
            with SessionLocal() as db:
                account_ids = list(db.execute(
                    select(PositionRule.broker_account_id).where(
                        PositionRule.status == PositionRuleStatus.ACTIVE,
                    ).distinct()
                ).scalars())
            for account_id in account_ids:
                try:
                    _check_account(account_id)
                except Exception:  # noqa: BLE001
                    log.exception("position_monitor: error on acct=%s", account_id)
        except Exception:  # noqa: BLE001
            log.exception("position_monitor: poll iteration failed")

        try:
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("position_monitor: KeyboardInterrupt, exiting")
            return
