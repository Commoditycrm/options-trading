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
    # Req #12: auto-liquidation equity floor ($). NULL = disabled.
    auto_liquidation_limit: Decimal | None = None
    # Percentage-based risk controls (NULL = disabled).
    daily_loss_limit_pct: Decimal | None = None
    daily_profit_limit_pct: Decimal | None = None
    per_trade_loss_limit_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_drawdown_equity_baseline: Decimal | None = None
    # Retry policy. NEVER (default) = no retry on broker-disconnect failures.
    retry_interval_open: RetryInterval = RetryInterval.NEVER
    retry_interval_close: RetryInterval = RetryInterval.NEVER
    todays_realized_pnl: Decimal | None = None
    # Req #6: exclusion list
    excluded_symbols: list[str] = []
    # Req #4: auto TP/SL
    take_profit_pct: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    # Mirror the trader's position exits (manual close + SL/TP cascade).
    follow_trader_exits: bool = True

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
    mirror_only_filled: bool = False
    default_broker_account_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class TraderMirrorOnlyFilledIn(BaseModel):
    mirror_only_filled: bool


class TraderDefaultBrokerIn(BaseModel):
    """Set (or clear) the default broker account for the Trade Panel dropdown.
    Pass null to clear the preference."""
    default_broker_account_id: uuid.UUID | None = None


class SubscriberToggleIn(BaseModel):
    copy_enabled: bool


class SubscriberSelfMultiplierIn(BaseModel):
    """Subscriber-editable multiplier. Bounded so a misclicked extra zero
    doesn't 100x someone's exposure."""

    multiplier: Decimal = Field(gt=0, le=10)


class DailyLossLimitIn(BaseModel):
    """Subscriber-set daily realized-loss kill switch. Pass null to disable."""

    daily_loss_limit: Decimal | None = Field(default=None, ge=0)


class AutoLiquidationLimitIn(BaseModel):
    """Req #12: auto-liquidation equity floor in absolute $. When live account
    equity falls to/at-or-below this, all positions are liquidated at market and
    copy is paused. Pass null to disable."""
    auto_liquidation_limit: Decimal | None = Field(default=None, ge=Decimal("0"))


class DailyLossLimitPctIn(BaseModel):
    """Daily realized-loss limit as a % of account equity. Null to disable."""
    daily_loss_limit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class DailyProfitLimitPctIn(BaseModel):
    """Daily realized-profit target as a % of account equity. When reached,
    copy auto-pauses for the day. Null to disable."""
    daily_profit_limit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1000"))


class PerTradeLossLimitPctIn(BaseModel):
    """Per-trade realized-loss limit as a % of account equity. Null to disable."""
    per_trade_loss_limit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class MaxDrawdownPctIn(BaseModel):
    """Max drawdown % below the equity baseline captured when enabled. Null to disable."""
    max_drawdown_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class ExcludedSymbolsIn(BaseModel):
    """Req #6: replace the whole exclusion list. Pass [] to clear."""
    excluded_symbols: list[str] = []


class TakeProfitPctIn(BaseModel):
    """Req #4: auto take-profit % above entry premium. Null to disable."""
    take_profit_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1000"))


class StopLossPctIn(BaseModel):
    """Req #4: auto stop-loss % below entry premium. Null to disable."""
    stop_loss_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))


class FollowTraderExitsIn(BaseModel):
    """Whether the subscriber mirrors the trader's position exits."""
    follow_trader_exits: bool


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


class RemoveSubscribersIn(BaseModel):
    """Trader removes one or more subscribers from following them. Each id must
    currently follow the caller. Removed subscribers are unfollowed and have
    copy turned off."""

    subscriber_ids: list[uuid.UUID] = Field(min_length=1)


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
