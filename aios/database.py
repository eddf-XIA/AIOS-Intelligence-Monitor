"""SQLAlchemy engine/session management for a threaded, single-user app.

The pipeline runs in worker threads and APScheduler jobs, so the two rules that
matter here are:

1. ``check_same_thread=False`` plus WAL, otherwise SQLite rejects cross-thread
   connections and readers block behind the writer.
2. Every unit of work takes its own session via :func:`session_scope`; sessions
   are never shared between threads.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import event, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Paths, get_paths

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker[Session]] = None


def _apply_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


def configure_engine(db_path: Path | str | None = None, echo: bool = False) -> Engine:
    """Create (or replace) the process-wide engine and session factory."""
    global _engine, _SessionFactory

    if db_path is None:
        url = get_paths().database_url
    elif str(db_path).startswith("sqlite"):
        url = str(db_path)
    else:
        url = f"sqlite:///{Path(db_path).as_posix()}"

    if _engine is not None:
        _engine.dispose()

    _engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(_engine, "connect", _apply_pragmas)
    _SessionFactory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        configure_engine()
    assert _SessionFactory is not None
    return _SessionFactory


def new_session() -> Session:
    """A fresh session the caller is responsible for closing."""
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    from .models import Base  # imported late so every model is registered

    Base.metadata.create_all(bind=get_engine())


def init_db(paths: Optional[Paths] = None, seed: bool = True) -> bool:
    """Create the database if needed and seed defaults on first launch.

    Returns True when this call created a brand-new database.
    """
    paths = paths or get_paths()
    paths.ensure()
    fresh = not paths.db_path.exists() or paths.db_path.stat().st_size == 0
    configure_engine(paths.db_path)
    create_all()
    _reconcile_added_columns()
    _sync_alembic_version(fresh)

    if seed:
        from .services.provider_migration import (
            migrate_legacy_provider,
            reconcile_key_state,
        )
        from .services.seed import seed_if_empty
        from .services.settings_service import ensure_default_settings

        with session_scope() as session:
            ensure_default_settings(session)
            seed_if_empty(session)
            # Carries pre-multi-provider installs onto the generic LLM layer.
            migrate_legacy_provider(session)
            # The provider rows mirror the OS vault; realign them so a drifted
            # mirror cannot block a run the credentials could actually serve.
            reconcile_key_state(session)

    if fresh:
        logger.info("Initialised new database at %s", paths.db_path)
    return fresh


#: Columns added to existing tables after a release shipped.
#:
#: ``create_all`` creates missing *tables* but never alters an existing one, and
#: an installed copy of AIOS is stamped at whatever revision it was created at
#: without anyone running ``alembic upgrade``. Without this reconciliation an
#: upgrade in place would start raising ``no such column`` on the first query.
#:
#: Additive only, and every entry must be nullable or have a default: this is a
#: safety net for a single-user desktop app, not a substitute for the migration
#: in ``alembic/versions`` (which remains the canonical schema history).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("reports", "coverage_json", "JSON"),
    ("report_sections", "coverage_state", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("module_runs", "collection_status", "VARCHAR(32) NOT NULL DEFAULT ''"),
)


def _reconcile_added_columns() -> None:
    """Add columns an older database is missing. Idempotent and additive."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column in columns:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("Added missing column %s.%s", table, column)
        except Exception as exc:  # pragma: no cover - surfaced on next query
            logger.warning("Could not add column %s.%s: %s", table, column, exc)


def _sync_alembic_version(fresh: bool) -> None:
    """Keep Alembic's bookkeeping consistent with ``create_all``.

    A brand-new database is stamped at head so future migrations apply cleanly.
    Alembic is optional: if it is not installed the app still runs.
    """
    try:
        from alembic import command
        from alembic.config import Config
        from .config import PROJECT_ROOT
    except Exception:  # pragma: no cover - alembic not installed
        return

    ini = PROJECT_ROOT / "alembic.ini"
    if not ini.exists():
        return

    from sqlalchemy import inspect

    engine = get_engine()
    try:
        has_version = inspect(engine).has_table("alembic_version")
        if has_version and not fresh:
            return
        cfg = Config(str(ini))
        cfg.attributes["configure_logger"] = False
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", str(engine.url))
        command.stamp(cfg, "head")
    except Exception as exc:  # pragma: no cover - non fatal bookkeeping
        logger.debug("Alembic stamp skipped: %s", exc)
