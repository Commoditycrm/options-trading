import enum
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RetryInterval(str, enum.Enum):
    """How long to wait before retrying a failed mirror order. Used by
    the subscriber-set retry policy (one value for opens, one for closes).
    NEVER disables the retry entirely (existing REJECT-on-first-failure
    behaviour). The minute values map to a single retry attempt at that
    delay — there is intentionally no multi-attempt option in v1 to keep
    the state machine simple."""
    NEVER = "never"
    ONE_MIN = "1m"
    TWO_MIN = "2m"
    THREE_MIN = "3m"
    FIVE_MIN = "5m"

    def seconds(self) -> int | None:
        """Convert to seconds for retry_at scheduling. Returns None for NEVER."""
        return {
            RetryInterval.NEVER:     None,
            RetryInterval.ONE_MIN:    60,
            RetryInterval.TWO_MIN:   120,
            RetryInterval.THREE_MIN: 180,
            RetryInterval.FIVE_MIN:  300,
        }[self]


class TraderSettings(Base, TimestampMixin):
    """One row per trader. Master kill switch for outgoing trades."""

    __tablename__ = "trader_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Pause fanout to subscribers. Pure gate — subscribers' own copy_enabled
    # flags are NOT touched when this flips. When True, fanout skips everyone
    # regardless of their preference.
    copy_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # When True, orders the trader places DIRECTLY at their broker (e.g. via
    # Alpaca's own web UI, IBKR TWS, or any other path that bypasses our
    # Trade Panel) are detected via the broker trade-update stream and
    # fanned out to subscribers automatically.
    # Default OFF so day-1 traders aren't surprised by their hedge / test
    # trades being mirrored. Trader explicitly opts in via Settings page.
    mirror_external_trades: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="trader_settings")


class SubscriberSettings(Base, TimestampMixin):
    """One row per subscriber. Holds the multiplier, the trader being followed,
    and the subscriber-side kill switch."""

    __tablename__ = "subscriber_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    following_trader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("1.000"), nullable=False)

    # Daily realized-loss kill switch. Stored as a positive amount (e.g. 500 means
    # "stop after $500 loss today"). NULL disables the feature.
    # When today's realized P&L falls below -daily_loss_limit, copy_enabled is
    # auto-flipped to false and an audit + SSE event are emitted.
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Retry policy for mirror orders that fail with a transient/broker-disconnect
    # error (network blip, 5xx, rate limit, timeout). On such a failure we
    # schedule a single retry at this interval; if THAT also fails we mark
    # REJECTED and create a notification. NEVER (default) preserves the legacy
    # behaviour: REJECT on first failure, no notification.
    # User-fixable rejections (insufficient buying power, expired option, etc.)
    # never trigger retry regardless of this setting — they'd just fail the
    # same way next time.
    retry_interval_open: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval",
             values_callable=lambda e: [m.value for m in e]),
        default=RetryInterval.NEVER, nullable=False,
    )
    retry_interval_close: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval",
             values_callable=lambda e: [m.value for m in e],
             create_type=False),    # type already created above
        default=RetryInterval.NEVER, nullable=False,
    )

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
