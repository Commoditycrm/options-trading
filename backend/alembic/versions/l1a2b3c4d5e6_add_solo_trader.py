"""add solo-trader: trader_settings.solo_mode + exit snapshot tables

solo_mode: admin-set flag — trader trades only for himself (no fan-out) and gets
the solo exit/simulation/re-enter toolset.
solo_exit_snapshots / solo_exit_items: capture each "Exit All" so we can show the
post-exit simulation and re-enter the same set. String enum-likes (no PG ENUMs).
Idempotent column add; plain create_table (new tables).

Revision ID: l1a2b3c4d5e6
Revises: k1a2b3c4d5e6
Create Date: 2026-06-15 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "l1a2b3c4d5e6"
down_revision: Union[str, None] = "k1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trader_settings "
        "ADD COLUMN IF NOT EXISTS solo_mode BOOLEAN NOT NULL DEFAULT false"
    )
    op.create_table(
        "solo_exit_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exit_mode", sa.String(length=8), nullable=False),
        sa.Column("reentered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_solo_exit_snapshots_user_id", "solo_exit_snapshots", ["user_id"])
    op.create_table(
        "solo_exit_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("solo_exit_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_type", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("occ_symbol", sa.String(length=32), nullable=True),
        sa.Column("original_side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_solo_exit_items_snapshot_id", "solo_exit_items", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_solo_exit_items_snapshot_id", table_name="solo_exit_items")
    op.drop_table("solo_exit_items")
    op.drop_index("ix_solo_exit_snapshots_user_id", table_name="solo_exit_snapshots")
    op.drop_table("solo_exit_snapshots")
    op.execute("ALTER TABLE trader_settings DROP COLUMN IF EXISTS solo_mode")
