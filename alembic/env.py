"""Alembic environment.

Metadata comes from the application models, and the URL from the application
config, so ``alembic`` and the app can never disagree about which database they
are talking to.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios.config import get_paths  # noqa: E402
from aios.models import Base  # noqa: E402

config = context.config

# fileConfig() resets the root logger. That is right for the `alembic` CLI but
# would wipe the application's handlers when aios.database stamps a new
# database at startup, so the app opts out via this attribute.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url", "").strip() or "data/aios.db" in config.get_main_option("sqlalchemy.url", ""):
    config.set_main_option("sqlalchemy.url", get_paths().database_url)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render custom column types as their plain SQLAlchemy equivalent.

    ``UTCDateTime`` is a thin TypeDecorator over ``DateTime``; emitting the
    latter keeps migration files free of imports from application code, which
    may be refactored long after the migration was written.
    """
    from aios.models.base import UTCDateTime

    if type_ == "type" and isinstance(obj, UTCDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites tables.
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
