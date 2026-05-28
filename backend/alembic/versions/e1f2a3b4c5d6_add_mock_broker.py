"""add 'mock' value to broker_name enum (demo broker)

Lets scripts/seed_demo.py create broker accounts backed by the simulated
MockAdapter so the queue demo runs without real broker credentials.

Revision ID: e1f2a3b4c5d6
Revises: d9a1b2c3e4f5
Create Date: 2026-05-28 00:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d9a1b2c3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction (PG <12);
    # all supported versions are 12+, but the autocommit block is the
    # safe, idiomatic way (matches a92fc3b551d4's RETRY_PENDING add).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'mock'")


def downgrade() -> None:
    # Postgres can't drop a value from an enum without recreating the type.
    # 'mock' is a harmless residual — leave it.
    pass
