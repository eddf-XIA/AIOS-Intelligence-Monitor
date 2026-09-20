"""Research topics - the user-facing concept behind 简易版.

A :class:`ResearchTopic` is what a normal user thinks they configured: "I want
to follow global smart terminals". Internally it is a *research brief* - scope,
focus areas, exclusions, a time window - which the Research Agent is given as
its instructions.

It deliberately does **not** replace :class:`~aios.models.MonitorModule`. The
Classic collection tree (modules → topics → queries) stays exactly as it was
and remains the Professional-mode model. A research topic is a second, simpler
entry point into the *same* intelligence core: the events and observations it
produces are ordinary :class:`~aios.models.IntelligenceEvent` rows, so history,
compare and reports work identically for both.

Revisions are kept because a topic is edited by natural language ("从现在起多
关注商业化"), and a user who does that must still be able to see what the brief
said when an older report was produced.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UTCDateTime
from ..timeutil import utcnow

#: Default research window. 72 hours is what the daily brief actually needs:
#: long enough to catch a Friday announcement on Monday, short enough that the
#: report is about *now*.
DEFAULT_WINDOW_HOURS = 72


class ResearchTopic(Base, TimestampMixin):
    """One saved research brief, with a stable id across natural-language edits.

    The id is stable on purpose: editing a topic by describing the change must
    keep every historical report, event and observation attached to it. A new id
    would silently orphan the timeline the product exists to maintain.
    """

    __tablename__ = "research_topics"
    __table_args__ = (UniqueConstraint("name", name="uq_research_topic_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(200), index=True)
    #: The natural-language research goal, as the user would describe it.
    brief: Mapped[str] = mapped_column(Text, default="")
    #: What is in scope - subject matter, not search syntax.
    scope: Mapped[str] = mapped_column(Text, default="")
    #: Plain strings, e.g. ["技术进展", "产品发布", "商业部署"].
    focus_areas_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    #: Plain strings, e.g. ["招聘", "纯营销", "重复转载"].
    exclusions_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    #: Geography in user words, e.g. "中国 + 全球".
    regions: Mapped[str] = mapped_column(String(200), default="")
    #: Named entities worth watching, shown as 关注 chips.
    keywords_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)

    window_hours: Mapped[int] = mapped_column(Integer, default=DEFAULT_WINDOW_HOURS)
    #: ``standard`` / ``deep``. Passed to the agent, never shown as jargon.
    depth: Mapped[str] = mapped_column(String(16), default="standard")

    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    #: ``HH:MM`` local time. No cron syntax ever reaches 简易版.
    schedule_time: Mapped[str] = mapped_column(String(5), default="08:00")

    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Bumped on every edit; the matching revision row holds the old brief.
    version: Mapped[int] = mapped_column(Integer, default=1)

    last_run_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True, index=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)

    revisions: Mapped[list["ResearchTopicRevision"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        order_by="ResearchTopicRevision.version.desc()",
    )

    # -- convenience, so templates never deal with None-vs-[] ---------------

    @property
    def focus_areas(self) -> list[str]:
        return [str(x) for x in (self.focus_areas_json or []) if str(x).strip()]

    @property
    def exclusions(self) -> list[str]:
        return [str(x) for x in (self.exclusions_json or []) if str(x).strip()]

    @property
    def keywords(self) -> list[str]:
        return [str(x) for x in (self.keywords_json or []) if str(x).strip()]

    @property
    def window_text(self) -> str:
        """``最近72小时`` / ``最近7天`` - the only form the user sees."""
        hours = max(1, self.window_hours or DEFAULT_WINDOW_HOURS)
        # Hours up to a week, days beyond it. 72 reads as 最近72小时, which is
        # how a daily-brief user thinks about it; 最近3天 is the same duration
        # and the wrong unit. A week or more is genuinely easier to read in
        # days, so 168 becomes 最近7天.
        if hours >= 168 and hours % 24 == 0:
            return f"最近{hours // 24}天"
        return f"最近{hours}小时"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ResearchTopic {self.id} {self.name}>"


class ResearchTopicRevision(Base):
    """A snapshot of a research brief before it was edited.

    Written by :func:`aios.repositories.research_topics.update_topic`, so a
    report produced three edits ago can still be read against the brief that
    actually produced it.
    """

    __tablename__ = "research_topic_revisions"
    __table_args__ = (
        UniqueConstraint("topic_id", "version", name="uq_research_revision_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("research_topics.id", ondelete="CASCADE"), index=True
    )

    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200), default="")
    brief: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(Text, default="")
    focus_areas_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    exclusions_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    keywords_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    regions: Mapped[str] = mapped_column(String(200), default="")
    window_hours: Mapped[int] = mapped_column(Integer, default=DEFAULT_WINDOW_HOURS)
    #: Why the brief changed - the user's own words where we have them.
    change_note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    topic: Mapped[ResearchTopic] = relationship(back_populates="revisions")
