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


class WebullCredentialsIn(BaseModel):
    """All five fields are required. ``mfa_code`` is the SMS/email code the
    user just received after hitting ``/api/brokers/webull/start-mfa``; if
    it's stale Webull rejects the login and the user has to restart. The
    ``trade_pin`` is the 6-digit PIN set in Webull's mobile app for trade
    confirmation — without it we can't place orders."""

    username: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=4, max_length=200)
    mfa_code: str = Field(min_length=3, max_length=20)
    trade_pin: str = Field(min_length=4, max_length=12)
    paper: bool = True


class StartWebullMfaIn(BaseModel):
    """Step 1 of the Webull connect flow: trigger Webull to send the MFA
    code. Nothing is stored yet — the user returns with
    ``WebullCredentialsIn`` on the second call (POST /api/brokers)."""

    username: str = Field(min_length=3, max_length=200)
    paper: bool = True


class StartWebullMfaOut(BaseModel):
    sent: bool
    message: str


class StartSnaptradeIn(BaseModel):
    """Step 1 of the SnapTrade connect flow: returns the hosted portal URL.
    ``broker_slug`` is optional — pass e.g. "WEBULL" to skip SnapTrade's
    broker picker, or leave unset to let the user choose."""

    label: str = Field(min_length=1, max_length=120)
    broker_slug: str | None = None
    paper: bool = False


class StartSnaptradeOut(BaseModel):
    portal_url: str


class FinishSnaptradeIn(BaseModel):
    """Step 2: called after the user returns from the portal. We list the
    user's authorizations on SnapTrade and pick the newest one to attach."""

    label: str = Field(min_length=1, max_length=120)


class ConnectBrokerIn(BaseModel):
    broker: BrokerName
    label: str = Field(min_length=1, max_length=120)
    # Exactly one credential block matching `broker` should be populated.
    # SnapTrade has its own two-step flow (start → finish) and does NOT use
    # this generic shape.
    alpaca: AlpacaCredentialsIn | None = None
    ibkr: IbkrCredentialsIn | None = None
    webull: WebullCredentialsIn | None = None


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker: BrokerName
    # Real brokerage name for SnapTrade connections (e.g. "Webull"); NULL for
    # direct integrations. Traders/admins see it; subscribers stay white-labeled.
    brokerage_name: str | None = None
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
