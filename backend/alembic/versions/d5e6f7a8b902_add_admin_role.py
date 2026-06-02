"""add 'admin' value to user_role enum

Adds the ADMIN role for platform operators (gates /api/admin/* + the
/admin frontend). Admins are created via scripts/create_admin.py, not
normal registration. Ported from anitha-admin; re-authored on App 2's
head (c4d5e6f7a801) rather than merging that chain.

Revision ID: d5e6f7a8b902
Revises: c4d5e6f7a801
Create Date: 2026-05-29 04:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d5e6f7a8b902"
down_revision: Union[str, None] = "c4d5e6f7a801"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'admin'")


def downgrade() -> None:
    # Postgres can't drop an enum value without recreating the type. Leave
    # 'admin' as a harmless residual; delete admin users before rollback.
    pass
