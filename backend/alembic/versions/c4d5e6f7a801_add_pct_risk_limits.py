"""add percentage-based risk limits to subscriber_settings

daily_loss_limit_pct, per_trade_loss_limit_pct, max_drawdown_pct, and
max_drawdown_equity_baseline. Enforced per-subscriber in the worker pool
before placing each mirror order. Ported from anitha-loss-limit-and-drawdown;
re-authored on App 2's head (b7e4c2a9f013) rather than merging that chain.

Revision ID: c4d5e6f7a801
Revises: b7e4c2a9f013
Create Date: 2026-05-29 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a801"
down_revision: Union[str, None] = "b7e4c2a9f013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("subscriber_settings",
                  sa.Column("daily_loss_limit_pct", sa.Numeric(6, 3), nullable=True))
    op.add_column("subscriber_settings",
                  sa.Column("per_trade_loss_limit_pct", sa.Numeric(6, 3), nullable=True))
    op.add_column("subscriber_settings",
                  sa.Column("max_drawdown_pct", sa.Numeric(6, 3), nullable=True))
    op.add_column("subscriber_settings",
                  sa.Column("max_drawdown_equity_baseline", sa.Numeric(20, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriber_settings", "max_drawdown_equity_baseline")
    op.drop_column("subscriber_settings", "max_drawdown_pct")
    op.drop_column("subscriber_settings", "per_trade_loss_limit_pct")
    op.drop_column("subscriber_settings", "daily_loss_limit_pct")
