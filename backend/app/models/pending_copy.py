"""Queue row for the demo's queue-based fanout architecture.

The trader-side hot path inserts one PendingCopy per eligible subscriber
and returns. A pool of async workers picks them up, runs the eligibility
gates from the in-memory cache, calls the broker, and records the
transition timestamps so the demo dashboard can render the timeline.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class PendingCopyStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUBMITTED = "submitted"
    FAILED = "failed"


class PendingCopy(Base):
    __tablename__ = "pending_copies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscriber_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[PendingCopyStatus] = mapped_column(
        Enum(PendingCopyStatus, name="pending_copy_status",
             values_callable=lambda e: [m.value for m in e]),
        default=PendingCopyStatus.QUEUED,
        nullable=False,
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    picked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queue_to_broker_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Latency split for the "<50ms platform" metric:
    #   pickup_ms   = queued_at → picked_up_at (how long it waited to be claimed;
    #                 ~0 with LISTEN/NOTIFY, was up to the poll interval before)
    #   platform_ms = our processing only = queue_to_broker_ms − broker_ms
    #                 (the number that must stay < 50ms; broker time is external)
    pickup_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
