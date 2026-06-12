"""backfill missing trader_settings / subscriber_settings rows

Root-cause fix companion: the admin "change role" endpoint historically only
flipped users.role without creating the role's settings row (registration does).
A trader with no trader_settings row is silently excluded from the
external_trade_poller (which gated on a TraderSettings join), so their
broker-placed trades never reflected. This backfills any existing users whose
settings row is missing for their current role. Idempotent (INSERT ... WHERE NOT
EXISTS); only the Python-default NOT NULL columns are supplied (created_at/
updated_at have server defaults).

Revision ID: h1a2b3c4d5e6
Revises: g1a2b3c4d5e6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "h1a2b3c4d5e6"
down_revision: Union[str, None] = "g1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Traders missing a trader_settings row.
    op.execute(
        """
        INSERT INTO trader_settings
            (user_id, trading_enabled, copy_paused, mirror_external_trades, mirror_only_filled)
        SELECT u.id, true, false, false, false
          FROM users u
         WHERE u.role = 'trader'
           AND NOT EXISTS (
               SELECT 1 FROM trader_settings ts WHERE ts.user_id = u.id
           )
        """
    )
    # Subscribers missing a subscriber_settings row.
    op.execute(
        """
        INSERT INTO subscriber_settings
            (user_id, copy_enabled, multiplier, retry_interval_open, retry_interval_close)
        SELECT u.id, false, 1.000, 'never', 'never'
          FROM users u
         WHERE u.role = 'subscriber'
           AND NOT EXISTS (
               SELECT 1 FROM subscriber_settings ss WHERE ss.user_id = u.id
           )
        """
    )


def downgrade() -> None:
    # Data backfill — nothing to undo (we can't tell backfilled rows from
    # legitimately-created ones, and dropping them would be destructive).
    pass
