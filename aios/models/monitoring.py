"""Monitoring configuration: modules, topics, queries and source hints.

This is the tree the pipeline walks on every run. Nothing about *what* is
monitored is hard-coded in Python - adding a module/topic/query through the web
UI changes the next run with no code edit.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class MonitorModule(Base, TimestampMixin):
    """A top-level section of the daily report (e.g. 移动智能终端侧)."""

    __tablename__ = "monitor_modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    lookback_days: Mapped[int] = mapped_column(Integer, default=3)
    max_candidates: Mapped[int] = mapped_column(Integer, default=18)
    max_report_items: Mapped[int] = mapped_column(Integer, default=2)

    analysis_prompt: Mapped[str] = mapped_column(Text, default="")

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="module",
        cascade="all, delete-orphan",
        order_by="Topic.sort_order, Topic.id",
    )
    preferred_sources: Mapped[list["PreferredSource"]] = relationship(
        back_populates="module",
        cascade="all, delete-orphan",
        primaryjoin="MonitorModule.id == PreferredSource.module_id",
    )
    excluded_keywords: Mapped[list["ExcludedKeyword"]] = relationship(
        back_populates="module",
        cascade="all, delete-orphan",
        primaryjoin="MonitorModule.id == ExcludedKeyword.module_id",
    )

    @property
    def active_topics(self) -> list["Topic"]:
        return [t for t in self.topics if t.enabled and not t.archived]

    @property
    def topic_count(self) -> int:
        return len([t for t in self.topics if not t.archived])

    @property
    def query_count(self) -> int:
        return sum(len([q for q in t.queries if q.enabled]) for t in self.topics if not t.archived)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MonitorModule {self.key}>"


class Topic(Base, TimestampMixin):
    """A monitored subject inside a module (e.g. HarmonyOS)."""

    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("module_id", "name", name="uq_topic_module_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("monitor_modules.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    analysis_prompt: Mapped[str] = mapped_column(Text, default="")

    module: Mapped[MonitorModule] = relationship(back_populates="topics")
    queries: Mapped[list["SearchQuery"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        order_by="SearchQuery.priority.desc(), SearchQuery.id",
    )
    preferred_sources: Mapped[list["PreferredSource"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        primaryjoin="Topic.id == PreferredSource.topic_id",
    )
    excluded_keywords: Mapped[list["ExcludedKeyword"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        primaryjoin="Topic.id == ExcludedKeyword.topic_id",
    )

    @property
    def active_queries(self) -> list["SearchQuery"]:
        return [q for q in self.queries if q.enabled]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Topic {self.name}>"


class SearchQuery(Base, TimestampMixin):
    """One search expression executed against the collectors."""

    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)

    query: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    last_run_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)
    last_result_count: Mapped[int] = mapped_column(Integer, default=0)

    topic: Mapped[Topic] = relationship(back_populates="queries")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SearchQuery {self.query[:40]!r}>"


class PreferredSource(Base, TimestampMixin):
    """A domain that should outrank aggregators for a topic or module."""

    __tablename__ = "preferred_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="CASCADE"), nullable=True, index=True
    )
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), nullable=True, index=True
    )

    domain: Mapped[str] = mapped_column(String(200), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    module: Mapped[Optional[MonitorModule]] = relationship(
        back_populates="preferred_sources", foreign_keys=[module_id]
    )
    topic: Mapped[Optional[Topic]] = relationship(
        back_populates="preferred_sources", foreign_keys=[topic_id]
    )


class ExcludedKeyword(Base, TimestampMixin):
    """A keyword that disqualifies an article (招聘, 二手, 优惠券 ...)."""

    __tablename__ = "excluded_keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="CASCADE"), nullable=True, index=True
    )
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), nullable=True, index=True
    )

    keyword: Mapped[str] = mapped_column(String(200), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    module: Mapped[Optional[MonitorModule]] = relationship(
        back_populates="excluded_keywords", foreign_keys=[module_id]
    )
    topic: Mapped[Optional[Topic]] = relationship(
        back_populates="excluded_keywords", foreign_keys=[topic_id]
    )
