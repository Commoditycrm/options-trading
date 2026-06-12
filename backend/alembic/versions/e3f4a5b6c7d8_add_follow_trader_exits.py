"""add subscriber_settings.follow_trader_exits

When True (default), the subscriber mirrors the trader's position exits
(manual close or SL/TP cascade). False = subscriber manages own exits.
Defaults to true so existing behaviour (closes fan out) is preserved.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS follow_trader_exits BOOLEAN NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS follow_trader_exits"
    )
