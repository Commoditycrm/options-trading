"""add broker_accounts.brokerage_name (real brokerage for SnapTrade)

The SnapTrade finish flow already captures the underlying brokerage name
(e.g. "Webull") but only stored it inside the encrypted creds blob. Persist
it as a queryable column so the UI can show the real broker instead of the
generic "SnapTrade".

Revision ID: c1d2e3f4a5b6
Revises: b7c8d9e0f1a2
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS keeps it idempotent even if a prior partial run added it.
    op.execute(
        "ALTER TABLE broker_accounts "
        "ADD COLUMN IF NOT EXISTS brokerage_name VARCHAR(120)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE broker_accounts DROP COLUMN IF EXISTS brokerage_name")
