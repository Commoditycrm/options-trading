"""add trade features: excluded_symbols, mirror_only_filled, default_broker,
take_profit_pct, stop_loss_pct

Covers all 5 new requirements in one migration:
  - subscriber_settings.excluded_symbols  TEXT[]   (req #6 exclusion list)
  - trader_settings.mirror_only_filled    BOOL     (req #3 filled-only toggle)
  - trader_settings.default_broker_account_id UUID (req #1 Option B default)
  - subscriber_settings.take_profit_pct  NUMERIC  (req #4 auto TP)
  - subscriber_settings.stop_loss_pct    NUMERIC  (req #4 auto SL)

Revision ID: e2f3a4b5c601
Revises: d5e6f7a8b902
Create Date: 2026-05-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e2f3a4b5c601"
down_revision: Union[str, None] = "d5e6f7a8b902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── subscriber_settings ───────────────────────────────────────────────
    op.add_column("subscriber_settings", sa.Column(
        "excluded_symbols",
        postgresql.ARRAY(sa.Text()),
        nullable=False,
        server_default="{}",
    ))
    op.add_column("subscriber_settings", sa.Column(
        "take_profit_pct",
        sa.Numeric(6, 3),
        nullable=True,
    ))
    op.add_column("subscriber_settings", sa.Column(
        "stop_loss_pct",
        sa.Numeric(6, 3),
        nullable=True,
    ))

    # ── trader_settings ───────────────────────────────────────────────────
    op.add_column("trader_settings", sa.Column(
        "mirror_only_filled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ))
    op.add_column("trader_settings", sa.Column(
        "default_broker_account_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("trader_settings", "default_broker_account_id")
    op.drop_column("trader_settings", "mirror_only_filled")
    op.drop_column("subscriber_settings", "stop_loss_pct")
    op.drop_column("subscriber_settings", "take_profit_pct")
    op.drop_column("subscriber_settings", "excluded_symbols")
