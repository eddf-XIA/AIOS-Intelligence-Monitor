"""Queries over research topics and their revision history."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    DEFAULT_WINDOW_HOURS,
    IntelligenceEvent,
    Report,
    ResearchTopic,
    ResearchTopicRevision,
)
from ..timeutil import utcnow

#: Fields a natural-language edit is allowed to change.
BRIEF_FIELDS = (
    "name",
    "brief",
    "scope",
    "focus_areas_json",
    "exclusions_json",
    "keywords_json",
    "regions",
    "window_hours",
)


def get_topic(session: Session, topic_id: int) -> Optional[ResearchTopic]:
    stmt = (
        select(ResearchTopic)
        .options(selectinload(ResearchTopic.revisions))
        .where(ResearchTopic.id == topic_id)
    )
    return session.scalars(stmt).unique().one_or_none()


def get_by_name(session: Session, name: str) -> Optional[ResearchTopic]:
    return session.scalar(
        select(ResearchTopic).where(ResearchTopic.name == (name or "").strip())
    )


def list_topics(
    session: Session, include_archived: bool = False, limit: int = 60
) -> list[ResearchTopic]:
    """Saved topics, most recently used first.

    Ordered by ``last_run_at`` rather than creation: 最近使用 on the Simple home
    should surface what the user actually researches, not what they happened to
    create first.
    """
    stmt = select(ResearchTopic)
    if not include_archived:
        stmt = stmt.where(ResearchTopic.archived.is_(False))
    stmt = (
        stmt.order_by(
            ResearchTopic.last_run_at.desc().nullslast(),
            ResearchTopic.updated_at.desc(),
            ResearchTopic.id.desc(),
        )
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def recent_topics(session: Session, limit: int = 6) -> list[ResearchTopic]:
    """The handful shown as 最近使用 chips."""
    return list_topics(session, limit=limit)


def count_topics(session: Session, include_archived: bool = False) -> int:
    stmt = select(func.count()).select_from(ResearchTopic)
    if not include_archived:
        stmt = stmt.where(ResearchTopic.archived.is_(False))
    return session.scalar(stmt) or 0


def unique_name(session: Session, name: str) -> str:
    """A topic name not already taken, suffixed only if it has to be."""
    base = (name or "研究主题").strip()[:180] or "研究主题"
    if get_by_name(session, base) is None:
        return base
    for suffix in range(2, 100):
        candidate = f"{base} {suffix}"[:200]
        if get_by_name(session, candidate) is None:
            return candidate
    return f"{base} {utcnow():%H%M%S}"[:200]


def create_topic(
    session: Session,
    name: str,
    brief: str = "",
    scope: str = "",
    focus_areas: Optional[list[str]] = None,
    exclusions: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    regions: str = "",
    window_hours: int = DEFAULT_WINDOW_HOURS,
    depth: str = "standard",
) -> ResearchTopic:
    """Save a new research topic. Name collisions are resolved, not rejected."""
    topic = ResearchTopic(
        name=unique_name(session, name),
        brief=(brief or "").strip(),
        scope=(scope or "").strip(),
        focus_areas_json=list(focus_areas or []) or None,
        exclusions_json=list(exclusions or []) or None,
        keywords_json=list(keywords or []) or None,
        regions=(regions or "").strip()[:200],
        window_hours=max(1, min(int(window_hours or DEFAULT_WINDOW_HOURS), 24 * 30)),
        depth=depth if depth in {"standard", "deep"} else "standard",
        version=1,
    )
    session.add(topic)
    session.flush()
    return topic


def update_topic(
    session: Session,
    topic: ResearchTopic,
    changes: dict[str, Any],
    change_note: str = "",
) -> ResearchTopic:
    """Apply an edit, snapshotting the previous brief first.

    The topic **id is never changed**. That is the point of this function: a
    natural-language edit ("以后多关注商业化") must leave every historical
    report, event and observation still attached to the same topic, which a
    create-new-and-abandon-the-old approach would silently break.
    """
    snapshot = ResearchTopicRevision(
        topic_id=topic.id,
        version=topic.version or 1,
        name=topic.name,
        brief=topic.brief,
        scope=topic.scope,
        focus_areas_json=topic.focus_areas_json,
        exclusions_json=topic.exclusions_json,
        keywords_json=topic.keywords_json,
        regions=topic.regions,
        window_hours=topic.window_hours,
        change_note=(change_note or "").strip()[:2000],
        created_at=utcnow(),
    )
    session.add(snapshot)

    if "name" in changes:
        wanted = (changes["name"] or "").strip()[:200]
        if wanted and wanted != topic.name:
            existing = get_by_name(session, wanted)
            # A rename onto another topic's name would violate the unique
            # constraint; keeping the current name is better than failing the
            # whole edit over a cosmetic field.
            if existing is None or existing.id == topic.id:
                topic.name = wanted

    for field in ("brief", "scope", "regions"):
        if field in changes:
            value = (changes[field] or "").strip()
            setattr(topic, field, value[:200] if field == "regions" else value)

    for field, key in (
        ("focus_areas", "focus_areas_json"),
        ("exclusions", "exclusions_json"),
        ("keywords", "keywords_json"),
    ):
        if field in changes:
            values = [str(v).strip() for v in (changes[field] or []) if str(v).strip()]
            setattr(topic, key, values or None)

    if "window_hours" in changes:
        try:
            topic.window_hours = max(1, min(int(changes["window_hours"]), 24 * 30))
        except (TypeError, ValueError):
            pass

    if "depth" in changes and changes["depth"] in {"standard", "deep"}:
        topic.depth = changes["depth"]

    topic.version = (topic.version or 1) + 1
    topic.updated_at = utcnow()
    session.flush()
    return topic


def set_schedule(
    session: Session, topic: ResearchTopic, enabled: bool, time_text: str = "08:00"
) -> ResearchTopic:
    """Turn 每天自动研究 on or off for one topic. No cron syntax involved."""
    topic.schedule_enabled = bool(enabled)
    cleaned = (time_text or "08:00").strip()[:5]
    topic.schedule_time = cleaned or "08:00"
    topic.updated_at = utcnow()
    session.flush()
    return topic


def scheduled_topics(session: Session) -> list[ResearchTopic]:
    """Topics with 每天自动研究 switched on."""
    stmt = (
        select(ResearchTopic)
        .where(
            ResearchTopic.schedule_enabled.is_(True),
            ResearchTopic.archived.is_(False),
        )
        .order_by(ResearchTopic.schedule_time, ResearchTopic.id)
    )
    return list(session.scalars(stmt).unique())


def mark_run(session: Session, topic_id: int, when: Optional[dt.datetime] = None) -> None:
    """Record that a research run started for this topic."""
    topic = session.get(ResearchTopic, topic_id)
    if topic is None:
        return
    topic.last_run_at = when or utcnow()
    topic.run_count = (topic.run_count or 0) + 1
    session.flush()


def archive_topic(session: Session, topic: ResearchTopic) -> None:
    """Hide a topic from 最近使用 without deleting anything it produced."""
    topic.archived = True
    topic.updated_at = utcnow()
    session.flush()


def restore_topic(session: Session, topic: ResearchTopic) -> None:
    topic.archived = False
    topic.updated_at = utcnow()
    session.flush()


# --- history ----------------------------------------------------------------

def reports_for_topic(session: Session, topic_id: int, limit: int = 60) -> list[Report]:
    """Every report this topic has produced, newest first."""
    from ..models import ReportItem, ReportSection

    stmt = (
        select(Report)
        .options(
            selectinload(Report.sections).selectinload(ReportSection.items),
            selectinload(Report.run),
        )
        .where(Report.research_topic_id == topic_id)
        .order_by(Report.report_date.desc(), Report.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def latest_report(session: Session, topic_id: int) -> Optional[Report]:
    reports = reports_for_topic(session, topic_id, limit=1)
    return reports[0] if reports else None


def section_names(session: Session, topic_id: int, limit: int = 12) -> list[str]:
    """Section names this topic's previous reports used.

    Passed to the agent as advisory structure so a topic's daily brief keeps a
    recognisable shape across days, which is what makes week-on-week comparison
    readable.
    """
    from ..models import ReportSection

    stmt = (
        select(ReportSection.module_name)
        .join(Report, Report.id == ReportSection.report_id)
        .where(Report.research_topic_id == topic_id)
        .group_by(ReportSection.module_name)
        .order_by(func.max(Report.report_date).desc())
        .limit(limit)
    )
    return [name for name in session.scalars(stmt) if name]


def count_events(session: Session, topic_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(IntelligenceEvent)
            .where(IntelligenceEvent.research_topic_id == topic_id)
        )
        or 0
    )


def delete_topic(session: Session, topic: ResearchTopic) -> None:
    """Remove a topic.

    Its events, observations and reports survive: the foreign keys are
    ``ON DELETE SET NULL`` because the intelligence a topic produced outlives
    the configuration that happened to discover it.
    """
    session.delete(topic)
    session.flush()
