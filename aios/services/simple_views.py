"""Turning the intelligence core into 简易版's plain-language views.

Simple mode does not get its own data - it gets its own *vocabulary*. These
builders read exactly the same rows the Professional pages read (reports,
sections, items, events, observations, evidence links) and the same diff engine,
then express the result as things a normal user recognises:

======================  ====================================
the data says           Simple mode says
======================  ====================================
``EventDiff(NEW)``      新增
``EventDiff(UPDATED)``  更新, with a dated timeline
``DATA_CHANGE``         a number that moved, old → new
``ObservationSource``   a publisher name you can click
``MonitoringRun``       (never mentioned)
======================  ====================================

Keeping the translation here rather than in the templates means the Changes
page cannot drift away from what ``/compare`` computes: there is one diff
engine, and both pages render its output.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import Report
from ..repositories import events as events_repo
from ..repositories import reports as reports_repo
from ..repositories import research_topics as topics_repo
from . import diff_engine

#: Diff kinds grouped the way a reader thinks about them, with the label each
#: group carries. ``RESOLVED`` is folded into 持续关注 rather than shown as its
#: own alarming category: an event dropping out of one day's report usually
#: means it had no news, not that it ended.
CHANGE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("新增", (diff_engine.NEW,)),
    ("更新", (diff_engine.UPDATED, diff_engine.DATA_CHANGE, diff_engine.CORRECTION)),
    ("持续关注", (diff_engine.UNCHANGED, diff_engine.RESOLVED)),
)


def _cn_date(value) -> str:
    return f"{value.month}/{value.day}" if value else ""


# --- 查看变化 ---------------------------------------------------------------

def changes_context(session: Session, report: Report) -> dict:
    """相比上一期 for one report.

    The baseline is the previous report *for the same research topic* where
    there is one: comparing a 人形机器人 brief against yesterday's 智能终端
    brief would produce a diff full of noise and no information.
    """
    previous = _baseline(session, report)
    topic = (
        topics_repo.get_topic(session, report.research_topic_id)
        if report.research_topic_id
        else None
    )

    if previous is None:
        return {
            "report": report,
            "previous": None,
            "topic": topic,
            "counts": {"new": 0, "updated": 0, "unchanged": 0},
            "groups": [],
        }

    diff = diff_engine.compare_reports(session, previous, report)
    by_type: dict[str, list[dict]] = {}
    for entry in diff.diffs:
        by_type.setdefault(entry.change_type, []).append(
            _entry_payload(session, entry, report)
        )

    groups = [
        {
            "label": label,
            "entries": [
                payload for kind in kinds for payload in by_type.get(kind, [])
            ],
        }
        for label, kinds in CHANGE_GROUPS
    ]

    summary = diff.summary
    return {
        "report": report,
        "previous": previous,
        "topic": topic,
        "counts": {
            "new": summary.get(diff_engine.NEW, 0),
            "updated": (
                summary.get(diff_engine.UPDATED, 0)
                + summary.get(diff_engine.DATA_CHANGE, 0)
                + summary.get(diff_engine.CORRECTION, 0)
            ),
            "unchanged": (
                summary.get(diff_engine.UNCHANGED, 0)
                + summary.get(diff_engine.RESOLVED, 0)
            ),
        },
        "groups": groups,
    }


def _baseline(session: Session, report: Report) -> Optional[Report]:
    """The report this one should be compared against."""
    if report.research_topic_id:
        for candidate in topics_repo.reports_for_topic(
            session, report.research_topic_id, limit=40
        ):
            if candidate.report_date < report.report_date or (
                candidate.report_date == report.report_date and candidate.id < report.id
            ):
                return reports_repo.get_report(session, candidate.id)
        return None
    return reports_repo.previous_report(session, report)


def _entry_payload(session: Session, entry: diff_engine.EventDiff, report: Report) -> dict:
    """One change, described without exposing an observation or a change enum."""
    timeline: list[dict] = []
    if entry.event_id is not None and entry.change_type != diff_engine.NEW:
        timeline = _timeline(session, entry.event_id, report)

    metrics = [
        {
            "label": change.key,
            "old": change.old_display or "—",
            "new": change.new_display or "—",
            "delta": change.absolute_display if change.comparable else "",
        }
        for change in entry.metric_changes
    ]

    return {
        "event_id": entry.event_id,
        "title": entry.title,
        "body": entry.new_facts,
        "timeline": timeline,
        "metrics": metrics,
    }


def _timeline(session: Session, event_id: int, report: Report, limit: int = 4) -> list[dict]:
    """The last few dated observations of one event.

    This is what makes 更新 legible: "9/13 计划启动 / 9/20 合作伙伴扩展至 27 家"
    tells the reader what actually moved, which a change-type label never does.
    """
    event = events_repo.get_event(session, event_id)
    if event is None:
        return []
    points = [
        {
            "date": _cn_date(observation.observation_date),
            "text": observation.fact_summary or observation.title,
        }
        for observation in event.observations
        if observation.observation_date <= report.report_date
    ]
    return points[-limit:]


# --- 来源证据 ---------------------------------------------------------------

def sources_context(session: Session, report: Report) -> dict:
    """Evidence for one report, grouped by intelligence item and by publisher."""
    topic = (
        topics_repo.get_topic(session, report.research_topic_id)
        if report.research_topic_id
        else None
    )

    groups: list[dict] = []
    publisher_counts: dict[str, int] = {}
    seen_articles: set[int] = set()

    for section in report.sections:
        for item in section.items:
            sources: list[dict] = []
            observation = item.observation
            if observation is not None:
                for link in observation.sources:
                    article = link.article
                    if article is None or not article.url:
                        continue
                    publisher = article.source or article.domain or "来源"
                    if article.id not in seen_articles:
                        seen_articles.add(article.id)
                        publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1
                    sources.append(
                        {
                            "publisher": publisher,
                            "url": article.url,
                            "date": article.published_raw
                            or _cn_date(
                                article.published_at.date() if article.published_at else None
                            ),
                            "title": article.title or "",
                        }
                    )
            groups.append(
                {
                    "event_id": item.event_id,
                    "title": item.title,
                    "sources": sources,
                }
            )

    publishers = [
        {"name": name, "count": count}
        for name, count in sorted(
            publisher_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )
    ]

    return {
        "report": report,
        "topic": topic,
        "groups": groups,
        "publishers": publishers,
        "source_count": len(seen_articles),
    }


# --- 历史记录 ---------------------------------------------------------------

def history_context(session: Session, topic_id: Optional[int] = None) -> dict:
    """Reports grouped by research topic, newest first within each group."""
    topics = topics_repo.list_topics(session, limit=30)
    selected = [t for t in topics if t.id == topic_id] if topic_id else topics

    groups: list[dict] = []
    for topic in selected:
        reports = topics_repo.reports_for_topic(session, topic.id, limit=40)
        if not reports:
            continue
        groups.append(
            {
                "topic": topic,
                "topic_name": topic.name,
                "event_count": topics_repo.count_events(session, topic.id),
                "reports": [{"report": report} for report in reports],
            }
        )

    # Classic reports have no research topic, but they are still the user's
    # history and must not vanish from the Simple history page.
    if not topic_id:
        orphans = [
            report
            for report in reports_repo.list_reports(session, limit=40)
            if report.research_topic_id is None
        ]
        if orphans:
            groups.append(
                {
                    "topic": None,
                    "topic_name": "本地专业版监测",
                    "event_count": 0,
                    "reports": [{"report": report} for report in orphans],
                }
            )

    return {
        "topics": topics,
        "selected_topic_id": topic_id,
        "groups": groups,
    }
