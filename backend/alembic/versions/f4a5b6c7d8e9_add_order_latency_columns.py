"""add orders.broker_ms + orders.fanout_published_at (latency instrumentation)

broker_ms = duration of the broker place_order call for this order.
fanout_published_at = when fan-out was enqueued (on the parent order), so the
Performance page can split per-subscriber platform-vs-broker latency.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS broker_ms INTEGER")
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS fanout_published_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS fanout_published_at")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS broker_ms")
