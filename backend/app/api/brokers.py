"""Broker connection endpoints.

Supported brokers
-----------------
- **Alpaca** (direct): paste API key + secret. Realtime via WebSocket.
- **IBKR** (direct, OAuth 1.0a): per-user access tokens; app-level signing
  config via env. Subscriber placement broker.
- **Webull** (direct, unofficial): username + password + MFA + 6-digit
  trade PIN. Two-step: ``/webull/start-mfa`` then ``POST /api/brokers``.
- **SnapTrade** (aggregator): hosted-portal OAuth flow via ``/snaptrade/start``
  then ``/snaptrade/finish``. ~20 brokers (incl. Webull, which supports
  trading through SnapTrade since Dec 2025).

One-broker-per-user
-------------------
Connecting a new broker *replaces* any existing one (delete old row + stop
its listener, attach the new one). Keeps copy-trading semantics
unambiguous — one source of truth for a trader's fills.

Phase note (broker consolidation)
---------------------------------
The trader-side detection ``listeners`` service is Phase 3. Calls to it
here are guarded with try/except ImportError so this module works before
that service exists; once ``app.services.listeners`` lands, trader
connects/deletes will automatically start/stop the right listener. Alpaca
keeps using the existing ``alpaca_stream`` directly in the meantime.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.brokers import adapter_for
# NOTE: app.brokers.webull / app.brokers.snaptrade are imported LAZILY inside
# the functions that need them — their modules import the fragile `webull` /
# `snaptrade_client` SDKs at module top, and main.py imports this router, so a
# top-level import here would make the whole app fail to boot without those
# SDKs installed. Keep the Phase-1 "app boots without the SDKs" guarantee.
from app.config import get_settings
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.schemas.broker import (
    BrokerAccountOut,
    ConnectBrokerIn,
    FinishSnaptradeIn,
    StartSnaptradeIn,
    StartSnaptradeOut,
    StartWebullMfaIn,
    StartWebullMfaOut,
)
from app.services import alpaca_stream, audit, memory_cache
from app.services.crypto import decrypt_json, encrypt_json

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["brokers"])


# ── listener wiring (Phase-3-safe) ───────────────────────────────────────────

def _start_trader_listener(user: User, acct: BrokerAccount) -> None:
    """Start trade-detection for a trader's broker. Alpaca uses the existing
    alpaca_stream; Webull/SnapTrade use the unified `listeners` service which
    arrives in Phase 3 (guarded so this works before then)."""
    if user.role != UserRole.TRADER:
        return
    if acct.broker == BrokerName.ALPACA:
        try:
            alpaca_stream.start_stream(acct.id)
        except Exception:  # noqa: BLE001
            log.exception("alpaca_stream.start_stream failed for %s", acct.id)
        return
    try:
        from app.services import listeners  # Phase 3
        listeners.start_listener(user.id, acct.id)
    except ImportError:
        log.info(
            "listeners service not present yet (Phase 3); %s detection deferred",
            acct.broker.value,
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to start %s listener", acct.broker.value)


def _stop_trader_listener(user_id: uuid.UUID, acct: BrokerAccount) -> None:
    if acct.broker == BrokerName.ALPACA:
        try:
            alpaca_stream.stop_stream(acct.id)
        except Exception:  # noqa: BLE001
            log.exception("alpaca_stream.stop_stream failed for %s", acct.id)
        return
    try:
        from app.services import listeners  # Phase 3
        listeners.stop_listener(user_id)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("stop_listener failed")


# ── credentials ──────────────────────────────────────────────────────────────

def _webull_device_id(user_id: uuid.UUID) -> str:
    """Stable device fingerprint per app user. Webull binds MFA codes to the
    requesting device's ``_did`` — get_mfa and the follow-up login MUST use
    the same value or Webull rejects the login with an empty body. Deriving
    from user.id via uuid5 keeps it deterministic across both endpoints."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"webull-did-{user_id}"))


