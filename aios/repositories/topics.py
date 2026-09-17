"""Queries over topics, their search queries and their source hints.

Split out from :mod:`aios.repositories.modules` so module-level and topic-level
access stay independently testable.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import ExcludedKeyword, PreferredSource, SearchQuery, Topic


def get_topic(session: Session, topic_id: int) -> Optional[Topic]:
    stmt = (
        select(Topic)
        .options(
            selectinload(Topic.queries),
            selectinload(Topic.preferred_sources),
            selectinload(Topic.excluded_keywords),
            selectinload(Topic.module),
        )
        .where(Topic.id == topic_id)
    )
    return session.scalars(stmt).unique().one_or_none()


def list_for_module(session: Session, module_id: int, include_archived: bool = False) -> list[Topic]:
    stmt = select(Topic).options(selectinload(Topic.queries)).where(Topic.module_id == module_id)
    if not include_archived:
        stmt = stmt.where(Topic.archived.is_(False))
    stmt = stmt.order_by(Topic.sort_order, Topic.id)
    return list(session.scalars(stmt).unique())


def next_sort_order(session: Session, module_id: int) -> int:
    current = session.scalar(
        select(func.max(Topic.sort_order)).where(Topic.module_id == module_id)
    )
    return (current or 0) + 10


def create_topic(session: Session, **fields) -> Topic:
    topic = Topic(**fields)
    session.add(topic)
    session.flush()
    return topic


def archive_topic(session: Session, topic: Topic) -> None:
    """Soft delete so historical reports referencing this topic still render."""
    topic.archived = True
    topic.enabled = False
    session.flush()


def delete_topic(session: Session, topic: Topic) -> None:
    session.delete(topic)


def count_topics(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Topic)) or 0


# --- search queries ---------------------------------------------------------

def get_query(session: Session, query_id: int) -> Optional[SearchQuery]:
    return session.get(SearchQuery, query_id)


def create_query(session: Session, topic_id: int, query: str, priority: int = 0) -> SearchQuery:
    item = SearchQuery(topic_id=topic_id, query=query.strip(), priority=priority)
    session.add(item)
    session.flush()
    return item


def delete_query(session: Session, item: SearchQuery) -> None:
    session.delete(item)


def count_queries(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(SearchQuery)) or 0


# --- source hints -----------------------------------------------------------

def add_preferred_source(
    session: Session,
    domain: str,
    topic_id: Optional[int] = None,
    module_id: Optional[int] = None,
    priority: int = 5,
) -> PreferredSource:
    row = PreferredSource(
        domain=domain.strip().lower().lstrip("."),
        topic_id=topic_id,
        module_id=module_id,
        priority=priority,
    )
    session.add(row)
    session.flush()
    return row


def get_preferred_source(session: Session, source_id: int) -> Optional[PreferredSource]:
    return session.get(PreferredSource, source_id)


def delete_preferred_source(session: Session, row: PreferredSource) -> None:
    session.delete(row)


def add_excluded_keyword(
    session: Session, keyword: str, topic_id: Optional[int] = None, module_id: Optional[int] = None
) -> ExcludedKeyword:
    row = ExcludedKeyword(keyword=keyword.strip(), topic_id=topic_id, module_id=module_id)
    session.add(row)
    session.flush()
    return row


def get_excluded_keyword(session: Session, keyword_id: int) -> Optional[ExcludedKeyword]:
    return session.get(ExcludedKeyword, keyword_id)


def delete_excluded_keyword(session: Session, row: ExcludedKeyword) -> None:
    session.delete(row)


def global_excluded_keywords(session: Session) -> Sequence[str]:
    """Keywords that apply everywhere (not bound to a module or topic)."""
    stmt = select(ExcludedKeyword.keyword).where(
        ExcludedKeyword.enabled.is_(True),
        ExcludedKeyword.module_id.is_(None),
        ExcludedKeyword.topic_id.is_(None),
    )
    return list(session.scalars(stmt))
