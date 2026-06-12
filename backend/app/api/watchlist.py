"""Per-user watchlist of underlying tickers."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.database import get_db
from app.models.user import User
from app.models.watchlist import WatchlistItem

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistAddIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)


def _symbols(db: Session, user_id) -> list[str]:
    return list(db.execute(
        select(WatchlistItem.symbol)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.symbol)
    ).scalars())


@router.get("", response_model=list[str])
def list_watchlist(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[str]:
    return _symbols(db, user.id)


@router.post("", response_model=list[str])
def add_watchlist(
    payload: WatchlistAddIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[str]:
    """Add a ticker (idempotent — re-adding an existing symbol is a no-op).
    Stored uppercase."""
    sym = payload.symbol.strip().upper()
    if not sym:
        raise HTTPException(422, "empty_symbol")
    existing = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id, WatchlistItem.symbol == sym
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(WatchlistItem(user_id=user.id, symbol=sym))
        db.commit()
    return _symbols(db, user.id)


@router.delete("/{symbol}", response_model=list[str])
def remove_watchlist(
    symbol: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[str]:
    sym = symbol.strip().upper()
    item = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id, WatchlistItem.symbol == sym
        )
    ).scalar_one_or_none()
    if item is not None:
        db.delete(item)
        db.commit()
    return _symbols(db, user.id)
