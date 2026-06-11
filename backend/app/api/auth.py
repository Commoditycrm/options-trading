from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.auth import (
    ForgotPasswordIn,
    LoginIn,
    MessageOut,
    RegisterIn,
    ResetPasswordIn,
    TokenPair,
    UserOut,
)
from app.services import audit
from app.services.email import send_password_reset_email

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)) -> User:
    existing = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email_taken")

    # Multiple traders are allowed: each trader has their own subscribers
    # (SubscriberSettings.following_trader_id), the fanout cache is keyed by
    # trader_id, and subscribers pick who to follow via GET /settings/traders.
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name,
    )
    db.add(user)
    db.flush()

    if user.role == UserRole.TRADER:
        db.add(TraderSettings(user_id=user.id, trading_enabled=True))
    else:
        db.add(
            SubscriberSettings(
                user_id=user.id,
                copy_enabled=False,
                multiplier=Decimal("1.000"),
            )
        )

    audit.record(
        db,
        actor_user_id=user.id,
        action="user.register",
        entity_type="user",
        entity_id=user.id,
        metadata={"role": user.role.value},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        audit.record(
            db,
            actor_user_id=user.id if user else None,
            action="user.login_failed",
            metadata={"email": payload.email},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="user_inactive")

    audit.record(
        db,
        actor_user_id=user.id,
        action="user.login",
        ip_address=client_ip(request),
    )
    db.commit()
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(refresh_token: str, db: Session = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(refresh_token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong_token_type")
    import uuid as _uuid

    user = db.get(User, _uuid.UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user_inactive")
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


# ─── Password reset ───────────────────────────────────────────────────────────

_GENERIC_RESET_MESSAGE = (
    "If an account exists for that email, a reset link has been sent."
)


@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Email a password-reset link. Always returns the same generic message so
    the response never reveals whether an email is registered (no enumeration).
    The email send is deferred to a background task — keeps the response fast
    and timing-independent of whether the user exists."""
    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()

    if user is not None and user.is_active:
        token = create_reset_token(str(user.id))
        link = f"{get_settings().frontend_base_url}/reset-password?token={token}"
        background.add_task(send_password_reset_email, user.email, link)
        audit.record(
            db, actor_user_id=user.id, action="user.password_reset_requested",
            ip_address=client_ip(request),
        )
        db.commit()

    return MessageOut(message=_GENERIC_RESET_MESSAGE)


@router.post("/reset-password", response_model=MessageOut)
def reset_password(
    payload: ResetPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Consume a reset token and set a new password."""
    try:
        claims = decode_token(payload.token)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")
    if claims.get("type") != "password_reset":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    import uuid as _uuid
    try:
        user = db.get(User, _uuid.UUID(claims["sub"]))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    user.password_hash = hash_password(payload.password)
    audit.record(
        db, actor_user_id=user.id, action="user.password_reset_completed",
        ip_address=client_ip(request),
    )
    db.commit()
    return MessageOut(message="Password updated. You can now sign in.")
