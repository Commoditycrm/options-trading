from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.database import _normalize_db_url  # same coercion as runtime
from app.models import Base  # noqa: F401 — registers all model metadata

config = context.config
# Render's auto-generated DATABASE_URL uses the bare ``postgresql://`` scheme,
# which SQLAlchemy maps to psycopg2 by default. We only have psycopg (v3)
# installed; normalize to ``postgresql+psycopg://`` so alembic uses the
# right driver. Local dev URLs that already include +psycopg pass through.
config.set_main_option("sqlalchemy.url", _normalize_db_url(get_settings().database_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
