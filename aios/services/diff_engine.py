"""Intelligence-level diffing between two reports.

This is not a text diff and not an HTML diff. Reports are compared through the
events behind them, so the answer to "what changed" is expressed as:

``NEW`` / ``UPDATED`` / ``UNCHANGED`` / ``RESOLVED`` / ``DATA_CHANGE`` / ``CORRECTION``

Numeric rules that matter:

* a percentage change is only computed when the baseline is a non-zero number,
* non-numeric values are shown as ``old -> new`` with no arithmetic,
* a value that cannot be parsed is never coerced into one.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import EventObservation, Report, ReportItem
from ..repositories import events as events_repo

# Change kinds, ordered by how loudly they should be displayed.
NEW = "NEW"
UPDATED = "UPDATED"
DATA_CHANGE = "DATA_CHANGE"
CORRECTION = "CORRECTION"
RESOLVED = "RESOLVED"
UNCHANGED = "UNCHANGED"

CHANGE_ORDER = {NEW: 0, CORRECTION: 1, DATA_CHANGE: 2, UPDATED: 3, RESOLVED: 4, UNCHANGED: 5}

CHANGE_LABELS = {
    NEW: "新增",
    UPDATED: "更新",
    DATA_CHANGE: "数据变化",
    CORRECTION: "数据修正",
    RESOLVED: "已结束",
    UNCHANGED: "无变化",
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(value: Any) -> Optional[float]:
    """Best-effort numeric reading of a metric value.

    Returns None for anything that is not unambiguously a number - strings such
    as "多家厂商" or "Q3" must never be turned into arithmetic.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    # Allow a bare number with a trailing unit such as "24.3%" or "87200000 devices".
    match = _NUMBER.fullmatch(text.rstrip("%").strip())
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def format_delta(absolute: float) -> str:
    """Signed, human-sized rendering of an absolute change."""
    sign = "+" if absolute > 0 else ("-" if absolute < 0 else "")
    magnitude = abs(absolute)
    if magnitude >= 100_000_000:
        return f"{sign}{magnitude / 100_000_000:.2f}亿".replace(".00亿", "亿")
    if magnitude >= 10_000:
        return f"{sign}{magnitude / 10_000:.2f}万".replace(".00万", "万")
    if magnitude == int(magnitude):
        return f"{sign}{int(magnitude):,}"
    return f"{sign}{magnitude:,.2f}"


@dataclass
class MetricChange:
    """One structured metric compared across two observations."""

    key: str
    old_display: str
    new_display: str
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    absolute_change: Optional[float] = None
    percentage_change: Optional[float] = None
    unit: str = ""
    comparable: bool = False

    @property
    def direction(self) -> str:
        if self.absolute_change is None or self.absolute_change == 0:
            return "flat"
        return "up" if self.absolute_change > 0 else "down"

    @property
    def absolute_display(self) -> str:
        return format_delta(self.absolute_change) if self.absolute_change is not None else ""

    @property
    def percentage_display(self) -> str:
        if self.percentage_change is None:
            return ""
        sign = "+" if self.percentage_change > 0 else ""
        return f"{sign}{self.percentage_change:.2f}%"


def compare_metric(key: str, old: Any, new: Any) -> MetricChange:
    """Compare one metric entry from two observations.

    ``old``/``new`` are the per-metric dicts stored in ``structured_data_json``.
    """
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}

    old_display = str(old.get("display", old.get("value", "")))
    new_display = str(new.get("display", new.get("value", "")))
    unit = str(new.get("unit") or old.get("unit") or "")

    old_number = parse_number(old.get("value"))
    if old_number is None:
        old_number = parse_number(old.get("display"))
    new_number = parse_number(new.get("value"))
    if new_number is None:
        new_number = parse_number(new.get("display"))

    change = MetricChange(
        key=key,
        old_display=old_display,
        new_display=new_display,
        old_value=old_number,
        new_value=new_number,
        unit=unit,
    )

    if old_number is None or new_number is None:
        # Not comparable: show old -> new and do no arithmetic at all.
        return change

    change.comparable = True
    change.absolute_change = new_number - old_number
    if old_number != 0:
        change.percentage_change = (new_number - old_number) / abs(old_number) * 100
    return change


def compare_structured_data(old: dict, new: dict) -> list[MetricChange]:
    """Compare every metric present in both observations."""
    old = old or {}
    new = new or {}
    changes: list[MetricChange] = []
    for key in new:
        if key not in old:
            continue
        change = compare_metric(key, old.get(key), new.get(key))
        if change.old_display != change.new_display or (
            change.absolute_change not in (None, 0)
        ):
            changes.append(change)
    return changes


@dataclass
class EventDiff:
    """How one event differs between report A and report B."""

    change_type: str
    event_id: Optional[int]
    title: str
    module_name: str
    module_key: str = ""
    topic_name: str = ""
    old_item: Optional[ReportItem] = None
    new_item: Optional[ReportItem] = None
    old_observation: Optional[EventObservation] = None
    new_observation: Optional[EventObservation] = None
    metric_changes: list[MetricChange] = field(default_factory=list)
    new_facts: str = ""
    sources: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.change_type, self.change_type)

    @property
    def badge_class(self) -> str:
        return f"badge-{self.change_type.lower()}"


