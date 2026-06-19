"""add solo auto re-enter: trader_settings.solo_reenter_pct + items.auto_reenter_armed

solo_reenter_pct: when set, a solo trader's exited positions are auto re-entered
once price moves this % favorably from the exit (long → buy back on a dip,
short → re-short on a rise). NULL = off.
auto_reenter_armed: per exit item, marks it for the position_monitor's auto
re-enter pass. Idempotent column adds.

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "n1a2b3c4d5e6"
down_revision: Union[str, None] = "m1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trader_settings "
        "ADD COLUMN IF NOT EXISTS solo_reenter_pct NUMERIC(6, 3)"
    )
    op.execute(
        "ALTER TABLE solo_exit_items "
        "ADD COLUMN IF NOT EXISTS auto_reenter_armed BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE solo_exit_items DROP COLUMN IF EXISTS auto_reenter_armed")
    op.execute("ALTER TABLE trader_settings DROP COLUMN IF EXISTS solo_reenter_pct")
