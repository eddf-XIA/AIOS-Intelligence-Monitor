"""Queries over generated reports."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    EventObservation,
    ObservationSource,
    Report,
    ReportItem,
    ReportSection,
)


def _full_options():
    return (
        selectinload(Report.sections)
        .selectinload(ReportSection.items)
        .selectinload(ReportItem.observation)
        .selectinload(EventObservation.sources)
        .selectinload(ObservationSource.article),
        selectinload(Report.sections)
        .selectinload(ReportSection.items)
        .selectinload(ReportItem.event),
    )


def get_report(session: Session, report_id: int) -> Optional[Report]:
    stmt = select(Report).options(*_full_options()).where(Report.id == report_id)
    return session.scalars(stmt).unique().one_or_none()


def get_by_date(session: Session, report_date: dt.date) -> Optional[Report]:
    """Latest report filed for a given calendar date."""
    stmt = (
        select(Report)
        .options(*_full_options())
        .where(Report.report_date == report_date)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).unique().first()


def get_by_run(session: Session, run_id: int) -> Optional[Report]:
    stmt = select(Report).options(*_full_options()).where(Report.run_id == run_id)
    return session.scalars(stmt).unique().one_or_none()


def list_reports(session: Session, limit: int = 60, offset: int = 0, term: str = "") -> list[Report]:
    stmt = select(Report).options(
        selectinload(Report.sections).selectinload(ReportSection.items), selectinload(Report.run)
    )
    if term.strip():
        like = f"%{term.strip()}%"
        stmt = stmt.where(Report.title.like(like))
    stmt = (
        stmt.order_by(Report.report_date.desc(), Report.id.desc()).limit(limit).offset(offset)
    )
    return list(session.scalars(stmt).unique())


def list_dates(session: Session, limit: int = 400) -> list[dt.date]:
    """Distinct report dates, newest first - drives the Compare pickers."""
    stmt = (
        select(Report.report_date)
        .group_by(Report.report_date)
        .order_by(Report.report_date.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def previous_report(session: Session, report: Report) -> Optional[Report]:
    """The report immediately preceding ``report`` in time."""
    stmt = (
        select(Report)
        .where(Report.report_date < report.report_date)
        .order_by(Report.report_date.desc(), Report.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def latest_report(session: Session) -> Optional[Report]:
    stmt = select(Report).order_by(Report.report_date.desc(), Report.id.desc()).limit(1)
    return session.scalars(stmt).first()


def count_reports(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Report)) or 0


def search_items(session: Session, term: str, limit: int = 60) -> list[ReportItem]:
    like = f"%{term.strip()}%"
    stmt = (
        select(ReportItem)
        .options(selectinload(ReportItem.section).selectinload(ReportSection.report))
        .where(
            ReportItem.title.like(like)
            | ReportItem.fact_summary.like(like)
            | ReportItem.assessment.like(like)
        )
        .order_by(ReportItem.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def delete_report(session: Session, report: Report) -> None:
    session.delete(report)
