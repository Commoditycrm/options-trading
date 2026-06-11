import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PositionRuleStatus(str, enum.Enum):
    ACTIVE = "active"        # being monitored
    TRIGGERED = "triggered"  # threshold hit, close order placed
    CANCELLED = "cancelled"  # cleared by the user, or position vanished


class PositionRule(Base, TimestampMixin):
    """A stop-loss / take-profit rule on ONE open position.

    Keyed by (broker_account_id, broker_symbol). The position_monitor poller
    watches the live broker price for each ACTIVE rule and auto-closes the
    position (a reverse market order on the owner's own account) when the
    take-profit or stop-loss price is crossed. Prices are absolute; the API
    resolves a percentage input to a price using the position's entry price.
    """

    __tablename__ = "position_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # Broker canonical id — OCC for options, ticker for stocks. The position key.
    broker_symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    # Snapshot of the position's entry price when the rule was set (for display
    # and for resolving percentage inputs).
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)

    status: Mapped[PositionRuleStatus] = mapped_column(
        Enum(
            PositionRuleStatus, name="position_rule_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PositionRuleStatus.ACTIVE, nullable=False, index=True,
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
