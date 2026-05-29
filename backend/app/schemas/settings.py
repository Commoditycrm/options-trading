import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.settings import RetryInterval


class SubscriberSettingsOut(BaseModel):
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None
    # Percentage-based risk controls (NULL = disabled).
    daily_loss_limit_pct: Decimal | None = None
    per_trade_loss_limit_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_drawdown_equity_baseline: Decimal | None = None
    # Retry policy. NEVER (default) = no retry on broker-disconnect failures.
    retry_interval_open: RetryInterval = RetryInterval.NEVER
    retry_interval_close: RetryInterval = RetryInterval.NEVER
    todays_realized_pnl: Decimal | None = None  # populated by GET endpoint, not by PATCH responses

    model_config = {"from_attributes": True}


class SubscriberRetryIntervalIn(BaseModel):
    """Subscriber sets their per-direction retry policy. Sent as TWO
    fields so the frontend can update them independently (one dropdown
    per direction in the UI)."""

    retry_interval_open: RetryInterval | None = None
    retry_interval_close: RetryInterval | None = None


class TraderSettingsOut(BaseModel):
    user_id: uuid.UUID
    trading_enabled: bool
    copy_paused: bool = False
    mirror_external_trades: bool = False

    model_config = {"from_attributes": True}


class SubscriberToggleIn(BaseModel):
    copy_enabled: bool


class SubscriberSelfMultiplierIn(BaseModel):
    """Subscriber-editable multiplier. Bounded so a misclicked extra zero
    doesn't 100x someone's exposure."""

    multiplier: Decimal = Field(gt=0, le=10)


class DailyLossLimitIn(BaseModel):
    """Subscriber-set daily realized-loss kill switch. Pass null to disable."""

    daily_loss_limit: Decimal | None = Field(default=None, ge=0)


class DailyLossLimitPctIn(BaseModel):
    """Daily realized-loss limit as a % of account equity. Null to disable."""
    daily_loss_limit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class PerTradeLossLimitPctIn(BaseModel):
    """Per-trade realized-loss limit as a % of account equity. Null to disable."""
    per_trade_loss_limit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class MaxDrawdownPctIn(BaseModel):
    """Max drawdown % below the equity baseline captured when enabled. Null to disable."""
    max_drawdown_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class FollowTraderIn(BaseModel):
    trader_id: uuid.UUID | None  # null to unfollow


class TraderToggleIn(BaseModel):
    trading_enabled: bool


class TraderMirrorExternalIn(BaseModel):
    """Trader opts in to having orders they place DIRECTLY at their broker
    (outside our Trade Panel) mirrored to subscribers. Default-off elsewhere
    so existing traders don't get surprised."""

    mirror_external_trades: bool


class SubscriberMultiplierIn(BaseModel):
    """Trader-only override of a subscriber's multiplier."""

    multiplier: Decimal = Field(gt=0, le=100)


class BulkCopyStateOut(BaseModel):
    """`total`/`enabled` reflect subscribers' own copy flags (informational).
    `paused` is the trader-side master fanout gate — when True, no mirrors
    are placed regardless of subscribers' individual settings."""

    total: int
    enabled: int
    paused: bool = False


class BulkCopyToggleIn(BaseModel):
    enabled: bool


class SubscriberSummary(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    copy_enabled: bool
    multiplier: Decimal
    broker_count: int
    realized_pnl_30d: Decimal
