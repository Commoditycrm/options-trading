"""Mock broker adapter — for the queue demo only.

Simulates a real broker's order-placement latency (a random 200-400ms
network + matching-engine delay) WITHOUT any real account or credentials.
Lets the demo dashboard show realistic per-subscriber broker timing bars
while the 100-worker pool runs against fake accounts.

Never use this for anything but the demo: it always "accepts" the order.
"""
from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timezone

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    ConnectionInfo,
)
from app.models.order import OrderStatus

# Tunable via the demo seed. Matches the 200-400ms target in the demo brief.
MIN_LATENCY_MS = 200
MAX_LATENCY_MS = 400


class MockAdapter(BrokerAdapter):
    name = "mock"

    def verify_connection(self) -> ConnectionInfo:
        return ConnectionInfo(
            broker_account_id="mock-account",
            supports_fractional=True,
            extra={"mock": True},
        )

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        # Simulate broker round-trip latency so the dashboard's broker bar
        # has realistic width. Blocking sleep is fine — the worker runs this
        # inside run_in_executor, so the event loop isn't blocked.
        latency = random.uniform(MIN_LATENCY_MS, MAX_LATENCY_MS) / 1000
        time.sleep(latency)
        # Occasionally fail (~3%) so the dashboard shows a red bar / failed
        # count, demonstrating per-subscriber error isolation.
        if random.random() < 0.03:
            raise RuntimeError("mock broker: simulated transient rejection")
        return BrokerOrderResult(
            broker_order_id=f"mock-{uuid.uuid4().hex[:12]}",
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=req.quantity,
            filled_avg_price=req.limit_price,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
        )
