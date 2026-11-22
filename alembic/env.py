"""Alembic environment for triage report storage.

The database URL comes from ``src.config.settings``, the same source the app uses,
so a migration run and the running service cannot end up pointed at different
databases. ``alembic.ini`` deliberately keeps ``sqlalchemy.url`` empty.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from src.config import settings
from src.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    url = settings.DATABASE_URL.strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic needs the URL the app uses, e.g. "
            '"postgresql+psycopg://triage:triage@localhost:5432/triage".'
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL for a target database without connecting to it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect using the app's URL and apply migrations against the live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
