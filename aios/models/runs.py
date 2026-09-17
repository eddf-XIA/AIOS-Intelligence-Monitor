"""Execution records: runs, per-module progress, logs and LLM usage."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UTCDateTime
from ..timeutil import utcnow


class RunStatus:
    """Allowed values for :attr:`MonitoringRun.status`."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = {COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED}
    ACTIVE = {PENDING, RUNNING}


class ModuleRunStatus:
    """Allowed values for :attr:`ModuleRun.status` - drives the dashboard."""

    PENDING = "pending"
    COLLECTING = "collecting"
    EXTRACTING = "extracting"
    ANALYZING = "analyzing"
    MATCHING = "matching"
    COMPLETED = "completed"
    #: Finished and produced usable output, but at least one source failed.
    #: A module is not "failed" merely because an optional source was down.
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    SKIPPED = "skipped"

    TERMINAL = {COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, SKIPPED}


class MonitoringRun(Base):
    """One end-to-end monitoring execution."""

    __tablename__ = "monitoring_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    started_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual")

    report_date: Mapped[dt.date] = mapped_column(Date, index=True)

    total_candidates: Mapped[int] = mapped_column(Integer, default=0)
    total_articles: Mapped[int] = mapped_column(Integer, default=0)
    total_events: Mapped[int] = mapped_column(Integer, default=0)
    total_new_events: Mapped[int] = mapped_column(Integer, default=0)
    total_updated_events: Mapped[int] = mapped_column(Integer, default=0)
    total_report_items: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    module_runs: Mapped[list["ModuleRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ModuleRun.id"
    )
    logs: Mapped[list["RunLog"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunLog.id"
    )
    usages: Mapped[list["LLMUsage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status in RunStatus.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MonitoringRun {self.id} {self.status}>"


class ModuleRun(Base):
    """Per-module progress inside a :class:`MonitoringRun`."""

    __tablename__ = "module_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("monitoring_runs.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("monitor_modules.id", ondelete="CASCADE"), index=True)

    module_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(32), default=ModuleRunStatus.PENDING, index=True)

    collect_started_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    collect_finished_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    analysis_started_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    analysis_finished_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)

    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Collection outcome for this module, from
    #: :class:`aios.services.collection_planner.TopicCollectionStatus`.
    #: Stored separately from ``status`` because "the module finished" and
    #: "the sources answered" are different facts.
    collection_status: Mapped[str] = mapped_column(String(32), default="")

    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    run: Mapped[MonitoringRun] = relationship(back_populates="module_runs")
    module: Mapped["MonitorModule"] = relationship()  # type: ignore[name-defined]


class RunLog(Base):
    """Timestamped progress line shown on ``/runs/{id}``.

    Never write secrets here - :func:`aios.services.keyring_service.redact`
    is applied by the pipeline logger before anything is persisted.
    """

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("monitoring_runs.id", ondelete="CASCADE"), index=True)

    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    module_key: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[MonitoringRun] = relationship(back_populates="logs")


class LLMUsage(Base):
    """One model call. Token counts are only stored when the API reports them.

    Providers that do not report usage leave the token columns NULL rather than
    having an estimate invented for them.
    """

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )

    purpose: Mapped[str] = mapped_column(String(64), default="", index=True)
    module_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Which backend answered, e.g. ``deepseek`` / ``qwen`` / ``ollama``.
    provider_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    success: Mapped[bool] = mapped_column(default=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    extra_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    run: Mapped[Optional[MonitoringRun]] = relationship(back_populates="usages")
