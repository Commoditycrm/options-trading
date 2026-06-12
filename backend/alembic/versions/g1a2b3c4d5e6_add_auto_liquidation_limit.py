"""add subscriber_settings.auto_liquidation_limit (Req #12)

Absolute $ equity floor. When a subscriber's LIVE account equity falls to/at-or-
below this amount, the position_monitor liquidates ALL open positions at market
and flips copy_enabled off until the subscriber manually re-enables copy. NULL =
disabled. Complements the percentage-based risk limits (daily_loss_limit_pct).

Revision ID: g1a2b3c4d5e6
Revises: f4a5b6c7d8e9
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "g1a2b3c4d5e6"
down_revision: Union[str, None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS auto_liquidation_limit NUMERIC(20, 2)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS auto_liquidation_limit"
    )
