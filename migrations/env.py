"""Alembic environment for the isolated SQLAlchemy persistence projection."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from app.persistence.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config

postgres_test_database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
if postgres_test_database_url is not None:
    config.set_main_option("sqlalchemy.url", postgres_test_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations using the configured deployment or local development URL."""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
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
