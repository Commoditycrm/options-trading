"""orders.broker_account_id: CASCADE -> SET NULL (stop trade-history loss)

Disconnecting a broker account was CASCADE-deleting every order (and its
fills) placed on it — silently wiping the user's trade and P&L history.
The brokers API docstring always claimed "Order rows survive (SET NULL)";
this migration makes the schema match: the column becomes nullable and the
FK is recreated with ON DELETE SET NULL.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres auto-generated name (the FK was created unnamed in the initial
# schema migration 36f268704ea8).
_FK_NAME = "orders_broker_account_id_fkey"


def upgrade() -> None:
    op.alter_column(
        "orders", "broker_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_constraint(_FK_NAME, "orders", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME, "orders", "broker_accounts",
        ["broker_account_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "orders", type_="foreignkey")
    # Orphaned rows (NULL) block restoring NOT NULL; delete them first —
    # this mirrors what CASCADE would have done at disconnect time.
    op.execute("DELETE FROM orders WHERE broker_account_id IS NULL")
    op.create_foreign_key(
        _FK_NAME, "orders", "broker_accounts",
        ["broker_account_id"], ["id"], ondelete="CASCADE",
    )
    op.alter_column(
        "orders", "broker_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
