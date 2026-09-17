"""The intelligence core: source articles, events, observations and evidence.

The distinction this whole system rests on:

* :class:`RawArticle` - a document we fetched (evidence).
* :class:`IntelligenceEvent` - a *continuing* story tracked across many days.
* :class:`EventObservation` - what we learned about that event on one day.

A news item on a given day is an observation, not a new event.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    Boolean, Date, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UTCDateTime
from ..timeutil import utcnow


class EventStatus:
    ACTIVE = "active"
    WATCHING = "watching"
    RESOLVED = "resolved"
    ARCHIVED = "archived"

    ALL = (ACTIVE, WATCHING, RESOLVED, ARCHIVED)


class RawArticle(Base):
    """A collected document, kept forever so every report item stays traceable."""

    __tablename__ = "raw_articles"
    __table_args__ = (
        Index("ix_raw_articles_collected", "collected_at"),
        Index("ix_raw_articles_topic_date", "topic_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, default="")
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(300), default="")
    domain: Mapped[str] = mapped_column(String(200), default="", index=True)

    published_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    published_raw: Mapped[str] = mapped_column(String(120), default="")
    collected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="")

    trust_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    collector: Mapped[str] = mapped_column(String(32), default="")

    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    @property
    def evidence_text(self) -> str:
        """Body when we could extract it, otherwise the snippet."""
        return self.body_text or self.snippet or ""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RawArticle {self.id} {self.domain}>"


class IntelligenceEvent(Base, TimestampMixin):
    """A continuing storyline, tracked across runs."""

    __tablename__ = "intelligence_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_event_key"),
        Index("ix_event_module_status", "module_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    event_key: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")

    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(32), default=EventStatus.ACTIVE, index=True)

    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    first_seen_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    last_seen_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True, index=True)

    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    legacy_import: Mapped[bool] = mapped_column(Boolean, default=False)

    module: Mapped[Optional["MonitorModule"]] = relationship()  # type: ignore[name-defined]
    topic: Mapped[Optional["Topic"]] = relationship()  # type: ignore[name-defined]
    observations: Mapped[list["EventObservation"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventObservation.observation_date, EventObservation.id",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IntelligenceEvent {self.id} {self.event_key}>"


class EventObservation(Base):
    """What we learned about an event on one particular day."""

    __tablename__ = "event_observations"
    __table_args__ = (Index("ix_obs_event_date", "event_id", "observation_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("intelligence_events.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    observation_date: Mapped[dt.date] = mapped_column(Date, index=True)

    title: Mapped[str] = mapped_column(Text, default="")
    tag: Mapped[str] = mapped_column(String(64), default="")
    #: Strictly what the evidence says.
    fact_summary: Mapped[str] = mapped_column(Text, default="")
    #: Explicitly the analyst judgement, never presented as fact.
    assessment: Mapped[str] = mapped_column(Text, default="")

    importance: Mapped[int] = mapped_column(Integer, default=3)
    confidence: Mapped[str] = mapped_column(String(16), default="medium")

    #: Metrics literally present in the evidence; may be empty.
    structured_data_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_import: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    event: Mapped[IntelligenceEvent] = relationship(back_populates="observations")
    sources: Mapped[list["ObservationSource"]] = relationship(
        back_populates="observation", cascade="all, delete-orphan"
    )

    @property
    def metrics(self) -> dict[str, Any]:
        return self.structured_data_json or {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EventObservation {self.id} {self.observation_date}>"


class ObservationSource(Base):
    """Evidence link: which articles produced this observation."""

    __tablename__ = "observation_sources"
    __table_args__ = (
        UniqueConstraint("observation_id", "article_id", name="uq_obs_article"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("event_observations.id", ondelete="CASCADE"), index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("raw_articles.id", ondelete="CASCADE"), index=True
    )
    relevance: Mapped[float] = mapped_column(Float, default=1.0)

    observation: Mapped[EventObservation] = relationship(back_populates="sources")
    article: Mapped[RawArticle] = relationship()
