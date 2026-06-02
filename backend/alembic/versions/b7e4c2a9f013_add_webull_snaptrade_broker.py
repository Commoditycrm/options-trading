"""add 'webull' and 'snaptrade' to broker_name enum

Phase 1 of the broker consolidation: brings the Webull (direct) and
SnapTrade (aggregator) adapters into App 2. This migration just widens
the Postgres broker_name enum; the adapter code lives in
app/brokers/webull.py and app/brokers/snaptrade.py.

Chained on App 2's own head (e1f2a3b4c5d6, the mock-broker enum add) —
we intentionally do NOT merge gaurav-snaptrade's divergent migration
chain; we re-author a clean enum-add here instead.

Revision ID: b7e4c2a9f013
Revises: e1f2a3b4c5d6
Create Date: 2026-05-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b7e4c2a9f013"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction (PG <12);
    # all supported versions are 12+, but the autocommit block is the
    # idiomatic, safe way (matches the mock/ibkr enum additions).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'webull'")
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'snaptrade'")


def downgrade() -> None:
    # Postgres can't drop a value from an enum without recreating the type.
    # 'webull'/'snaptrade' are harmless residuals — leave them.
    pass
