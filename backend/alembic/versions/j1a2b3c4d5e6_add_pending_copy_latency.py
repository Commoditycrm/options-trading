"""add pending_copies.pickup_ms + platform_ms (latency split)

pickup_ms   = queued_at → picked_up_at (claim wait; ~0 with LISTEN/NOTIFY)
platform_ms = our processing only = queue_to_broker_ms − broker_ms (the < 50ms
              metric; the broker round-trip is excluded as external).
Idempotent plain-integer columns — no enums.

Revision ID: j1a2b3c4d5e6
Revises: i1a2b3c4d5e6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "j1a2b3c4d5e6"
down_revision: Union[str, None] = "i1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE pending_copies ADD COLUMN IF NOT EXISTS pickup_ms INTEGER")
    op.execute("ALTER TABLE pending_copies ADD COLUMN IF NOT EXISTS platform_ms INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE pending_copies DROP COLUMN IF EXISTS platform_ms")
    op.execute("ALTER TABLE pending_copies DROP COLUMN IF EXISTS pickup_ms")
