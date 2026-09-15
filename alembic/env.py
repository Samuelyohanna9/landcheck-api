from __future__ import annotations

import os
from logging.config import fileConfig

from dotenv import load_dotenv

from alembic import context
from sqlalchemy import engine_from_config, pool

# app/db.py resolves DATABASE_URL the same way (load_dotenv() then os.getenv) - alembic runs as
# its own standalone process (e.g. `docker compose exec api alembic ...`), so without this it never
# sees the .env file the app itself reads, and silently falls back to the placeholder URL in
# alembic.ini instead (which pointed at localhost, not the "db" container).
load_dotenv()

from app.db_base import Base
from app.models import estate_foundation  # noqa: F401 - register Estate tables for autogeneration.
from app.models import estate_auth  # noqa: F401 - register Estate identity tables for autogeneration.

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return str(os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
