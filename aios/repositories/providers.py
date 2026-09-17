"""Queries over provider configuration and task routing."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import LLMProviderConfig, TaskModelRoute


def list_providers(session: Session, enabled_only: bool = False) -> list[LLMProviderConfig]:
    """Configured providers, default first, then alphabetically."""
    stmt = select(LLMProviderConfig)
    if enabled_only:
        stmt = stmt.where(LLMProviderConfig.enabled.is_(True))
    stmt = stmt.order_by(
        LLMProviderConfig.is_default.desc(),
        LLMProviderConfig.provider_id,
    )
    return list(session.scalars(stmt).unique())


def get_provider(session: Session, config_id: int) -> Optional[LLMProviderConfig]:
    return session.get(LLMProviderConfig, config_id)


def get_by_provider_id(session: Session, provider_id: str) -> Optional[LLMProviderConfig]:
    return session.scalar(
        select(LLMProviderConfig).where(
            LLMProviderConfig.provider_id == (provider_id or "").strip().lower()
        )
    )


def default_provider(session: Session) -> Optional[LLMProviderConfig]:
    """The provider used when a task has no route of its own.

    Falls back to any enabled provider so a user who never pressed
    "Set default" still gets a working system.
    """
    row = session.scalar(
        select(LLMProviderConfig).where(
            LLMProviderConfig.is_default.is_(True), LLMProviderConfig.enabled.is_(True)
        )
    )
    if row is not None:
        return row
    return session.scalar(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.enabled.is_(True))
        .order_by(LLMProviderConfig.id)
        .limit(1)
    )


def create_provider(session: Session, **fields) -> LLMProviderConfig:
    row = LLMProviderConfig(**fields)
    session.add(row)
    session.flush()
    return row


def set_default(session: Session, row: LLMProviderConfig) -> None:
    """Make one provider the default, clearing the flag everywhere else."""
    for other in session.scalars(select(LLMProviderConfig)):
        other.is_default = other.id == row.id
    row.enabled = True
    session.flush()


def delete_provider(session: Session, row: LLMProviderConfig) -> None:
    """Remove a provider. Routes pointing at it fall back to the default."""
    was_default = row.is_default
    session.delete(row)
    session.flush()
    if was_default:
        replacement = default_provider(session)
        if replacement is not None:
            replacement.is_default = True
            session.flush()


def count_providers(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(LLMProviderConfig)) or 0


def configured_providers(session: Session) -> list[LLMProviderConfig]:
    """Providers that could actually answer a request right now."""
    return [p for p in list_providers(session, enabled_only=True) if p.is_configured]


# --- task routing -----------------------------------------------------------

def list_routes(session: Session) -> list[TaskModelRoute]:
    stmt = select(TaskModelRoute).options(selectinload(TaskModelRoute.provider))
    return list(session.scalars(stmt).unique())


def routes_by_task(session: Session) -> dict[str, TaskModelRoute]:
    return {route.task: route for route in list_routes(session)}


def get_route(session: Session, task: str) -> Optional[TaskModelRoute]:
    stmt = (
        select(TaskModelRoute)
        .options(selectinload(TaskModelRoute.provider))
        .where(TaskModelRoute.task == task)
    )
    return session.scalars(stmt).unique().one_or_none()


def set_route(
    session: Session, task: str, provider_config_id: Optional[int], model: str = ""
) -> TaskModelRoute:
    """Point a task at a provider/model, or clear it back to the default."""
    route = get_route(session, task)
    if route is None:
        route = TaskModelRoute(task=task)
        session.add(route)
    route.provider_config_id = provider_config_id
    route.model = (model or "").strip()
    session.flush()
    return route


def clear_route(session: Session, task: str) -> None:
    route = get_route(session, task)
    if route is not None:
        session.delete(route)
        session.flush()
