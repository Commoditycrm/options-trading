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


engine = create_engine(_normalize_db_url(settings.database_url), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
