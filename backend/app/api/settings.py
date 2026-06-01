from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_subscriber, require_trader
from app.database import get_db
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.settings import (
    DailyLossLimitIn,
    DailyLossLimitPctIn,
    ExcludedSymbolsIn,
    FollowTraderIn,
    MaxDrawdownPctIn,
    PerTradeLossLimitPctIn,
    StopLossPctIn,
    SubscriberRetryIntervalIn,
    SubscriberSelfMultiplierIn,
    SubscriberSettingsOut,
    SubscriberToggleIn,
    TakeProfitPctIn,
    TraderDefaultBrokerIn,
    TraderMirrorExternalIn,
    TraderMirrorOnlyFilledIn,
    TraderSettingsOut,
    TraderToggleIn,
)
from app.services.pnl import get_account_equity, today_realized_pnl
from app.services import audit


def _sub_out(s: SubscriberSettings, db: Session) -> SubscriberSettingsOut:
    """Build the full subscriber-settings response."""
    return SubscriberSettingsOut(
        user_id=s.user_id,
        following_trader_id=s.following_trader_id,
        copy_enabled=s.copy_enabled,
        multiplier=s.multiplier,
        daily_loss_limit=s.daily_loss_limit,
        daily_loss_limit_pct=s.daily_loss_limit_pct,
        per_trade_loss_limit_pct=s.per_trade_loss_limit_pct,
        max_drawdown_pct=s.max_drawdown_pct,
        max_drawdown_equity_baseline=s.max_drawdown_equity_baseline,
        retry_interval_open=s.retry_interval_open,
        retry_interval_close=s.retry_interval_close,
        todays_realized_pnl=today_realized_pnl(db, s.user_id),
        excluded_symbols=list(s.excluded_symbols or []),
        take_profit_pct=s.take_profit_pct,
        stop_loss_pct=s.stop_loss_pct,
    )

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/subscriber", response_model=SubscriberSettingsOut)
def get_subscriber_settings(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber)
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return _sub_out(s, db)


@router.patch("/subscriber/retry-intervals", response_model=SubscriberSettingsOut)
def set_retry_intervals(
    payload: SubscriberRetryIntervalIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Update one or both of the subscriber's retry intervals. Each
    field is optional in the request so the frontend can update one
    dropdown without sending the other.

    Default for both = NEVER (no retry, REJECTED on first failure —
    the legacy behaviour before this feature shipped)."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old_open = s.retry_interval_open
    old_close = s.retry_interval_close
    if payload.retry_interval_open is not None:
        s.retry_interval_open = payload.retry_interval_open
    if payload.retry_interval_close is not None:
        s.retry_interval_close = payload.retry_interval_close
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.retry_intervals_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old_open": old_open.value if hasattr(old_open, "value") else str(old_open),
            "new_open": s.retry_interval_open.value,
            "old_close": old_close.value if hasattr(old_close, "value") else str(old_close),
            "new_close": s.retry_interval_close.value,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return _sub_out(s, db)


@router.patch("/subscriber/daily-loss-limit", response_model=SubscriberSettingsOut)
def set_daily_loss_limit(
    payload: DailyLossLimitIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_loss_limit
    s.daily_loss_limit = payload.daily_loss_limit
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.daily_loss_limit_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.daily_loss_limit) if payload.daily_loss_limit is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return _sub_out(s, db)


@router.patch("/subscriber/copy", response_model=SubscriberSettingsOut)
def toggle_copy(
    payload: SubscriberToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    s.copy_enabled = payload.copy_enabled
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.copy_toggled",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"copy_enabled": payload.copy_enabled},
        ip_address=client_ip(request),
    )
    db.commit()
    # Keep the queue fanout's in-memory subscriber cache in sync so the next
    # detected order sees this copy_enabled change immediately.
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return s


@router.patch("/subscriber/multiplier", response_model=SubscriberSettingsOut)
def set_own_multiplier(
    payload: SubscriberSelfMultiplierIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = str(s.multiplier)
    s.multiplier = payload.multiplier
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.multiplier_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"old": old, "new": str(payload.multiplier)},
        ip_address=client_ip(request),
    )
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return s


