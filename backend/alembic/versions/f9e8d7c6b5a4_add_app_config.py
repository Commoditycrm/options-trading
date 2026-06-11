"""add app_config table for white-label branding (business_name)

Single-row table (id=1) holding the editable business name. Read by the
public GET /api/config endpoint; mutated by admins via PATCH /api/admin/config.

Revision ID: f9e8d7c6b5a4
Revises: e2f3a4b5c601
Create Date: 2026-06-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9e8d7c6b5a4"
down_revision: Union[str, None] = "e2f3a4b5c601"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "business_name", sa.String(length=120), nullable=False,
            server_default="The Option Haven",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    # Seed the single config row. Idempotent-safe: table was just created.
    op.execute(
        "INSERT INTO app_config (id, business_name) VALUES (1, 'The Option Haven')"
    )


def downgrade() -> None:
    op.drop_table("app_config")
