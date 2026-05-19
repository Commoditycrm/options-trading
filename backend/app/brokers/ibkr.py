"""Interactive Brokers (IBKR) Web API adapter — OAuth 1.0a 3rd-party flow.

Why OAuth 1.0a Web API (not TWS Gateway)
----------------------------------------
IBKR's TWS API needs the Trader Workstation / IB Gateway desktop app running
locally, with per-process clientId management and daily auto-logout cycles.
Painful for a SaaS. The OAuth Web API is stateless HTTPS over the same
HTTP signing model as Twitter's old API — the app holds an app-level
consumer_key + RSA signing keys, and each user authorizes us with their
own access_token + access_token_secret.

Underlying SDK
--------------
We use the `ibind` PyPI package (https://github.com/Voyz/ibind) — well-
maintained, supports OAuth 1.0a headless auth, wraps both REST and the
trade-update WebSocket. Same architectural choice as Alpaca's `alpaca-py`.

What's verified vs. unverified
------------------------------
The OAuth signing, brokerage-session bootstrap (`ssodh/init`), and tickle
keepalives live inside ibind — well-tested by its maintainer. Our code
just calls ibind methods, so signing bugs are not ours.

What we WROTE without live IBKR credentials — and therefore haven't run
against the broker — is the mapping between our BrokerAdapter ABC and
ibind's method shapes. Method names and field paths are taken from
ibind's docs as of 2026-05. If a method name has shifted in a newer ibind
release, update the call sites; the structure stays the same.

Once the client completes IBKR's third-party onboarding
(webapionboarding@interactivebrokers.com) and provides the consumer key
+ PEM trio, set them as backend env vars (see app/config.py
ibkr_* fields). At that point this adapter is testable against IBKR's
paper environment.

App-level config required (env vars on backend)
-----------------------------------------------
- IBKR_CONSUMER_KEY              — issued by IBKR on app approval
- IBKR_DH_PARAM_PEM              — generated locally, public half uploaded
- IBKR_PRIVATE_ENCRYPTION_PEM    — generated locally, public half uploaded
- IBKR_PRIVATE_SIGNATURE_PEM     — generated locally, public half uploaded

Per-user creds (stored encrypted on BrokerAccount.encrypted_credentials):
{"access_token": "...", "access_token_secret": "...", "account_id": "U1234567", "paper": false}
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.config import get_settings
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType

log = logging.getLogger(__name__)


# IBKR's order-status vocabulary (per Web API docs) → our enum. They use
# "Submitted" / "Filled" / "Cancelled" / "PendingSubmit" / "PreSubmitted"
# / "Inactive" (rejected) / "PendingCancel" — all PascalCase.
_STATUS_IN = {
    "PendingSubmit": OrderStatus.PENDING,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "Accepted": OrderStatus.ACCEPTED,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "Canceled": OrderStatus.CANCELED,
    "PendingCancel": OrderStatus.SUBMITTED,
    "Inactive": OrderStatus.REJECTED,
    "Rejected": OrderStatus.REJECTED,
    "ApiCancelled": OrderStatus.CANCELED,
}

_SIDE_OUT = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_TIF_OUT = "DAY"

# IBKR order-type strings on the Web API.
_TYPE_OUT = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "STP",
    OrderType.STOP_LIMIT: "STP_LIMIT",
}


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


class IbkrAdapter(BrokerAdapter):
    name = "ibkr"

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self._client = None       # lazy

    # ── client construction ───────────────────────────────────────────────

    def _c(self):
        """Build the ibind IbkrClient once on first call. Raises if the
        backend env doesn't have the app-level IBKR config — the connect
        endpoint should pre-check via Settings.ibkr_configured, but we
        guard here too so direct usage fails loudly."""
        if self._client is not None:
            return self._client
        s = get_settings()
        if not s.ibkr_configured:
            raise RuntimeError(
                "IBKR not configured: set IBKR_CONSUMER_KEY and the three "
                "IBKR_*_PEM env vars before placing orders."
            )
        try:
            # Defer the import so the rest of the backend can boot even if
            # ibind isn't installed (e.g. during early dev / CI without
            # the optional dep).
            from ibind import IbkrClient                            # type: ignore
            from ibind.support.oauth_config import OAuthConfig      # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "ibind package not installed. Add `ibind` to backend/requirements.txt "
                "and reinstall."
            ) from exc

        oauth_config = OAuthConfig(
            access_token=self.credentials["access_token"],
            access_token_secret=self.credentials["access_token_secret"],
            consumer_key=s.ibkr_consumer_key,
            dh_prime=s.ibkr_dh_param_pem,
            encryption_key_fp=s.ibkr_private_encryption_pem,
            signature_key_fp=s.ibkr_private_signature_pem,
        )
        client = IbkrClient(
            account_id=self.credentials["account_id"],
            oauth_config=oauth_config,
        )
        # ibind requires bootstrapping the brokerage session before trading
        # endpoints work; it handles the tickle/keepalive internally.
        client.init_brokerage_session()
        self._client = client
        return client

    # ── connection ────────────────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        c = self._c()
        # portfolio_accounts is a cheap authenticated GET — confirms the
        # OAuth flow + brokerage session are functional.
        resp = c.portfolio_accounts()
        accounts = getattr(resp, "data", None) or []
        if not accounts:
            raise RuntimeError("IBKR returned no accounts for this OAuth token")
        # Caller already chose which account_id to bind to; just confirm
        # it's one of the ones the token unlocks.
        configured = self.credentials.get("account_id")
        ids = [a.get("accountId") if isinstance(a, dict) else getattr(a, "accountId", None) for a in accounts]
        if configured and configured not in ids:
            raise RuntimeError(
                f"account_id {configured} not in OAuth-authorized accounts: {ids}"
            )
        return ConnectionInfo(
            broker_account_id=configured,
            supports_fractional=False,   # IBKR supports fractional only on certain accounts
                                         # /products; conservative default is False.
            extra={"accounts": ids, "paper": self.credentials.get("paper", False)},
        )

    # ── orders ────────────────────────────────────────────────────────────

    def _resolve_conid(self, req: BrokerOrderRequest) -> int:
        """Turn (symbol[, expiry, strike, right]) into IBKR's numeric conid.

        For stocks: trsrv/stocks?symbols=AAPL — pick the US listing.
        For options: TODO — needs secdef/search + secdef/info chain
        traversal. Not implemented in this first pass. The copy engine will
        surface a clean rejection until we add it.
        """
        c = self._c()
        if req.instrument_type == InstrumentType.OPTION:
            raise NotImplementedError(
                "IBKR option order placement not yet wired up. Stocks only "
                "in the initial adapter; options will come in a follow-up "
                "once we add the secdef/info chain lookup."
            )
        resp = c.security_stocks_by_symbol(symbols=req.symbol.upper())
        data = getattr(resp, "data", None) or {}
        rows = data.get(req.symbol.upper()) if isinstance(data, dict) else None
        if not rows:
            raise RuntimeError(f"IBKR doesn't know symbol {req.symbol}")
        # Prefer a US-listed SMART-routable contract.
        for row in rows:
            contracts = row.get("contracts", [])
            for ct in contracts:
                if ct.get("isUS") or ct.get("exchange") in ("NASDAQ", "NYSE", "ARCA"):
                    return int(ct["conid"])
        # Fallback: first conid we see.
        first = rows[0].get("contracts", [{}])[0]
        if "conid" not in first:
            raise RuntimeError(f"Couldn't resolve conid for {req.symbol}")
        return int(first["conid"])

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        c = self._c()
        conid = self._resolve_conid(req)
        body = {
            "conid": conid,
            "orderType": _TYPE_OUT[req.order_type],
            "side": _SIDE_OUT[req.side],
            "tif": _TIF_OUT,
            "quantity": float(req.quantity),
        }
        if req.limit_price is not None:
            body["price"] = float(req.limit_price)
        if req.stop_price is not None:
            body["auxPrice"] = float(req.stop_price)
        if req.client_order_id:
            body["cOID"] = req.client_order_id

        resp = c.place_order(
            account_id=self.credentials["account_id"],
            order_request=body,
        )
        data = getattr(resp, "data", None) or {}
        if isinstance(data, list) and data:
            data = data[0]
        # IBKR's place_order returns either an immediate result OR a list of
        # confirmation prompts (the "automated question/answer" flow ibind
        # mentions). When ibind handles the prompts internally we get back
        # the order receipt; otherwise we see a `messageIds` key. Surface
        # that as an error so the caller knows the order didn't go through.
        if not data or "order_id" not in data and "id" not in data:
            raise RuntimeError(
                f"IBKR didn't accept the order outright (response: {data!r}). "
                "Likely a confirmation prompt that wasn't auto-answered."
            )
        broker_order_id = str(data.get("order_id") or data.get("id"))
        status_text = str(data.get("order_status") or data.get("status") or "Submitted")
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=_STATUS_IN.get(status_text, OrderStatus.SUBMITTED),
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=_dec_or_none(data.get("filled_quantity")) or Decimal(0),
            filled_avg_price=_dec_or_none(data.get("avg_price")),
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        c = self._c()
        resp = c.live_orders()
        rows = getattr(resp, "data", None) or {}
        orders = rows.get("orders", []) if isinstance(rows, dict) else []
        for o in orders:
            if str(o.get("orderId") or o.get("order_id")) == broker_order_id:
                status_text = str(o.get("status") or "Submitted")
                return BrokerOrderResult(
                    broker_order_id=broker_order_id,
                    status=_STATUS_IN.get(status_text, OrderStatus.SUBMITTED),
                    submitted_at=datetime.now(timezone.utc),
                    filled_quantity=_dec_or_none(o.get("filledQuantity")) or Decimal(0),
                    filled_avg_price=_dec_or_none(o.get("avgPrice")),
                )
        # Not found in live_orders means the order has terminalized and
        # aged out; treat as filled-with-unknown unless caller knows
        # otherwise.
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            submitted_at=datetime.now(timezone.utc),
        )

    def cancel_order(self, broker_order_id: str) -> None:
        c = self._c()
        c.cancel_order(
            account_id=self.credentials["account_id"],
            order_id=broker_order_id,
        )

    # ── positions + balance ───────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        c = self._c()
        resp = c.portfolio_positions(account_id=self.credentials["account_id"])
        rows = getattr(resp, "data", None) or []
        out: list[BrokerPosition] = []
        for p in rows:
            symbol = str(p.get("ticker") or p.get("contractDesc") or "")
            qty = _dec_or_none(p.get("position")) or Decimal(0)
            instrument = InstrumentType.OPTION if str(p.get("assetClass", "")).upper() == "OPT" else InstrumentType.STOCK
            out.append(BrokerPosition(
                broker_symbol=str(p.get("conid") or symbol),
                symbol=symbol,
                instrument_type=instrument,
                quantity=qty,
                avg_entry_price=_dec_or_none(p.get("avgPrice")),
                current_price=_dec_or_none(p.get("mktPrice")),
                market_value=_dec_or_none(p.get("mktValue")),
                unrealized_pnl=_dec_or_none(p.get("unrealizedPnl")),
                cost_basis=_dec_or_none(p.get("avgCost")),
            ))
        return out

    def get_balance_snapshot(self) -> dict[str, Any]:
        c = self._c()
        resp = c.portfolio_account_summary(account_id=self.credentials["account_id"])
        data = getattr(resp, "data", None) or {}
        def pick(*keys: str) -> Decimal | None:
            for k in keys:
                if k in data:
                    return _dec_or_none(data[k].get("amount") if isinstance(data[k], dict) else data[k])
            return None
        return {
            "cash": pick("totalcashvalue", "availablefunds"),
            "buying_power": pick("buyingpower"),
            "total_equity": pick("netliquidation", "equitywithloanvalue"),
            "currency": (data.get("currency") if isinstance(data.get("currency"), str) else "USD") or "USD",
        }

    def list_option_contracts(
        self,
        underlying: str,
        expiry: date | None = None,
        expiry_gte: date | None = None,
        expiry_lte: date | None = None,
        limit: int = 200,
    ) -> list[Any]:
        # TODO: implement using secdef/search → secdef/info traversal once
        # we tackle IBKR options. Returning [] keeps the options-chain UI
        # quiet on accounts whose broker is IBKR.
        return []
