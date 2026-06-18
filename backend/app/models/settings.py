import enum
import uuid
from decimal import Decimal
from typing import List

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RetryInterval(str, enum.Enum):
    """How long to wait before retrying a failed mirror order."""
    NEVER = "never"
    ONE_MIN = "1m"
    TWO_MIN = "2m"
    THREE_MIN = "3m"
    FIVE_MIN = "5m"

    def seconds(self) -> int | None:
        return {
            RetryInterval.NEVER:     None,
            RetryInterval.ONE_MIN:    60,
            RetryInterval.TWO_MIN:   120,
            RetryInterval.THREE_MIN: 180,
            RetryInterval.FIVE_MIN:  300,
        }[self]


class TraderSettings(Base, TimestampMixin):
    """One row per trader. Master kill switch + per-trader preferences."""

    __tablename__ = "trader_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Pause fanout to all subscribers.
    copy_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # When True, external orders (placed in broker app, not our Trade Panel)
    # are detected and fanned out. Default OFF.
    mirror_external_trades: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Solo trader: trades only for himself (no subscribers / no fan-out) and gets
    # the solo exit/simulation/re-enter toolset instead of the copy-trading UI.
    # Admin-set. Default OFF (a normal copy-trader).
    solo_mode: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # Req #3: if True, only FILLED orders are mirrored to subscribers
    # (not open/pending). Default False = mirror immediately on detection.
    mirror_only_filled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Req #1 (Option B): trader can connect multiple brokers (Alpaca + Webull).
    # This stores which one to pre-select in the Trade Panel dropdown.
    # NULL means no preference (first connected account is used).
    # SET NULL on broker_account delete so it degrades gracefully.
    default_broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )

    # White-label logo the trader's subscribers see — a base64 data URL stored
    # in the DB (the box's disk is wiped on each deploy; Postgres persists).
    # DEFERRED so the fan-out hot path (queue_fanout loads TraderSettings) never
    # pulls this potentially-large blob; it's fetched only when explicitly read.
    logo: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)

    user = relationship("User", back_populates="trader_settings")


class SubscriberSettings(Base, TimestampMixin):
    """One row per subscriber. Per-subscriber risk limits + preferences."""

    __tablename__ = "subscriber_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    following_trader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("1.000"), nullable=False)

    # ── Absolute daily-loss kill switch (legacy $) ─────────────────────────
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # ── Req #12: Auto-liquidation equity floor ($) ─────────────────────────
    # When the account's LIVE equity falls to/at-or-below this $ amount, the
    # position_monitor liquidates ALL open positions at market and flips
    # copy_enabled off — a capital-preservation kill switch. NULL = disabled.
    # Armed only while copy_enabled is True; firing disarms it (copy off) until
    # the subscriber manually re-enables copy, mirroring the daily-loss pattern.
    auto_liquidation_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # ── Percentage-based risk controls ─────────────────────────────────────
    daily_loss_limit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    # Daily PROFIT target as % of account equity. When today's realized P&L
    # reaches +this%, copy is auto-paused for the day (lock in gains) — the
    # mirror image of daily_loss_limit_pct.
    daily_profit_limit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    per_trade_loss_limit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    max_drawdown_equity_baseline: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)

    # ── Retry policy ───────────────────────────────────────────────────────
    retry_interval_open: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval",
             values_callable=lambda e: [m.value for m in e]),
        default=RetryInterval.NEVER, nullable=False,
    )
    retry_interval_close: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval",
             values_callable=lambda e: [m.value for m in e],
             create_type=False),
        default=RetryInterval.NEVER, nullable=False,
    )

    # ── Req #6: Exclusion list ─────────────────────────────────────────────
    # Underlying tickers the subscriber does NOT want mirrored to them.
    # e.g. ["AAPL", "META"] skips all AAPL/META options at any strike/expiry.
    # Stored uppercase; checked against trader_order.symbol (already uppercase).
    excluded_symbols: Mapped[List[str]] = mapped_column(
        ARRAY(Text()), nullable=False, default=list, server_default="{}"
    )

    # When True (default), the subscriber mirrors the trader's position
    # EXITS too — so a trader's manual close or SL/TP-triggered exit closes
    # the subscriber's mirrored position. False = the subscriber manages
    # their own exits (via their take_profit_pct/stop_loss_pct or manually).
    follow_trader_exits: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    # ── Req #4: Auto take-profit / stop-loss (Replace mode) ───────────────
    # When set, the worker places an IBKR OCA bracket around the option entry.
    # Stored as a positive percentage, e.g. 5.000 = 5%. NULL = disabled.
    # REPLACE mode: once a bracket is armed, the trader's later closing order
    # is NOT mirrored to this subscriber — they manage their own exit.
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
