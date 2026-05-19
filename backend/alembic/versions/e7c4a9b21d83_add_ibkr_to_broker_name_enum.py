"""add ibkr to broker_name enum

Revision ID: e7c4a9b21d83
Revises: d5b8e3a91f47
Create Date: 2026-05-20 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e7c4a9b21d83"
down_revision: Union[str, None] = "d5b8e3a91f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction in Postgres <12.
    # All currently-supported versions are 12+. autocommit_block lets alembic
    # commit before the DDL so the new enum value is visible to the rest of
    # the migration's session if needed.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'ibkr'")


def downgrade() -> None:
    # Postgres doesn't support DROP VALUE on an enum. To remove "ibkr" you'd
    # have to recreate the type without it and re-cast every column that uses
    # it — destructive and unnecessary for a downgrade. We accept the value
    # lingering in the type even after downgrade.
    pass
