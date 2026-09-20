"""Published report structure.

The HTML/JSON files under ``data/reports`` are *export artifacts*. These tables
are the source of truth: every report item points back to the observation and,
through it, to the articles that justified it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import Date, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UTCDateTime
from ..timeutil import utcnow


class Report(Base):
    """One generated daily report."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("run_id", name="uq_report_run"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    report_date: Mapped[dt.date] = mapped_column(Date, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(Text, default="")

    #: Set when a Research Agent produced this report. NULL for Classic
    #: reports, including every report written before v2.2.
    research_topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_topics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: ``classic`` / ``agent``. Decides which detail chrome 简易版 renders.
    engine: Mapped[str] = mapped_column(String(16), default="classic", index=True)
    #: The agent's own coverage verdict, mirrored here so a report can still
    #: answer "was this complete?" after its run row is gone.
    coverage_status: Mapped[str] = mapped_column(String(16), default="")

    headline_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    trends_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    metrics_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)

    #: Source-coverage record for this report: which collectors were impaired
    #: while it was produced. Empty/None means coverage was not recorded (every
    #: report written before this column existed), which is *not* the same as
    #: "coverage was complete" - the UI only shows a banner when it says so.
    coverage_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    html_path: Mapped[str] = mapped_column(Text, default="")
    json_path: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    legacy_import: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    run: Mapped[Optional["MonitoringRun"]] = relationship()  # type: ignore[name-defined]
    research_topic: Mapped[Optional["ResearchTopic"]] = relationship()  # type: ignore[name-defined]
    sections: Mapped[list["ReportSection"]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportSection.sort_order, ReportSection.id",
    )

    @property
    def item_count(self) -> int:
        return sum(len(s.items) for s in self.sections)

    @property
    def new_count(self) -> int:
        """Items that were a first appearance when this report was published."""
        return sum(1 for s in self.sections for i in s.items if i.event_state == "new")

    @property
    def updated_count(self) -> int:
        """Items that continued an event already in the timeline."""
        return sum(1 for s in self.sections for i in s.items if i.event_state == "updated")

    @property
    def source_count(self) -> int:
        """Distinct evidence articles cited anywhere in this report."""
        seen: set[int] = set()
        for section in self.sections:
            for item in section.items:
                observation = item.observation
                if observation is None:
                    continue
                for link in observation.sources:
                    if link.article_id is not None:
                        seen.add(link.article_id)
        return len(seen)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Report {self.id} {self.report_date}>"


class ReportSection(Base):
    """One module's slice of a report.

    ``module_name`` is denormalised on purpose: modules can be archived later
    and historical reports must keep rendering exactly as published.
    """

    __tablename__ = "report_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )

    module_key: Mapped[str] = mapped_column(String(64), default="")
    module_name: Mapped[str] = mapped_column(String(200), default="")

    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    #: ``new`` when the module produced items, ``watch`` when nothing qualified.
    status: Mapped[str] = mapped_column(String(32), default="watch")
    summary: Mapped[str] = mapped_column(Text, default="")
    metrics_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    #: How collection went for this module: ``ok`` / ``no_candidates`` /
    #: ``partial_collection`` / ``collection_failed``. Decides whether an empty
    #: section may say "no new developments" or must say "we could not check".
    coverage_state: Mapped[str] = mapped_column(String(32), default="")

    report: Mapped[Report] = relationship(back_populates="sections")
    items: Mapped[list["ReportItem"]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="ReportItem.sort_order, ReportItem.id",
    )


class ReportItem(Base):
    """A single card in the report, anchored to an event observation."""

    __tablename__ = "report_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_section_id: Mapped[int] = mapped_column(
        ForeignKey("report_sections.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("intelligence_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    observation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("event_observations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    tag: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    fact_summary: Mapped[str] = mapped_column(Text, default="")
    assessment: Mapped[str] = mapped_column(Text, default="")

    importance: Mapped[int] = mapped_column(Integer, default=3)
    confidence: Mapped[str] = mapped_column(String(16), default="medium")
    #: ``new`` / ``updated`` relative to the event's own history at publish time.
    event_state: Mapped[str] = mapped_column(String(16), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    section: Mapped[ReportSection] = relationship(back_populates="items")
    event: Mapped[Optional["IntelligenceEvent"]] = relationship()  # type: ignore[name-defined]
    observation: Mapped[Optional["EventObservation"]] = relationship()  # type: ignore[name-defined]