def _credentials_for(payload: ConnectBrokerIn, user_id: uuid.UUID) -> dict[str, Any]:
    """Build the credentials dict Fernet-encrypted onto the BrokerAccount.
    For Webull this runs the full login flow (we can't store username/password
    alone — we need the session tokens from Webull's login endpoint)."""
    match payload.broker:
        case BrokerName.ALPACA:
            if not payload.alpaca:
                raise HTTPException(422, "alpaca credentials required")
            return payload.alpaca.model_dump()
        case BrokerName.IBKR:
            if not payload.ibkr:
                raise HTTPException(422, "ibkr credentials required")
            if not get_settings().ibkr_configured:
                raise HTTPException(
                    501,
                    "ibkr_not_configured: backend missing IBKR_CONSUMER_KEY "
                    "and/or IBKR_*_PEM env vars. Complete IBKR's third-party "
                    "onboarding and set the env vars before connecting.",
                )
            return payload.ibkr.model_dump()
        case BrokerName.WEBULL:
            if not payload.webull:
                raise HTTPException(422, "webull credentials required")
            w = payload.webull
            device_id = _webull_device_id(user_id)
            from app.brokers.webull import login_with_mfa
            try:
                return login_with_mfa(
                    username=w.username,
                    password=w.password,
                    mfa_code=w.mfa_code,
                    trade_pin=w.trade_pin,
                    paper=w.paper,
                    device_id=device_id,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                log.exception("webull login_with_mfa unexpected failure")
                raise HTTPException(400, f"webull_error: {exc}") from exc
    raise HTTPException(422, "unknown broker (SnapTrade uses /snaptrade/start)")


def _refresh_balance_into(acct: BrokerAccount, creds: dict[str, Any]) -> None:
    """Best-effort. Errors are recorded into last_error, not raised."""
    try:
        adapter = adapter_for(acct, creds)
        if hasattr(adapter, "get_balance_snapshot"):
            bal = adapter.get_balance_snapshot()
            acct.cash = bal.get("cash")
            acct.buying_power = bal.get("buying_power")
            acct.total_equity = bal.get("total_equity")
            acct.currency = bal.get("currency")
            acct.balance_updated_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        acct.last_error = f"balance fetch failed: {str(exc)[:400]}"


def _evict_existing_brokers(db: Session, user: User, request: Request) -> None:
    """One-broker-per-user: delete existing broker_account rows for this user
    and stop their listeners before inserting a new one. Order rows survive
    (broker_account_id is SET NULL on delete).

    Req #1 (Option B): TRADERS can connect multiple brokers (e.g. both Alpaca
    and Webull) so they can pick which one to place each order from in the Trade
    Panel. Eviction is skipped for traders — each new broker connect simply adds
    a row. Subscribers remain one-broker-per-user (cleaner fanout semantics).
    """
    if user.role == UserRole.TRADER:
        # Traders: allow multiple broker accounts. No eviction.
        return

    existing = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
    ).scalars())
    for acct in existing:
        audit.record(
            db, actor_user_id=user.id, action="broker.replaced",
            entity_type="broker_account", entity_id=acct.id,
            metadata={"broker": acct.broker.value, "label": acct.label},
            ip_address=client_ip(request),
        )
        _stop_trader_listener(user.id, acct)
        db.delete(acct)
    if existing:
        db.flush()


# ── SnapTrade connect-session store (in-process, no Redis) ────────────────────
#
# The two-step SnapTrade flow must remember the user_secret between the
# "start portal" call and the "finish" call. App 2 deliberately avoids
# Redis, and start+finish hit the same single backend instance, so an
# in-process dict with a 30-min TTL is sufficient. (If App 2 is ever scaled
# to multiple instances, swap this for Redis or a DB row — see ARCHITECTURE.md.)

_SNAPTRADE_SESSIONS: dict[str, tuple[float, dict[str, Any]]] = {}
_SNAPTRADE_SESSION_TTL = 30 * 60  # seconds


def _save_snaptrade_session(user_id: uuid.UUID, payload: dict[str, Any]) -> None:
    _SNAPTRADE_SESSIONS[str(user_id)] = (time.time() + _SNAPTRADE_SESSION_TTL, payload)


def _load_snaptrade_session(user_id: uuid.UUID) -> dict[str, Any] | None:
    entry = _SNAPTRADE_SESSIONS.get(str(user_id))
    if entry is None:
        return None
    expires_at, payload = entry
    if time.time() > expires_at:
        _SNAPTRADE_SESSIONS.pop(str(user_id), None)
        return None
    return payload


