"""Queries over monitor modules.

Topic/query level access lives in :mod:`aios.repositories.topics`.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import MonitorModule, Topic


def _loaded():
    return (
        selectinload(MonitorModule.topics).selectinload(Topic.queries),
        selectinload(MonitorModule.preferred_sources),
        selectinload(MonitorModule.excluded_keywords),
    )


def list_modules(session: Session, include_archived: bool = False) -> list[MonitorModule]:
    stmt = select(MonitorModule).options(*_loaded())
    if not include_archived:
        stmt = stmt.where(MonitorModule.archived.is_(False))
    stmt = stmt.order_by(MonitorModule.sort_order, MonitorModule.id)
    return list(session.scalars(stmt).unique())


def list_enabled_modules(session: Session) -> list[MonitorModule]:
    """Modules the next run will execute, in display order."""
    stmt = (
        select(MonitorModule)
        .options(*_loaded())
        .where(MonitorModule.enabled.is_(True), MonitorModule.archived.is_(False))
        .order_by(MonitorModule.sort_order, MonitorModule.id)
    )
    return list(session.scalars(stmt).unique())


def get_module(session: Session, module_id: int) -> Optional[MonitorModule]:
    stmt = select(MonitorModule).options(*_loaded()).where(MonitorModule.id == module_id)
    return session.scalars(stmt).unique().one_or_none()


def get_module_by_key(session: Session, key: str) -> Optional[MonitorModule]:
    stmt = select(MonitorModule).options(*_loaded()).where(MonitorModule.key == key)
    return session.scalars(stmt).unique().one_or_none()


def count_modules(session: Session, include_archived: bool = False) -> int:
    stmt = select(func.count()).select_from(MonitorModule)
    if not include_archived:
        stmt = stmt.where(MonitorModule.archived.is_(False))
    return session.scalar(stmt) or 0


def next_sort_order(session: Session) -> int:
    current = session.scalar(select(func.max(MonitorModule.sort_order)))
    return (current or 0) + 10


def create_module(session: Session, **fields) -> MonitorModule:
    module = MonitorModule(**fields)
    session.add(module)
    session.flush()
    return module


def archive_module(session: Session, module: MonitorModule) -> None:
    """Soft delete: history keeps rendering, the module stops being monitored."""
    module.archived = True
    module.enabled = False
    session.flush()


def restore_module(session: Session, module: MonitorModule) -> None:
    module.archived = False
    session.flush()


def delete_module(session: Session, module: MonitorModule) -> None:
    """Hard delete. Only offered for modules that never produced a report."""
    session.delete(module)


def key_exists(session: Session, key: str, exclude_id: Optional[int] = None) -> bool:
    stmt = select(func.count()).select_from(MonitorModule).where(MonitorModule.key == key)
    if exclude_id is not None:
        stmt = stmt.where(MonitorModule.id != exclude_id)
    return bool(session.scalar(stmt))
