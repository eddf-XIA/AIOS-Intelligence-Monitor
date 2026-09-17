"""Persisted data-source configuration and per-run source health history.

Only two things earn a table here:

* :class:`FeedSource` - user-entered RSS/Atom feeds. These are configuration,
  exactly like a search query, and must survive a restart.
* :class:`RunSourceStat` - the per-run counters behind the 数据源状态 panel.
  Kept because "was GDELT throttled the day that report was thin?" is a real
  question a week later, and because the live run view reads them back.

Transient state - open circuits, the short-term query cache, the current
in-flight health of a source - deliberately stays in memory. Persisting it
would mean showing the user a health verdict from a network that no longer
exists.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UTCDateTime
from ..timeutil import utcnow


class FeedSource(Base, TimestampMixin):
    """One RSS/Atom feed the RSS collector polls.

    Feeds are the answer to "how do we stay useful when search engines are not
    reachable" - an official newsroom feed needs no API key, no search index and
    no circumvention of anything.
    """

    __tablename__ = "feed_sources"
    __table_args__ = (UniqueConstraint("url", name="uq_feed_source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(200), default="")
    domain: Mapped[str] = mapped_column(String(200), default="", index=True)
    #: Optional scoping: a feed may belong to one module, or to everything.
    module_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("monitor_modules.id", ondelete="CASCADE"), nullable=True, index=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    last_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="")
    last_item_count: Mapped[int] = mapped_column(Integer, default=0)

    module: Mapped[Optional["MonitorModule"]] = relationship()  # type: ignore[name-defined]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FeedSource {self.url[:48]!r}>"


class RunSourceStat(Base):
    """Per-run, per-collector counters.

    Every column is a tally of something that happened. Nothing here is an
    average or an estimate - the UI derives rates from these at display time so
    a percentage can never outlive the numbers that produced it.
    """

    __tablename__ = "run_source_stats"
    __table_args__ = (
        UniqueConstraint("run_id", "collector", name="uq_run_source_collector"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="CASCADE"), index=True
    )

    collector: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(32), default="unknown", index=True)

    requests_attempted: Mapped[int] = mapped_column(Integer, default=0)
    requests_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    zero_result_responses: Mapped[int] = mapped_column(Integer, default=0)
    candidates_returned: Mapped[int] = mapped_column(Integer, default=0)

    timeouts: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    network_errors: Mapped[int] = mapped_column(Integer, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, default=0)
    http_errors: Mapped[int] = mapped_column(Integer, default=0)
    circuit_activations: Mapped[int] = mapped_column(Integer, default=0)
    circuit_open_skips: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)

    avg_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    extra_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    run: Mapped["MonitoringRun"] = relationship()  # type: ignore[name-defined]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RunSourceStat run={self.run_id} {self.collector}={self.state}>"
