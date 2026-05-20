"""add mirror_external_trades to trader_settings

Revision ID: f8d2c11a4501
Revises: e7c4a9b21d83
Create Date: 2026-05-20 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8d2c11a4501"
down_revision: Union[str, None] = "e7c4a9b21d83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column(
            "mirror_external_trades",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "mirror_external_trades")
