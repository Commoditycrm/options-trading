import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()


def _normalize_db_url(url: str) -> str:
    """Render (and other managed Postgres providers) hand out URLs starting
    with ``postgresql://`` — SQLAlchemy 2.x defaults that prefix to the
    psycopg2 driver, but we only have psycopg (v3) installed. Coerce to
    ``postgresql+psycopg://`` so the right driver is picked.

    Idempotent: an explicit ``postgresql+psycopg://`` URL passes through
    untouched. Also handles ``postgres://`` (the legacy Heroku-style scheme
    Render still emits in some cases)."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


# Connection-pool sizing — critical for the queue-based fanout. Each active
# subscriber_worker holds a DB connection while it claims a row + submits the
# mirror order, so the SQLAlchemy pool (NOT just Postgres max_connections) is
# the real concurrency ceiling. Default pool is size 5 + overflow 10 = 15,
# which throttles a 100-worker pool to 15 concurrent DB ops. Size the pool to
# the worker count + headroom for the API / poller / retry scheduler. Keep
# Postgres max_connections comfortably above this (see docker-compose.demo.yml
# and doc/ARCHITECTURE.md §8).
_worker_count = int(os.environ.get("QUEUE_DEMO_WORKER_COUNT", "0") or 0)
_pool_size = 20
_max_overflow = max(10, _worker_count + 20)

engine = create_engine(
    _normalize_db_url(settings.database_url),
    pool_pre_ping=True,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_timeout=30,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
