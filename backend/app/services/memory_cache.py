"""In-process subscriber cache used by the queue-based demo fanout.

The trader-side hot path can't afford a `SELECT subscriber_settings + broker_account`
on every detected order — at 100 subs that's ~2s of serial DB work even
on a healthy connection. So we keep the full subscriber set in a process-
local dict and reload entries whenever the underlying rows change.

Layout:
    cache[trader_id] -> list[SubscriberCacheEntry]

A SubscriberCacheEntry is a frozen snapshot of everything the worker
needs to decide eligibility and submit to the broker WITHOUT touching
the DB: subscriber settings (multiplier, copy_enabled, daily_loss_limit,
following_trader_id) and a list of broker accounts (id +
supports_fractional). Worker still opens its own session at submit time
to write the child Order and audit log, but the *hot path* and the
*per-worker gate* both read from here.

Concurrency: the dict + lists are mutated only under a single lock.
Readers (queue_fanout, workers) take snapshots without the lock — they
work off whatever the cache returned at call time. A settings change
mid-flight just means the next order sees the new value.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.settings import SubscriberSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    id: uuid.UUID
    supports_fractional: bool


@dataclass(frozen=True)
class SubscriberCacheEntry:
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None
    # Percentage-based risk controls (NULL = disabled).
    daily_loss_limit_pct: Decimal | None
    per_trade_loss_limit_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    max_drawdown_equity_baseline: Decimal | None
    # Req #6: tickers the subscriber refuses to copy (underlying, uppercase).
    excluded_symbols: tuple[str, ...]
    # Req #4: auto take-profit / stop-loss % for bracket orders (NULL = off).
    take_profit_pct: Decimal | None
    stop_loss_pct: Decimal | None
    broker_accounts: tuple[BrokerAccountSnapshot, ...]


_cache: dict[uuid.UUID, list[SubscriberCacheEntry]] = {}
_lock = threading.RLock()
_loaded = False


def _build_entry(db: Session, sub: SubscriberSettings) -> SubscriberCacheEntry:
    accounts = db.execute(
        select(BrokerAccount.id, BrokerAccount.supports_fractional).where(
            BrokerAccount.user_id == sub.user_id
        )
    ).all()
    return SubscriberCacheEntry(
        user_id=sub.user_id,
        following_trader_id=sub.following_trader_id,
        copy_enabled=sub.copy_enabled,
        multiplier=sub.multiplier,
        daily_loss_limit=sub.daily_loss_limit,
        daily_loss_limit_pct=sub.daily_loss_limit_pct,
        per_trade_loss_limit_pct=sub.per_trade_loss_limit_pct,
        max_drawdown_pct=sub.max_drawdown_pct,
        max_drawdown_equity_baseline=sub.max_drawdown_equity_baseline,
        excluded_symbols=tuple(s.upper() for s in (sub.excluded_symbols or [])),
        take_profit_pct=sub.take_profit_pct,
        stop_loss_pct=sub.stop_loss_pct,
        broker_accounts=tuple(
            BrokerAccountSnapshot(id=a.id, supports_fractional=a.supports_fractional)
            for a in accounts
        ),
    )


def load_all() -> None:
    """Walk every SubscriberSettings row and rebuild the cache. Called once
    at startup; tests can call it again to reset."""
    global _loaded
    with _lock:
        _cache.clear()
        with SessionLocal() as db:
            subs = db.execute(select(SubscriberSettings)).scalars().all()
            for sub in subs:
                if sub.following_trader_id is None:
                    continue
                entry = _build_entry(db, sub)
                _cache.setdefault(sub.following_trader_id, []).append(entry)
        _loaded = True
        total = sum(len(v) for v in _cache.values())
        log.info("memory_cache: loaded %d subscribers across %d traders",
                 total, len(_cache))


def subscribers_for_trader(trader_id: uuid.UUID) -> list[SubscriberCacheEntry]:
    """Hot-path read. Returns a list snapshot — caller is free to iterate
    without holding the lock."""
    with _lock:
        return list(_cache.get(trader_id, ()))


def get_subscriber(user_id: uuid.UUID) -> SubscriberCacheEntry | None:
    """Worker-side read: fetch one subscriber's snapshot for gate evaluation."""
    with _lock:
        for entries in _cache.values():
            for e in entries:
                if e.user_id == user_id:
                    return e
    return None


def get_subscriber_or_load(user_id: uuid.UUID) -> SubscriberCacheEntry | None:
    """Like get_subscriber, but on a cache MISS falls back to loading the
    subscriber from the DB and populating the cache, instead of treating a
    miss as 'not following'.

    Why this matters: the cache is process-local and loaded at startup. A
    subscriber created/changed out-of-band (e.g. by the seed script, a DB
    import, or simply after this process booted) won't be in this process's
    cache. Without the fallback a worker would wrongly fail the copy as
    'copy_disabled'. With it, a cold/stale cache self-heals on first use —
    the DB stays the source of truth; the cache is just an accelerator."""
    e = get_subscriber(user_id)
    if e is not None:
        return e
    with SessionLocal() as db:
        sub = db.get(SubscriberSettings, user_id)
        if sub is None or sub.following_trader_id is None:
            return None
        entry = _build_entry(db, sub)
    with _lock:
        bucket = _cache.setdefault(entry.following_trader_id, [])
        # De-dupe in case a concurrent worker just loaded the same subscriber.
        _cache[entry.following_trader_id] = [
            x for x in bucket if x.user_id != user_id
        ] + [entry]
    return entry


def invalidate_subscriber(user_id: uuid.UUID) -> None:
    """Reload one subscriber's row from the DB and reposition it under the
    correct trader_id bucket. Call this from any endpoint that mutates
    SubscriberSettings (follow / unfollow / multiplier / toggle copy /
    daily_loss_limit) so the in-memory snapshot stays consistent."""
    with _lock:
        # Drop the subscriber from every bucket first.
        for trader_id in list(_cache.keys()):
            _cache[trader_id] = [e for e in _cache[trader_id] if e.user_id != user_id]
            if not _cache[trader_id]:
                del _cache[trader_id]

        with SessionLocal() as db:
            sub = db.get(SubscriberSettings, user_id)
            if sub is None or sub.following_trader_id is None:
                return
            entry = _build_entry(db, sub)
            _cache.setdefault(sub.following_trader_id, []).append(entry)


def invalidate_broker_accounts(user_id: uuid.UUID) -> None:
    """Convenience alias: broker-account create / delete also needs a refresh
    because the snapshot bakes in the account list."""
    invalidate_subscriber(user_id)


def snapshot_stats() -> dict:
    """For debug / health-check surfacing."""
    with _lock:
        return {
            "loaded": _loaded,
            "trader_count": len(_cache),
            "subscriber_count": sum(len(v) for v in _cache.values()),
        }
