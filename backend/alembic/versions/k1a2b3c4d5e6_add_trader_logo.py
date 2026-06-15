"""add trader_settings.logo (per-trader white-label logo)

Base64 data URL stored in the DB (the container disk is ephemeral across
deploys; Postgres persists). Read lazily (deferred on the model) so the fan-out
hot path never loads it. Idempotent Text column, no enums.

Revision ID: k1a2b3c4d5e6
Revises: j1a2b3c4d5e6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "k1a2b3c4d5e6"
down_revision: Union[str, None] = "j1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trader_settings ADD COLUMN IF NOT EXISTS logo TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE trader_settings DROP COLUMN IF EXISTS logo")