@router.patch("/subscriber/follow", response_model=SubscriberSettingsOut)
def follow_trader(
    payload: FollowTraderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    if payload.trader_id is not None:
        trader = db.get(User, payload.trader_id)
        if not trader or trader.role != UserRole.TRADER:
            raise HTTPException(404, "trader_not_found")
    s.following_trader_id = payload.trader_id
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.follow_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"trader_id": str(payload.trader_id) if payload.trader_id else None},
        ip_address=client_ip(request),
    )
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return s


# ── Percentage-based risk controls ──────────────────────────────────────────

@router.patch("/subscriber/daily-loss-limit-pct", response_model=SubscriberSettingsOut)
def set_daily_loss_limit_pct(
    payload: DailyLossLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the daily loss limit as a % of account equity."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_loss_limit_pct
    s.daily_loss_limit_pct = payload.daily_loss_limit_pct
    audit.record(
        db, actor_user_id=user.id, action="subscriber.daily_loss_limit_pct_changed",
        entity_type="subscriber_settings", entity_id=user.id,
        metadata={"old": str(old) if old is not None else None,
                  "new": str(payload.daily_loss_limit_pct) if payload.daily_loss_limit_pct is not None else None},
        ip_address=client_ip(request),
    )
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


@router.patch("/subscriber/per-trade-loss-limit", response_model=SubscriberSettingsOut)
def set_per_trade_loss_limit(
    payload: PerTradeLossLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the per-trade loss limit as a % of account equity."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.per_trade_loss_limit_pct
    s.per_trade_loss_limit_pct = payload.per_trade_loss_limit_pct
    audit.record(
        db, actor_user_id=user.id, action="subscriber.per_trade_loss_limit_changed",
        entity_type="subscriber_settings", entity_id=user.id,
        metadata={"old": str(old) if old is not None else None,
                  "new": str(payload.per_trade_loss_limit_pct) if payload.per_trade_loss_limit_pct is not None else None},
        ip_address=client_ip(request),
    )
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


@router.patch("/subscriber/max-drawdown", response_model=SubscriberSettingsOut)
def set_max_drawdown(
    payload: MaxDrawdownPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) max-drawdown protection. When enabled, the current
    account equity is captured as the baseline drawdown is measured against."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old_pct = s.max_drawdown_pct
    s.max_drawdown_pct = payload.max_drawdown_pct
    if payload.max_drawdown_pct is not None:
        s.max_drawdown_equity_baseline = get_account_equity(db, user.id)
    else:
        s.max_drawdown_equity_baseline = None
    audit.record(
        db, actor_user_id=user.id, action="subscriber.max_drawdown_changed",
        entity_type="subscriber_settings", entity_id=user.id,
        metadata={"old": str(old_pct) if old_pct is not None else None,
                  "new": str(payload.max_drawdown_pct) if payload.max_drawdown_pct is not None else None,
                  "equity_baseline": str(s.max_drawdown_equity_baseline) if s.max_drawdown_equity_baseline is not None else None},
        ip_address=client_ip(request),
    )
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


@router.get("/trader", response_model=TraderSettingsOut)
def get_trader_settings(
    db: Session = Depends(get_db), user: User = Depends(require_trader)
) -> TraderSettings:
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return s


@router.patch("/trader", response_model=TraderSettingsOut)
def toggle_trading(
    payload: TraderToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> TraderSettings:
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    s.trading_enabled = payload.trading_enabled
    audit.record(
        db,
        actor_user_id=user.id,
        action="trader.trading_toggled",
        entity_type="trader_settings",
        entity_id=user.id,
        metadata={"trading_enabled": payload.trading_enabled},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.patch("/trader/mirror-external", response_model=TraderSettingsOut)
def toggle_mirror_external(
    payload: TraderMirrorExternalIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> TraderSettings:
    """Opt the trader in/out of having their direct-at-broker trades
    mirrored to subscribers. When True, the trade-update stream watches the
    trader's broker accounts and triggers fanout for any order placed
    outside our Trade Panel. Default-off so this is always an explicit
    decision by the trader."""
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.mirror_external_trades
    s.mirror_external_trades = payload.mirror_external_trades
    audit.record(
        db,
        actor_user_id=user.id,
        action="trader.mirror_external_toggled",
        entity_type="trader_settings",
        entity_id=user.id,
        metadata={"old": old, "new": payload.mirror_external_trades},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.patch("/trader/mirror-only-filled", response_model=TraderSettingsOut)
def set_mirror_only_filled(
    payload: TraderMirrorOnlyFilledIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> TraderSettings:
    """Req #3: toggle whether only FILLED orders are mirrored (vs immediate)."""
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    s.mirror_only_filled = payload.mirror_only_filled
    audit.record(db, actor_user_id=user.id, action="trader.mirror_only_filled_changed",
                 entity_type="trader_settings", entity_id=user.id,
                 metadata={"mirror_only_filled": payload.mirror_only_filled},
                 ip_address=client_ip(request))
    db.commit()
    db.refresh(s)
    return s


@router.patch("/trader/default-broker", response_model=TraderSettingsOut)
def set_default_broker(
    payload: TraderDefaultBrokerIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> TraderSettings:
    """Req #1: set (or clear) the default broker account for the Trade Panel dropdown."""
    from app.models.broker_account import BrokerAccount
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    if payload.default_broker_account_id is not None:
        acct = db.get(BrokerAccount, payload.default_broker_account_id)
        if not acct or acct.user_id != user.id:
            raise HTTPException(404, "broker_account_not_found")
    s.default_broker_account_id = payload.default_broker_account_id
    audit.record(db, actor_user_id=user.id, action="trader.default_broker_changed",
                 entity_type="trader_settings", entity_id=user.id,
                 metadata={"default_broker_account_id": str(payload.default_broker_account_id)
                           if payload.default_broker_account_id else None},
                 ip_address=client_ip(request))
    db.commit()
    db.refresh(s)
    return s


# ── Subscriber: exclusion list (Req #6) ────────────────────────────────────

@router.patch("/subscriber/excluded-symbols", response_model=SubscriberSettingsOut)
def set_excluded_symbols(
    payload: ExcludedSymbolsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Replace the subscriber's stock exclusion list (underlying tickers, e.g. AAPL).
    Pass [] to clear. Uppercase is enforced on the backend."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = list(s.excluded_symbols or [])
    s.excluded_symbols = [sym.upper().strip() for sym in payload.excluded_symbols if sym.strip()]
    audit.record(db, actor_user_id=user.id, action="subscriber.excluded_symbols_changed",
                 entity_type="subscriber_settings", entity_id=user.id,
                 metadata={"old": old, "new": s.excluded_symbols},
                 ip_address=client_ip(request))
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


# ── Subscriber: auto TP/SL (Req #4) ────────────────────────────────────────

@router.patch("/subscriber/take-profit", response_model=SubscriberSettingsOut)
def set_take_profit(
    payload: TakeProfitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the auto take-profit % above entry premium for option brackets."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.take_profit_pct
    s.take_profit_pct = payload.take_profit_pct
    audit.record(db, actor_user_id=user.id, action="subscriber.take_profit_changed",
                 entity_type="subscriber_settings", entity_id=user.id,
                 metadata={"old": str(old) if old is not None else None,
                           "new": str(payload.take_profit_pct) if payload.take_profit_pct is not None else None},
                 ip_address=client_ip(request))
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


@router.patch("/subscriber/stop-loss", response_model=SubscriberSettingsOut)
def set_stop_loss(
    payload: StopLossPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the auto stop-loss % below entry premium for option brackets."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.stop_loss_pct
    s.stop_loss_pct = payload.stop_loss_pct
    audit.record(db, actor_user_id=user.id, action="subscriber.stop_loss_changed",
                 entity_type="subscriber_settings", entity_id=user.id,
                 metadata={"old": str(old) if old is not None else None,
                           "new": str(payload.stop_loss_pct) if payload.stop_loss_pct is not None else None},
                 ip_address=client_ip(request))
    db.commit()
    from app.services import memory_cache
    memory_cache.invalidate_subscriber(user.id)
    db.refresh(s)
    return _sub_out(s, db)


@router.get("/traders", response_model=list[dict])
def list_available_traders(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    """Subscribers use this to find the trader to follow."""
    rows = db.execute(select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))).scalars()
    return [{"id": str(t.id), "display_name": t.display_name, "email": t.email} for t in rows]
