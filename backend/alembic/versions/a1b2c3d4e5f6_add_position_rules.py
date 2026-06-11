"""add position_rules table for position-level SL/TP

Per-position stop-loss / take-profit rules, monitored by
services.position_monitor which auto-closes the position when a threshold
price is crossed.

Revision ID: a1b2c3d4e5f6
Revises: f9e8d7c6b5a4
Create Date: 2026-06-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f9e8d7c6b5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    status_enum = postgresql.ENUM(
        "active", "triggered", "cancelled", name="position_rule_status",
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "position_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "broker_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("broker_symbol", sa.String(length=64), nullable=False),
        sa.Column("take_profit_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("stop_loss_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("entry_price", sa.Numeric(20, 6), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "triggered", "cancelled",
                name="position_rule_status", create_type=False,
            ),
            nullable=False, server_default="active",
        ),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_position_rules_user_id", "position_rules", ["user_id"])
    op.create_index("ix_position_rules_broker_account_id", "position_rules", ["broker_account_id"])
    op.create_index("ix_position_rules_status", "position_rules", ["status"])


def downgrade() -> None:
    op.drop_index("ix_position_rules_status", table_name="position_rules")
    op.drop_index("ix_position_rules_broker_account_id", table_name="position_rules")
    op.drop_index("ix_position_rules_user_id", table_name="position_rules")
    op.drop_table("position_rules")
    postgresql.ENUM(name="position_rule_status").drop(op.get_bind(), checkfirst=True)
