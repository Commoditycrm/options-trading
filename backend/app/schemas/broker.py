import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.broker_account import BrokerName


class AlpacaCredentialsIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)
    api_secret: str = Field(min_length=8, max_length=200)
    paper: bool = True


class IbkrCredentialsIn(BaseModel):
    """Per-user OAuth 1.0a tokens for IBKR Web API.

    The consumer_key and the three PEM signing artefacts are APP-level
    (shared across all users, set as backend env vars). What's per-user
    is just the access_token + access_token_secret obtained when the
    user authorizes the app via IBKR's OAuth flow.

    account_id is IBKR's account identifier (e.g. "U1234567") for the
    account the user wants to trade. One IBKR login can have multiple
    accounts; we pick one.
    """
    access_token: str = Field(min_length=8, max_length=400)
    access_token_secret: str = Field(min_length=8, max_length=400)
    account_id: str = Field(min_length=4, max_length=40)
    paper: bool = False


class ConnectBrokerIn(BaseModel):
    broker: BrokerName
    label: str = Field(min_length=1, max_length=120)
    # Exactly one credential block matching `broker` should be populated.
    alpaca: AlpacaCredentialsIn | None = None
    ibkr: IbkrCredentialsIn | None = None


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker: BrokerName
    label: str
    is_paper: bool
    supports_fractional: bool
    broker_account_number: str | None
    connection_status: str
    last_error: str | None
    created_at: datetime

    cash: Decimal | None = None
    buying_power: Decimal | None = None
    total_equity: Decimal | None = None
    currency: str | None = None
    balance_updated_at: datetime | None = None

    model_config = {"from_attributes": True}
