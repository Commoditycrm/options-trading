"""Schemas for /api/positions — currently held positions at the broker."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.order import InstrumentType, OptionRight, OrderType


class PositionOut(BaseModel):
    broker_account_id: uuid.UUID
    broker_symbol: str                # canonical broker id (OCC for options, ticker for stocks)
    symbol: str                       # bare ticker (root for options)
    instrument_type: InstrumentType
    quantity: Decimal                 # signed: positive = long, negative = short
    avg_entry_price: Decimal | None
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    cost_basis: Decimal | None
    option_expiry: date | None
    option_strike: Decimal | None
    option_right: OptionRight | None


class ClosePositionIn(BaseModel):
    """Close (or partially close) an open position by placing a reverse-side
    order. Quantity defaults to the full position size."""

    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "ClosePositionIn":
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price required for limit close")
        if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("close only supports market or limit")
        return self


class SetSLTPIn(BaseModel):
    """Set (upsert) a stop-loss / take-profit rule on one open position.

    Provide either an absolute price or a percentage of the entry price for
    each of take-profit and stop-loss. At least one of the two must be set.
    """

    broker_account_id: uuid.UUID
    broker_symbol: str = Field(min_length=1, max_length=64)
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    take_profit_pct: Decimal | None = Field(default=None, gt=0)
    stop_loss_pct: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "SetSLTPIn":
        has_tp = self.take_profit_price is not None or self.take_profit_pct is not None
        has_sl = self.stop_loss_price is not None or self.stop_loss_pct is not None
        if not (has_tp or has_sl):
            raise ValueError("set at least one of take_profit or stop_loss")
        return self


class PositionRuleOut(BaseModel):
    id: uuid.UUID
    broker_account_id: uuid.UUID
    broker_symbol: str
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    entry_price: Decimal | None
    status: str
    triggered_at: datetime | None
    detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
