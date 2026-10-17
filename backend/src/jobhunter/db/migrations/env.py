"""Alembic environment: online mode only, against the URL that `alembic_runner.alembic_config` sets.

The URL contains the database password. This module never logs it and does not configure logging.
"""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "the Alembic config has no sqlalchemy.url; build it with alembic_runner.alembic_config"
        )
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported: migrate against a database (db.alembic_runner)")
run_migrations_online()
