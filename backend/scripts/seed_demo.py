"""Seed data for the queue-fanout demo.

Creates (idempotently):
  - 1 trader  (demo-trader@optionhaven.test / demo1234) with trading enabled
  - N subscribers (demo-sub-XX@optionhaven.test / demo1234) each:
      * following the trader, copy_enabled=True, multiplier=1.0
      * one MOCK broker account (simulated 200-400ms latency, no real keys)

Then optionally fires one trader order through queue_fanout so the
/admin/demo dashboard has live data to render.

Usage (inside the backend container or any env with DATABASE_URL set):

    python -m scripts.seed_demo --subscribers 100
    python -m scripts.seed_demo --subscribers 100 --fire-order
    python -m scripts.seed_demo --reset        # delete all demo-*@optionhaven.test users

The mock broker accepts every order (with ~3% simulated failures), so this
needs NO real broker credentials — it exists purely to exercise the queue
+ worker pool + dashboard end to end.
"""
from __future__ import annotations

import argparse
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.brokers import MockAdapter  # noqa: F401 — ensures adapter import path ok
from app.core.security import hash_password
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType, Order, OrderSide, OrderStatus, OrderType,
)
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services.crypto import encrypt_json

DEMO_DOMAIN = "optionhaven.test"
TRADER_EMAIL = f"demo-trader@{DEMO_DOMAIN}"
PASSWORD = "demo1234"


def _get_or_create_trader(db) -> User:
    trader = db.execute(select(User).where(User.email == TRADER_EMAIL)).scalar_one_or_none()
    if trader is None:
        trader = User(
            email=TRADER_EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.TRADER,
            display_name="Demo Trader",
        )
        db.add(trader)
        db.flush()
    ts = db.get(TraderSettings, trader.id)
    if ts is None:
        ts = TraderSettings(user_id=trader.id, trading_enabled=True, copy_paused=False)
        db.add(ts)
    else:
        ts.trading_enabled = True
        ts.copy_paused = False
    return trader


def _mock_account(user_id: uuid.UUID, label: str) -> BrokerAccount:
    return BrokerAccount(
        user_id=user_id,
        broker=BrokerName.MOCK,
        label=label,
        is_paper=True,
        supports_fractional=True,
        encrypted_credentials=encrypt_json({"mock": True}),
        broker_account_number="MOCK-0000",
        connection_status="connected",
    )


def seed(subscribers: int, fire_order: bool) -> None:
    with SessionLocal() as db:
        trader = _get_or_create_trader(db)
        db.flush()

        created = 0
        for i in range(subscribers):
            email = f"demo-sub-{i:03d}@{DEMO_DOMAIN}"
            sub = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if sub is None:
                sub = User(
                    email=email,
                    password_hash=hash_password(PASSWORD),
                    role=UserRole.SUBSCRIBER,
                    display_name=f"Demo Sub {i:03d}",
                )
                db.add(sub)
                db.flush()
                created += 1

            ss = db.get(SubscriberSettings, sub.id)
            if ss is None:
                ss = SubscriberSettings(
                    user_id=sub.id,
                    following_trader_id=trader.id,
                    copy_enabled=True,
                    multiplier=Decimal("1.000"),
                )
                db.add(ss)
            else:
                ss.following_trader_id = trader.id
                ss.copy_enabled = True

            # Ensure exactly one mock broker account.
            has_acct = db.execute(
                select(BrokerAccount.id).where(BrokerAccount.user_id == sub.id)
            ).first()
            if not has_acct:
                db.add(_mock_account(sub.id, "Demo Mock Broker"))

        db.commit()
        print(f"Seed complete: trader={TRADER_EMAIL}, "
              f"{subscribers} subscribers ({created} newly created). "
              f"Password for all: {PASSWORD}")

        if fire_order:
            _fire_order(db, trader)


def _fire_order(db, trader: User) -> None:
    """Create one trader order and run it through the queue-based fanout so
    the dashboard fills with pending_copies rows. Reloads the memory cache
    first so freshly-seeded subscribers are visible to queue_fanout."""
    from app.services import copy_engine, memory_cache

    memory_cache.load_all()

    order = Order(
        user_id=trader.id,
        broker_account_id=_ensure_trader_account(db, trader),
        instrument_type=InstrumentType.STOCK,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        status=OrderStatus.SUBMITTED,
        fanned_out_to_subscribers=True,
    )
    db.add(order)
    db.commit()

    queued = copy_engine.queue_fanout(db, order, trader)
    print(f"Fired trader order {order.id} → queued {queued} pending_copies. "
          f"Open /admin/demo to watch the workers drain them.")


def _ensure_trader_account(db, trader: User) -> uuid.UUID:
    acct_id = db.execute(
        select(BrokerAccount.id).where(BrokerAccount.user_id == trader.id)
    ).scalar_one_or_none()
    if acct_id:
        return acct_id
    acct = _mock_account(trader.id, "Demo Trader Mock Broker")
    db.add(acct)
    db.flush()
    return acct.id


def reset() -> None:
    with SessionLocal() as db:
        users = db.execute(
            select(User).where(User.email.like(f"demo-%@{DEMO_DOMAIN}"))
        ).scalars().all()
        # ON DELETE CASCADE on broker_accounts/settings/orders handles children.
        for u in users:
            db.delete(u)
        db.commit()
        print(f"Reset: deleted {len(users)} demo users (and cascaded data).")


def main() -> None:
    p = argparse.ArgumentParser(description="Seed queue-demo data.")
    p.add_argument("--subscribers", type=int, default=100,
                   help="number of demo subscribers to create (default 100)")
    p.add_argument("--fire-order", action="store_true",
                   help="also create + fan out one trader order")
    p.add_argument("--reset", action="store_true",
                   help="delete all demo-*@optionhaven.test users and exit")
    args = p.parse_args()

    if args.reset:
        reset()
        return
    seed(args.subscribers, args.fire_order)


if __name__ == "__main__":
    main()
