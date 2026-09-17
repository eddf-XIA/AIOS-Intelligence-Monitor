"""Queries over intelligence events and their observations."""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import EventObservation, EventStatus, IntelligenceEvent, ObservationSource
from ..timeutil import utcnow


def get_event(session: Session, event_id: int) -> Optional[IntelligenceEvent]:
    stmt = (
        select(IntelligenceEvent)
        .options(
            selectinload(IntelligenceEvent.observations)
            .selectinload(EventObservation.sources)
            .selectinload(ObservationSource.article),
            selectinload(IntelligenceEvent.module),
            selectinload(IntelligenceEvent.topic),
        )
        .where(IntelligenceEvent.id == event_id)
    )
    return session.scalars(stmt).unique().one_or_none()


def get_by_key(session: Session, event_key: str) -> Optional[IntelligenceEvent]:
    return session.scalar(
        select(IntelligenceEvent).where(IntelligenceEvent.event_key == event_key)
    )


def candidate_events(
    session: Session,
    module_id: Optional[int],
    topic_id: Optional[int],
    lookback_days: int = 45,
    limit: int = 40,
) -> list[IntelligenceEvent]:
    """Stage-1 shortlist for event matching.

    Narrow by module/topic and recency first; the LLM only ever sees a small,
    plausible set rather than the whole history.
    """
    cutoff = utcnow() - dt.timedelta(days=max(lookback_days, 1))
    stmt = select(IntelligenceEvent).where(
        IntelligenceEvent.status.in_([EventStatus.ACTIVE, EventStatus.WATCHING]),
        IntelligenceEvent.last_seen_at >= cutoff,
    )
    if topic_id is not None:
        stmt = stmt.where(
            (IntelligenceEvent.topic_id == topic_id)
            | (IntelligenceEvent.module_id == module_id)
        )
    elif module_id is not None:
        stmt = stmt.where(IntelligenceEvent.module_id == module_id)
    stmt = stmt.order_by(IntelligenceEvent.last_seen_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def list_events(
    session: Session,
    status: Optional[str] = None,
    module_id: Optional[int] = None,
    term: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[IntelligenceEvent]:
    stmt = select(IntelligenceEvent).options(
        selectinload(IntelligenceEvent.module), selectinload(IntelligenceEvent.topic)
    )
    if status:
        stmt = stmt.where(IntelligenceEvent.status == status)
    if module_id:
        stmt = stmt.where(IntelligenceEvent.module_id == module_id)
    if term.strip():
        like = f"%{term.strip()}%"
        stmt = stmt.where(
            IntelligenceEvent.title.like(like) | IntelligenceEvent.summary.like(like)
        )
    stmt = stmt.order_by(IntelligenceEvent.last_seen_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(stmt).unique())


def count_events(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(IntelligenceEvent)) or 0


def create_event(session: Session, **fields) -> IntelligenceEvent:
    event = IntelligenceEvent(**fields)
    session.add(event)
    session.flush()
    return event


def add_observation(session: Session, **fields) -> EventObservation:
    observation = EventObservation(**fields)
    session.add(observation)
    session.flush()
    return observation


def link_sources(session: Session, observation_id: int, article_ids: Sequence[int]) -> int:
    """Attach evidence articles to an observation, ignoring duplicates."""
    linked = 0
    seen: set[int] = set()
    for article_id in article_ids:
        if article_id in seen:
            continue
        seen.add(article_id)
        exists = session.scalar(
            select(ObservationSource.id).where(
                ObservationSource.observation_id == observation_id,
                ObservationSource.article_id == article_id,
            )
        )
        if exists:
            continue
        session.add(ObservationSource(observation_id=observation_id, article_id=article_id))
        linked += 1
    session.flush()
    return linked


def observations_for_date(session: Session, report_date: dt.date) -> list[EventObservation]:
    stmt = (
        select(EventObservation)
        .options(selectinload(EventObservation.event))
        .where(EventObservation.observation_date == report_date)
        .order_by(EventObservation.importance, EventObservation.id)
    )
    return list(session.scalars(stmt).unique())


def latest_observation_before(
    session: Session, event_id: int, before_date: dt.date
) -> Optional[EventObservation]:
    """Most recent observation strictly before ``before_date`` - the Diff baseline."""
    stmt = (
        select(EventObservation)
        .where(
            EventObservation.event_id == event_id,
            EventObservation.observation_date < before_date,
        )
        .order_by(EventObservation.observation_date.desc(), EventObservation.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def get_observation(session: Session, observation_id: int) -> Optional[EventObservation]:
    stmt = (
        select(EventObservation)
        .options(
            selectinload(EventObservation.sources).selectinload(ObservationSource.article),
            selectinload(EventObservation.event),
        )
        .where(EventObservation.id == observation_id)
    )
    return session.scalars(stmt).unique().one_or_none()
