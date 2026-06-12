"""Live quotes (bid/ask/mid) for the trade panel.

Uses Alpaca's market-data API with the caller's own stored Alpaca keys (same
keys as trading). Best-effort: if the account has no Alpaca connection or no
data entitlement, returns nulls with available=false so the UI shows "—"
instead of erroring. Note: Alpaca's free tier is IEX/delayed and options data
may require a subscription — the values are advisory, flagged as such in the UI.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User
from app.services.crypto import decrypt_json

router = APIRouter(prefix="/api/marketdata", tags=["marketdata"])


def _alpaca_creds(db: Session, user_id) -> dict | None:
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker == BrokerName.ALPACA,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return None
    return decrypt_json(acct.encrypted_credentials)


@router.get("/quote")
def get_quote(
    symbol: str | None = Query(None, description="Underlying ticker, e.g. AAPL"),
    occ: str | None = Query(None, description="OCC option symbol, e.g. AAPL250719C00200000"),
    expiry: str | None = Query(None, description="Option expiry YYYY-MM-DD (with symbol/strike/right)"),
    strike: str | None = Query(None),
    right: str | None = Query(None, description="call | put"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Return {bid, ask, mid, available}. Provide either `symbol` (stock),
    `occ` (option), or symbol+expiry+strike+right (option, assembled here)."""
    creds = _alpaca_creds(db, user.id)
    if creds is None:
        return {"bid": None, "ask": None, "mid": None, "available": False, "reason": "no_alpaca_account"}

    adapter = AlpacaAdapter(creds)

    # Resolve an option OCC from the parts if not given directly.
    if occ is None and expiry and strike and right and symbol:
        from datetime import date
        from decimal import Decimal
        try:
            y, m, d = (int(x) for x in expiry.split("-"))
            occ = build_occ_symbol(symbol, date(y, m, d), Decimal(strike), right)
        except Exception:  # noqa: BLE001
            raise HTTPException(422, "bad_option_params")

    if occ:
        q = adapter.get_option_quote(occ)
    elif symbol:
        q = adapter.get_stock_quote(symbol)
    else:
        raise HTTPException(422, "symbol_or_occ_required")

    return {**q, "available": q.get("mid") is not None or q.get("bid") is not None}