@dataclass
class ReportDiff:
    """Full comparison result rendered by ``/compare``."""

    report_a: Optional[Report]
    report_b: Optional[Report]
    date_a: Optional[dt.date]
    date_b: Optional[dt.date]
    diffs: list[EventDiff] = field(default_factory=list)
    error: str = ""

    @property
    def summary(self) -> dict[str, int]:
        counts = {key: 0 for key in CHANGE_ORDER}
        for diff in self.diffs:
            counts[diff.change_type] = counts.get(diff.change_type, 0) + 1
        return counts

    def by_module(self) -> list[tuple[str, list[EventDiff]]]:
        """Group diffs by module, preserving report section order."""
        grouped: dict[str, list[EventDiff]] = {}
        for diff in self.diffs:
            grouped.setdefault(diff.module_name or "其他", []).append(diff)
        for items in grouped.values():
            items.sort(key=lambda d: (CHANGE_ORDER.get(d.change_type, 9), d.title))
        return list(grouped.items())


def _index_items(report: Optional[Report]) -> dict[int, ReportItem]:
    """Map event_id -> report item for the items that carry an event."""
    if report is None:
        return {}
    indexed: dict[int, ReportItem] = {}
    for section in report.sections:
        for item in section.items:
            if item.event_id is not None:
                indexed[item.event_id] = item
    return indexed


def _section_name(item: Optional[ReportItem]) -> str:
    return item.section.module_name if item is not None and item.section else ""


def _section_key(item: Optional[ReportItem]) -> str:
    return item.section.module_key if item is not None and item.section else ""


def _sources_of(observation: Optional[EventObservation]) -> list:
    if observation is None:
        return []
    return [link.article for link in observation.sources if link.article is not None]


def _classify(
    old_item: Optional[ReportItem],
    new_item: Optional[ReportItem],
    old_obs: Optional[EventObservation],
    new_obs: Optional[EventObservation],
    metric_changes: list[MetricChange],
) -> str:
    if old_item is None:
        return NEW
    if new_item is None:
        return RESOLVED
    if new_obs is not None and new_obs.is_correction:
        return CORRECTION
    if metric_changes:
        return DATA_CHANGE
    if old_obs is not None and new_obs is not None and old_obs.id != new_obs.id:
        if (new_obs.fact_summary or "").strip() != (old_obs.fact_summary or "").strip():
            return UPDATED
    return UNCHANGED


def compare_reports(session: Session, report_a: Optional[Report], report_b: Optional[Report]) -> ReportDiff:
    """Diff report B (newer) against report A (older) at the event level."""
    if report_a is None or report_b is None:
        return ReportDiff(
            report_a=report_a,
            report_b=report_b,
            date_a=report_a.report_date if report_a else None,
            date_b=report_b.report_date if report_b else None,
            error="需要两份报告才能对比。",
        )

    if report_a.report_date > report_b.report_date:
        report_a, report_b = report_b, report_a

    old_items = _index_items(report_a)
    new_items = _index_items(report_b)

    diffs: list[EventDiff] = []
    for event_id in sorted(set(old_items) | set(new_items)):
        old_item = old_items.get(event_id)
        new_item = new_items.get(event_id)

        old_obs = old_item.observation if old_item else None
        new_obs = new_item.observation if new_item else None

        # For an event that carried over, compare against its own prior
        # observation even if that observation predates report A.
        if new_obs is not None and old_obs is None and old_item is not None:
            old_obs = events_repo.latest_observation_before(
                session, event_id, report_b.report_date
            )

        metric_changes: list[MetricChange] = []
        if old_obs is not None and new_obs is not None and old_obs.id != new_obs.id:
            metric_changes = compare_structured_data(old_obs.metrics, new_obs.metrics)

        change_type = _classify(old_item, new_item, old_obs, new_obs, metric_changes)

        reference = new_item or old_item
        event = reference.event if reference else None
        diffs.append(
            EventDiff(
                change_type=change_type,
                event_id=event_id,
                title=(reference.title if reference else "") or (event.title if event else ""),
                module_name=_section_name(new_item) or _section_name(old_item),
                module_key=_section_key(new_item) or _section_key(old_item),
                topic_name=event.topic.name if event and event.topic else "",
                old_item=old_item,
                new_item=new_item,
                old_observation=old_obs,
                new_observation=new_obs,
                metric_changes=metric_changes,
                new_facts=(new_obs.fact_summary if new_obs else "") or "",
                sources=_sources_of(new_obs) or _sources_of(old_obs),
            )
        )

    diffs.sort(key=lambda d: (CHANGE_ORDER.get(d.change_type, 9), d.module_name, d.title))
    return ReportDiff(
        report_a=report_a,
        report_b=report_b,
        date_a=report_a.report_date,
        date_b=report_b.report_date,
        diffs=diffs,
    )


def compare_dates(session: Session, date_a: dt.date, date_b: dt.date) -> ReportDiff:
    """Convenience wrapper used by the ``/compare`` router."""
    from ..repositories import reports as reports_repo

    report_a = reports_repo.get_by_date(session, date_a)
    report_b = reports_repo.get_by_date(session, date_b)
    if report_a is None or report_b is None:
        missing = []
        if report_a is None:
            missing.append(date_a.isoformat())
        if report_b is None:
            missing.append(date_b.isoformat())
        return ReportDiff(
            report_a=report_a,
            report_b=report_b,
            date_a=date_a,
            date_b=date_b,
            error="没有找到以下日期的报告：" + "、".join(missing),
        )
    return compare_reports(session, report_a, report_b)


def diff_against_previous(session: Session, report: Report) -> ReportDiff:
    """Today vs. the report immediately before it - the dashboard's daily diff."""
    from ..repositories import reports as reports_repo

    previous = reports_repo.previous_report(session, report)
    if previous is None:
        return ReportDiff(
            report_a=None,
            report_b=report,
            date_a=None,
            date_b=report.report_date,
            error="这是第一份报告，暂无可对比的历史。",
        )
    return compare_reports(session, previous, report)