def _clear_snaptrade_session(user_id: uuid.UUID) -> None:
    _SNAPTRADE_SESSIONS.pop(str(user_id), None)


def _ensure_snaptrade_configured() -> None:
    from app.brokers import snaptrade as snap
    if not snap.snaptrade_configured():
        raise HTTPException(
            503,
            "SnapTrade is not configured on this server "
            "(SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY).",
        )


def _attr_safe(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant attribute/key lookup — SnapTrade SDK responses are sometimes
    dict, sometimes typed."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _register_or_reset_snaptrade_user(user_id: uuid.UUID) -> str:
    """Register the SnapTrade user, handling 'already exists' by delete +
    re-register. Returns the userSecret (only issued once, at registration)."""
    from snaptrade_client.exceptions import ApiException
    from app.brokers import snaptrade as snap

    uid_str = str(user_id)
    try:
        return snap.register_user(uid_str)
    except ApiException as exc:
        status_code = getattr(exc, "status", None)
        body = getattr(exc, "body", None) or {}
        detail = body.get("detail") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None

        if status_code == 401:
            raise HTTPException(
                502,
                f"snaptrade_auth_failed: {detail or 'Unauthorized'} "
                f"(SnapTrade code={code}). Check SNAPTRADE_CLIENT_ID and "
                f"SNAPTRADE_CONSUMER_KEY — code 1076 means the consumer key is wrong.",
            ) from exc

        msg = str(detail or "").lower()
        looks_like_dupe = (
            (400 <= (status_code or 0) < 500)
            and ("already" in msg or "exists" in msg or "duplicate" in msg)
        )
        if not looks_like_dupe:
            raise HTTPException(
                502,
                f"snaptrade_error: {detail or exc} (status={status_code}, code={code})",
            ) from exc

        log.info("snaptrade register_user(%s) duplicate; deleting + retrying", user_id)
        try:
            snap._build_client().authentication.delete_snap_trade_user(  # noqa: SLF001
                user_id=uid_str
            )
        except ApiException:
            log.warning("snaptrade delete_snap_trade_user(%s) failed — re-registering anyway", user_id)
        try:
            return snap.register_user(uid_str)
        except ApiException as exc2:
            raise HTTPException(
                502, f"snaptrade_error_after_reset: {getattr(exc2, 'body', exc2)}",
            ) from exc2


# ── SnapTrade two-step connect ───────────────────────────────────────────────

@router.post("/snaptrade/webhook")
async def snaptrade_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Inbound SnapTrade webhook — UNAUTHENTICATED (SnapTrade calls this).
    On any event carrying a userId we recognise, schedule an immediate poll
    of that trader's orders so detection is near-instant instead of waiting
    for the next poll tick. Safe-by-design: a forged call can at most trigger
    a re-poll, which fetches REAL orders from SnapTrade and dedups — it can't
    inject fake trades. Signature verification is a TODO (log headers first).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    event_type = body.get("eventType") or body.get("type") or "unknown"
    user_id_raw = body.get("userId") or body.get("user_id")
    if not user_id_raw:
        log.info("snaptrade webhook: test/no-user event=%s", event_type)
        return {"ok": True}
    try:
        trader_user_id = uuid.UUID(str(user_id_raw))
    except (ValueError, TypeError):
        log.warning("snaptrade webhook: unparseable userId=%r", user_id_raw)
        return {"ok": True}
    try:
        from app.services import snaptrade_listener
        background.add_task(snaptrade_listener.poll_now_for_trader, trader_user_id)
    except ImportError:
        log.info("snaptrade webhook: snaptrade SDK not installed; ignoring")
    return {"ok": True}


@router.post("/snaptrade/start", response_model=StartSnaptradeOut)
def snaptrade_start(
    payload: StartSnaptradeIn,
    user: User = Depends(current_user),
) -> StartSnaptradeOut:
    """Step 1: register (or re-register) the SnapTrade user, cache the
    userSecret + label in a 30-min connect session, and return the hosted
    portal URL for the frontend to redirect into."""
    from app.brokers import snaptrade as snap
    _ensure_snaptrade_configured()
    user_secret = _register_or_reset_snaptrade_user(user.id)

    s = get_settings()
    custom_redirect = f"{s.frontend_base_url}/brokers?snaptrade_connected=1"
    try:
        portal_url = snap.make_login_url(
            user_id=str(user.id),
            user_secret=user_secret,
            custom_redirect=custom_redirect,
            broker_slug=payload.broker_slug,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("snaptrade make_login_url failed")
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc

    _save_snaptrade_session(user.id, {
        "user_secret": user_secret,
        "label":       payload.label,
        "paper":       bool(payload.paper),
        "broker_slug": payload.broker_slug,
    })
    return StartSnaptradeOut(portal_url=portal_url)


@router.post("/snaptrade/finish", response_model=BrokerAccountOut,
             status_code=status.HTTP_201_CREATED)
def snaptrade_finish(
    payload: FinishSnaptradeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    """Step 2: after the user returns from the portal, resolve the newest
    authorization + account, persist it as a BrokerAccount, and start the
    trader listener. Per-user advisory lock guards against a double-fired
    redirect creating duplicate rows."""
    from sqlalchemy import text
    from app.brokers import snaptrade as snap

    _ensure_snaptrade_configured()
    lock_key = hash(("snaptrade-finish", str(user.id))) & 0x7FFFFFFFFFFFFFFF
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    existing_snap = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.broker == BrokerName.SNAPTRADE,
        ).order_by(BrokerAccount.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if existing_snap is not None:
        log.info("snaptrade /finish: existing account for user %s; returning it", user.id)
        return existing_snap

    session = _load_snaptrade_session(user.id)
    if session is None:
        raise HTTPException(
            400, "no_snaptrade_session — start the portal flow first via "
            "POST /api/brokers/snaptrade/start",
        )
    user_secret = session["user_secret"]
    label = payload.label or session.get("label") or "SnapTrade"
    paper = bool(session.get("paper", False))

    try:
        auths = snap.list_authorizations(str(user.id), user_secret)
    except Exception as exc:  # noqa: BLE001
        log.exception("snaptrade list_authorizations failed")
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc
    if not auths:
        raise HTTPException(
            400, "no_connection_found — the portal closed without completing. "
            "Click 'Connect via SnapTrade' to try again.",
        )

    newest = sorted(
        auths,
        key=lambda a: str(_attr_safe(a, "created_date", "createdDate", default="")),
        reverse=True,
    )[0]
    auth_id = str(_attr_safe(newest, "id", "authorizationId"))
    brokerage = _attr_safe(newest, "brokerage", default={}) or {}
    brokerage_name = str(_attr_safe(brokerage, "name", default="SnapTrade Brokerage"))
    brokerage_slug = str(_attr_safe(brokerage, "slug", default=""))
    auth_type = str(_attr_safe(newest, "type", default="read")).lower()

    try:
        accounts = snap.list_accounts(str(user.id), user_secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc
    matching = [
        a for a in accounts
        if str(_attr_safe(_attr_safe(a, "brokerage_authorization", default={}), "id",
                          default=_attr_safe(a, "brokerage_authorization_id", default=""))) == auth_id
    ] or accounts
    if not matching:
        raise HTTPException(
            400, "no_account_found — authorization exists but no accounts attached.",
        )
    account_obj = matching[0]
    account_id = str(_attr_safe(account_obj, "id", "accountId"))
    account_number = str(_attr_safe(account_obj, "number", "account_number", default="") or "")

    creds: dict[str, Any] = {
        "snaptrade_user_id":     str(user.id),
        "snaptrade_user_secret": user_secret,
        "authorization_id":      auth_id,
        "account_id":            account_id,
        "brokerage_name":        brokerage_name,
        "brokerage_slug":        brokerage_slug,
        "paper":                 paper,
        "auth_type":             auth_type,
    }

    # A subscriber needs trade permission to receive mirror orders. If the
    # broker only granted read (e.g. some brokers via SnapTrade), block the
    # connect with a clear message rather than silently failing every fanout.
    if user.role == UserRole.SUBSCRIBER and auth_type != "trade":
        _clear_snaptrade_session(user.id)
        raise HTTPException(
            400,
            f"snaptrade_read_only — {brokerage_name} only granted read-only access "
            f"through SnapTrade, so mirror orders can't be placed. Pick a different "
            f"broker or connect Alpaca directly.",
        )

    _evict_existing_brokers(db, user, request)

    acct = BrokerAccount(
        user_id=user.id,
        broker=BrokerName.SNAPTRADE,
        label=label,
        is_paper=paper,
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
        broker_account_number=account_number or None,
    )
    try:
        info = adapter_for(acct, creds).verify_connection()
        if info.broker_account_id:
            acct.broker_account_number = info.broker_account_id
        acct.connection_status = "connected"
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": "snaptrade", "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(400, f"snaptrade_verify_failed: {exc}")

    db.add(acct)
    db.flush()
    audit.record(
        db, actor_user_id=user.id, action="broker.connected",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": "snaptrade", "label": label,
                  "brokerage": brokerage_name, "account": acct.broker_account_number},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    memory_cache.invalidate_broker_accounts(user.id)
    _clear_snaptrade_session(user.id)
    _start_trader_listener(user, acct)
    return acct


# ── Webull MFA trigger ───────────────────────────────────────────────────────

@router.post("/webull/start-mfa", response_model=StartWebullMfaOut)
def webull_start_mfa(
    payload: StartWebullMfaIn,
    user: User = Depends(current_user),
) -> StartWebullMfaOut:
    """Trigger Webull to send the MFA code, using the same per-user device_id
    the follow-up POST /api/brokers call will use."""
    device_id = _webull_device_id(user.id)
    from app.brokers.webull import request_mfa
    try:
        request_mfa(payload.username, paper=payload.paper, device_id=device_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"webull_mfa_error: {exc}") from exc
    return StartWebullMfaOut(
        sent=True,
        message="MFA code sent. Enter it on the next step to finish connecting.",
    )


# ── Direct connect (Alpaca, IBKR, Webull) ────────────────────────────────────

@router.post("", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
def connect(
    payload: ConnectBrokerIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    creds = _credentials_for(payload, user.id)
    _evict_existing_brokers(db, user, request)

    acct = BrokerAccount(
        user_id=user.id,
        broker=payload.broker,
        label=payload.label,
        is_paper=bool(creds.get("paper", True)),
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
    )
    try:
        info = adapter_for(acct, creds).verify_connection()
        acct.broker_account_number = info.broker_account_id
        acct.supports_fractional = info.supports_fractional
        acct.connection_status = "connected"
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": payload.broker.value, "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(400, f"broker_error: {exc}")

    db.add(acct)
    db.flush()
    audit.record(
        db, actor_user_id=user.id, action="broker.connected",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": payload.broker.value, "label": payload.label,
                  "is_paper": acct.is_paper, "account": acct.broker_account_number},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    memory_cache.invalidate_broker_accounts(user.id)
    _start_trader_listener(user, acct)
    return acct


@router.get("", response_model=list[BrokerAccountOut])
def list_my_brokers(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[BrokerAccount]:
    return list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at.desc())
    ).scalars())


@router.post("/{account_id}/refresh-balance", response_model=BrokerAccountOut)
def refresh_balance(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    creds = decrypt_json(acct.encrypted_credentials)
    _refresh_balance_into(acct, creds)
    audit.record(
        db, actor_user_id=user.id, action="broker.balance_refreshed",
        entity_type="broker_account", entity_id=acct.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    return acct


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_broker(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")

    # For SnapTrade, also remove the upstream authorization (best-effort).
    if acct.broker == BrokerName.SNAPTRADE:
        try:
            from app.brokers import snaptrade as snap
            creds = decrypt_json(acct.encrypted_credentials)
            snap.delete_authorization(
                creds["snaptrade_user_id"],
                creds["snaptrade_user_secret"],
                creds["authorization_id"],
            )
        except Exception:  # noqa: BLE001
            log.warning("snaptrade delete_authorization on delete failed", exc_info=True)

    audit.record(
        db, actor_user_id=user.id, action="broker.deleted",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": acct.broker.value, "label": acct.label},
        ip_address=client_ip(request),
    )
    _stop_trader_listener(user.id, acct)
    db.delete(acct)
    db.commit()
    memory_cache.invalidate_broker_accounts(user.id)
