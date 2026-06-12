"""add subscriber_settings.daily_profit_limit_pct

Daily profit target as % of equity — auto-pauses copy when reached (mirror of
daily_loss_limit_pct).

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS daily_profit_limit_pct NUMERIC(6, 3)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS daily_profit_limit_pct"
    )
