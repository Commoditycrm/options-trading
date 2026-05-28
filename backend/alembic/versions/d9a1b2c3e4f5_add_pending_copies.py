"""add pending_copies queue table

Demo-only table for the queue-based fanout architecture. The hot path on
the trader's order detection writes one row per subscriber here and
returns immediately; an async worker pool picks rows up, runs gates from
the in-memory cache, and submits to the broker.

Columns capture every transition (queued_at -> picked_up_at ->
submitted_at) so the demo dashboard can render per-subscriber timeline
bars and compute the queue-to-broker latency.

Revision ID: d9a1b2c3e4f5
Revises: a92fc3b551d4
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d9a1b2c3e4f5"
down_revision: Union[str, None] = "a92fc3b551d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pending_copy_status = sa.Enum(
        "queued", "processing", "submitted", "failed",
        name="pending_copy_status",
    )
    pending_copy_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "pending_copies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "parent_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscriber_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "processing", "submitted", "failed",
                name="pending_copy_status", create_type=False,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queue_to_broker_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_pending_copies_status_queued_at",
        "pending_copies",
        ["status", "queued_at"],
    )
    op.create_index(
        "ix_pending_copies_parent_order_id",
        "pending_copies",
        ["parent_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_copies_parent_order_id", table_name="pending_copies")
    op.drop_index("ix_pending_copies_status_queued_at", table_name="pending_copies")
    op.drop_table("pending_copies")
    op.execute("DROP TYPE IF EXISTS pending_copy_status")
