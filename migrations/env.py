"""Alembic environment.

Two things worth knowing:

1. The database URL comes from the DATABASE_URL environment variable, not from
   alembic.ini. Credentials stay out of the repository.

2. We use the async engine, the same one the app uses, so there is a single
   driver (asyncpg) rather than one for the app and another for migrations.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import get_settings

# Importing this package registers every model on Base.metadata.
# Without it, autogenerate would think the database should be empty.
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    """Configure and run, both inside the same synchronous context.

    Alembic's context is not async-aware, so we hand it a plain connection via
    run_sync. Configuring and running must happen together in that one call.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes (VARCHAR(50) -> VARCHAR(100)), which
        # autogenerate ignores by default.
        compare_type=True,
        # Detect changes to server defaults too.
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Generate SQL without connecting, via `alembic upgrade head --sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
