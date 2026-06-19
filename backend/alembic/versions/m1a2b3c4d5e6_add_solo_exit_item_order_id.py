"""add solo_exit_items.order_id — link an exited contract to its closing Order

Lets the /solo page show each exit's live order status (submitted / filled /
rejected + fill price) inline alongside the simulation, instead of forcing a
trip to Order History. Nullable + ON DELETE SET NULL so deleting an order never
cascades away the snapshot. Idempotent column add.

Revision ID: m1a2b3c4d5e6
Revises: l1a2b3c4d5e6
Create Date: 2026-06-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "m1a2b3c4d5e6"
down_revision: Union[str, None] = "l1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE solo_exit_items "
        "ADD COLUMN IF NOT EXISTS order_id UUID REFERENCES orders(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE solo_exit_items DROP COLUMN IF EXISTS order_id")
