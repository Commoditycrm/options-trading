"""Solo-trader exit snapshots — drive the post-exit simulation + re-enter.

When a solo trader hits "Exit All", we record the set of positions we just
closed (one SoloExitItem per position) so we can (a) show a live "what-if"
simulation of those contracts and (b) re-enter exactly the same set later.

Enum-like fields (exit_mode, side, instrument_type) are stored as plain strings
on purpose — no Postgres ENUM types, to avoid the enum-label-casing pitfalls.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base, TimestampMixin


class SoloExitSnapshot(Base, TimestampMixin):
    """One "Exit All" event for a solo trader."""

    __tablename__ = "solo_exit_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # "market" | "bid" | "ask"
    exit_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    reentered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items = relationship("SoloExitItem", back_populates="snapshot", cascade="all, delete-orphan")


class SoloExitItem(Base):
    """One position captured at exit time (for simulation + re-enter)."""

    __tablename__ = "solo_exit_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("solo_exit_snapshots.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False
    )
    # The closing Order placed for this position (for live status on /solo).
    # Nullable + SET NULL: an order delete never removes the snapshot row.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "stock" | "option"
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)           # underlying root
    occ_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True) # full OCC for options
    original_side: Mapped[str] = mapped_column(String(8), nullable=False)     # "buy" | "sell" (the entry)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    snapshot = relationship("SoloExitSnapshot", back_populates="items")
