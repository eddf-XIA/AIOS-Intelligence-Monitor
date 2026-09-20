"""The validated contract a Research Agent must satisfy.

This schema is the reason Agent research does not destroy AIOS's historical
intelligence tracking. An agent that returned one giant Markdown string would
give the user a nice report today and nothing to compare against tomorrow, so
the agent is required to return **three separate things**:

``report``
    Optimised for a human reader: title, focus, sections, trend analysis. This
    is what the report page renders.

``events``
    Optimised for *identity*. Each event carries organisation, product/project,
    event type and date so the matcher can recognise the same real-world story
    next week even when the agent words it completely differently.

``sources``
    Optimised for evidence. Every claim in the report and every event points at
    source ids, and a source id that does not resolve is dropped rather than
    displayed.

``coverage`` sits above all three and answers a question the product refuses to
conflate: *did we manage to look?* ``complete`` / ``partial`` / ``failed`` are
three different facts, and "no important developments found" is only ever
allowed to mean the first one.

Nothing in here is ever shown to the user as JSON. It is an implementation
detail between the agent and :mod:`aios.services.research_pipeline`.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# --- coverage ---------------------------------------------------------------

#: The research ran and the agent is satisfied it saw what it needed.
COVERAGE_COMPLETE = "complete"
#: The research ran but something was unavailable - the report may have holes.
COVERAGE_PARTIAL = "partial"
#: The research did not happen. Emphatically **not** the same as "no news".
COVERAGE_FAILED = "failed"

COVERAGE_STATUSES = (COVERAGE_COMPLETE, COVERAGE_PARTIAL, COVERAGE_FAILED)

#: What each coverage state is allowed to say on screen. The wording is the
#: whole point: a user must never read "今日没有重要动态" when the truth is
#: "我们没能查".
COVERAGE_LABELS = {
    COVERAGE_COMPLETE: "研究完成",
    COVERAGE_PARTIAL: "部分完成",
    COVERAGE_FAILED: "研究未完成",
}

COVERAGE_PARTIAL_NOTICE = "本次研究部分来源不可用，结果可能不完整。"
#: Only legitimate when coverage is ``complete``.
COVERAGE_EMPTY_NOTICE = "本次研究已完成，但未发现达到入报标准的重要动态。"

#: Hard caps. An agent asked for "important developments" will happily return
#: forty; the report is a daily brief, not an archive dump.
MAX_EVENTS = 40
MAX_SOURCES = 200
MAX_SECTIONS = 16
MAX_ITEMS_PER_SECTION = 8
MAX_TRENDS = 6
MAX_MARKET_ROWS = 10
MAX_WATCH_NEXT = 8

_WS = re.compile(r"\s+")


def _clean(value: Any, limit: int = 4000) -> str:
    """Collapse whitespace and truncate. Never returns None."""
    return _WS.sub(" ", str(value if value is not None else "").strip())[:limit]


def _string_list(value: Any, limit: int, item_limit: int = 120) -> list[str]:
    """Coerce a scalar-or-list into a de-duplicated list of clean strings."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if isinstance(raw, dict):
            raw = raw.get("name") or raw.get("text") or raw.get("value") or ""
        text = _clean(raw, item_limit)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _int_list(value: Any, limit: int = 40) -> list[int]:
    """Evidence ids, tolerating the strings models sometimes emit."""
    if value is None:
        return []
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for raw in value:
        if isinstance(raw, bool):
            continue
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if number not in out:
            out.append(number)
        if len(out) >= limit:
            break
    return out


def parse_date(value: Any) -> Optional[dt.date]:
    """Read a date the agent reported, or None. Never guesses a value.

    An event with an unparseable date keeps ``None`` rather than being dated
    "today": the matcher treats a missing date as "no signal", which is honest,
    whereas a wrong date is an active signal pointing the wrong way.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean(value, 40)
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%d %b %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    # ISO 8601 with a time component or an offset.
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


# --- pieces -----------------------------------------------------------------

class ResearchCoverage(BaseModel):
    """Whether the research actually happened, and what was missing if not."""

    model_config = ConfigDict(extra="ignore")

    status: str = COVERAGE_COMPLETE
    window_from: str = ""
    window_to: str = ""
    limitations: list[str] = Field(default_factory=list)
    sources_examined: int = 0

    @field_validator("status", mode="before")
    @classmethod
    def clean_status(cls, value: Any) -> str:
        text = _clean(value, 24).lower().replace("-", "_")
        if text in COVERAGE_STATUSES:
            return text
        # Common synonyms. Anything unrecognised is treated as ``partial``:
        # claiming ``complete`` on the strength of a word we do not understand
        # would let a degraded run masquerade as an authoritative one.
        if text in {"ok", "success", "succeeded", "full", "done"}:
            return COVERAGE_COMPLETE
        if text in {"error", "failure", "unavailable", "aborted"}:
            return COVERAGE_FAILED
        return COVERAGE_PARTIAL if text else COVERAGE_COMPLETE

    @field_validator("limitations", mode="before")
    @classmethod
    def clean_limitations(cls, value: Any) -> list[str]:
        return _string_list(value, limit=8, item_limit=300)

    @field_validator("sources_examined", mode="before")
    @classmethod
    def clean_examined(cls, value: Any) -> int:
        try:
            return max(0, int(str(value).strip()))
        except (TypeError, ValueError):
            return 0

    @property
    def is_failed(self) -> bool:
        return self.status == COVERAGE_FAILED

    @property
    def is_partial(self) -> bool:
        return self.status == COVERAGE_PARTIAL

    @property
    def label(self) -> str:
        return COVERAGE_LABELS.get(self.status, self.status)


class ResearchSource(BaseModel):
    """One document the agent read. The evidence layer of the whole result."""

    model_config = ConfigDict(extra="ignore")

    source_id: int
    title: str = ""
    publisher: str = ""
    url: str
    published_at: str = ""

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        url = _clean(value, 1000)
        if not url.startswith(("http://", "https://")):
            raise ValueError("来源必须是 http(s) URL。")
        return url

    @field_validator("title", "publisher", "published_at", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean(value, 300)

    @property
    def published_date(self) -> Optional[dt.date]:
        return parse_date(self.published_at)


class ResearchEvent(BaseModel):
    """One real-world development, described so it can be *recognised again*.

    The identity fields are not decoration. They are what lets day 3's
    "Helix 2 获得新的工厂部署" attach to day 1's "Figure 发布 Helix 2" instead
    of starting a third unrelated timeline.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=2, max_length=300)
    summary: str = ""
    entities: list[str] = Field(default_factory=list)
    organization: str = ""
    product_or_project: str = ""
    event_type: str = ""
    event_date: Optional[dt.date] = None
    significance: str = ""
    importance: int = 3
    section: str = ""
    source_ids: list[int] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value: Any) -> str:
        return _clean(value, 300)

    @field_validator("summary", "significance", mode="before")
    @classmethod
    def clean_body(cls, value: Any) -> str:
        return _clean(value, 2000)

    @field_validator("organization", "product_or_project", "event_type", "section", mode="before")
    @classmethod
    def clean_identity(cls, value: Any) -> str:
        return _clean(value, 200)

    @field_validator("entities", mode="before")
    @classmethod
    def clean_entities(cls, value: Any) -> list[str]:
        return _string_list(value, limit=12)

    @field_validator("source_ids", mode="before")
    @classmethod
    def clean_source_ids(cls, value: Any) -> list[int]:
        return _int_list(value)

    @field_validator("event_date", mode="before")
    @classmethod
    def clean_date(cls, value: Any) -> Optional[dt.date]:
        return parse_date(value)

    @field_validator("importance", mode="before")
    @classmethod
    def clean_importance(cls, value: Any) -> int:
        try:
            return max(1, min(5, int(str(value).strip())))
        except (TypeError, ValueError):
            return 3


class ResearchSummaryItem(BaseModel):
    """One paragraph in a report section, tied to the evidence behind it."""

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(min_length=2, max_length=300)
    body: str = ""
    #: Why it matters. Kept separate from ``body`` so judgement is never
    #: presented as fact - the same separation the Classic analyser enforces.
    significance: str = ""
    evidence_ids: list[int] = Field(default_factory=list)

    @field_validator("headline", mode="before")
    @classmethod
    def clean_headline(cls, value: Any) -> str:
        return _clean(value, 300)

    @field_validator("body", "significance", mode="before")
    @classmethod
    def clean_body(cls, value: Any) -> str:
        return _clean(value, 3000)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def clean_ids(cls, value: Any) -> list[int]:
        return _int_list(value)


class ResearchSection(BaseModel):
    """A domain slice of the report (移动智能终端侧, 服务器侧, ...)."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    summary: str = ""
    summary_items: list[ResearchSummaryItem] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: Any) -> str:
        return _clean(value, 200)

    @field_validator("summary", mode="before")
    @classmethod
    def clean_summary(cls, value: Any) -> str:
        return _clean(value, 1200)

    @field_validator("summary_items", mode="before")
    @classmethod
    def cap_items(cls, value: Any) -> Any:
        if isinstance(value, list):
            return value[:MAX_ITEMS_PER_SECTION]
        return value


class ResearchMarketRow(BaseModel):
    """One row of 市场份额速览 - only ever rendered when evidence supports it."""

    model_config = ConfigDict(extra="ignore")

    label: str = Field(min_length=1, max_length=200)
    value: str = ""
    note: str = ""
    evidence_ids: list[int] = Field(default_factory=list)

    @field_validator("label", "value", "note", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean(value, 200)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def clean_ids(cls, value: Any) -> list[int]:
        return _int_list(value)


class ResearchTrend(BaseModel):
    """A synthesis judgement. Always labelled as judgement, never as fact."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=2, max_length=300)
    analysis: str = ""
    confidence: str = "medium"
    evidence_ids: list[int] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value: Any) -> str:
        return _clean(value, 300)

    @field_validator("analysis", mode="before")
    @classmethod
    def clean_analysis(cls, value: Any) -> str:
        return _clean(value, 3000)

    @field_validator("confidence", mode="before")
    @classmethod
    def clean_confidence(cls, value: Any) -> str:
        text = _clean(value, 16).lower()
        return text if text in {"high", "medium", "low"} else "medium"

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def clean_ids(cls, value: Any) -> list[int]:
        return _int_list(value)


class ResearchReportBody(BaseModel):
    """The human-facing half of the result."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    focus_title: str = ""
    focus_summary: str = ""
    sections: list[ResearchSection] = Field(default_factory=list)
    market_snapshot: list[ResearchMarketRow] = Field(default_factory=list)
    trend_analysis: list[ResearchTrend] = Field(default_factory=list)

    @field_validator("title", "focus_title", mode="before")
    @classmethod
    def clean_title(cls, value: Any) -> str:
        return _clean(value, 300)

    @field_validator("focus_summary", mode="before")
    @classmethod
    def clean_focus(cls, value: Any) -> str:
        return _clean(value, 3000)

    @field_validator("sections", mode="before")
    @classmethod
    def cap_sections(cls, value: Any) -> Any:
        return value[:MAX_SECTIONS] if isinstance(value, list) else value

    @field_validator("market_snapshot", mode="before")
    @classmethod
    def cap_market(cls, value: Any) -> Any:
        return value[:MAX_MARKET_ROWS] if isinstance(value, list) else value

    @field_validator("trend_analysis", mode="before")
    @classmethod
    def cap_trends(cls, value: Any) -> Any:
        return value[:MAX_TRENDS] if isinstance(value, list) else value


class ResearchResult(BaseModel):
    """One completed research pass, ready to become events and a report."""

    model_config = ConfigDict(extra="ignore")

    coverage: ResearchCoverage = Field(default_factory=ResearchCoverage)
    report: ResearchReportBody = Field(default_factory=ResearchReportBody)
    events: list[ResearchEvent] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    watch_next: list[str] = Field(default_factory=list)

    @field_validator("events", mode="before")
    @classmethod
    def cap_events(cls, value: Any) -> Any:
        return value[:MAX_EVENTS] if isinstance(value, list) else value

    @field_validator("sources", mode="before")
    @classmethod
    def cap_sources(cls, value: Any) -> Any:
        return value[:MAX_SOURCES] if isinstance(value, list) else value

    @field_validator("watch_next", mode="before")
    @classmethod
    def clean_watch(cls, value: Any) -> list[str]:
        return _string_list(value, limit=MAX_WATCH_NEXT, item_limit=300)

    # -- derived ---------------------------------------------------------

    @property
    def source_ids(self) -> set[int]:
        return {source.source_id for source in self.sources}

    @property
    def is_empty(self) -> bool:
        """No events. Only a *valid* outcome when coverage is complete."""
        return not self.events

    def source_by_id(self, source_id: int) -> Optional[ResearchSource]:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        return None


# --- repair -----------------------------------------------------------------

class ResearchResultError(ValueError):
    """The agent's payload could not be repaired into a usable result."""


def _coerce_source(raw: Any, fallback_id: int) -> Optional[dict]:
    """One source entry, tolerating the shapes agents actually emit."""
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        return None

    url = _clean(raw.get("url") or raw.get("link") or raw.get("source_url") or "", 1000)
    # Dropped here rather than raising: one hallucinated citation must not
    # discard an otherwise good research pass. The item that cited it loses its
    # evidence and is pruned in turn, which is the correct outcome.
    if not url.startswith(("http://", "https://")):
        return None

    identifier = raw.get("source_id", raw.get("id", raw.get("index")))
    try:
        source_id = int(str(identifier).strip())
    except (TypeError, ValueError):
        source_id = fallback_id

    return {
        "source_id": source_id,
        "title": raw.get("title") or raw.get("headline") or "",
        "publisher": raw.get("publisher") or raw.get("source") or raw.get("site") or "",
        "url": url,
        "published_at": raw.get("published_at")
        or raw.get("published")
        or raw.get("date")
        or "",
    }


def _coerce_event(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("title") or raw.get("headline") or raw.get("name") or "", 300)
    if len(title) < 2:
        return None
    return {
        "title": title,
        "summary": raw.get("summary") or raw.get("body") or raw.get("description") or "",
        "entities": raw.get("entities") or raw.get("actors") or raw.get("companies"),
        "organization": raw.get("organization")
        or raw.get("org")
        or raw.get("company")
        or raw.get("vendor")
        or "",
        "product_or_project": raw.get("product_or_project")
        or raw.get("product")
        or raw.get("project")
        or raw.get("product_name")
        or "",
        "event_type": raw.get("event_type") or raw.get("type") or raw.get("category") or "",
        "event_date": raw.get("event_date") or raw.get("date") or raw.get("occurred_at"),
        "significance": raw.get("significance")
        or raw.get("why_it_matters")
        or raw.get("assessment")
        or "",
        "importance": raw.get("importance", raw.get("priority", 3)),
        "section": raw.get("section") or raw.get("domain") or raw.get("area") or "",
        "source_ids": raw.get("source_ids")
        if raw.get("source_ids") is not None
        else raw.get("evidence_ids"),
    }


def _coerce_item(raw: Any) -> Optional[dict]:
    if isinstance(raw, str):
        raw = {"headline": raw}
    if not isinstance(raw, dict):
        return None
    headline = _clean(raw.get("headline") or raw.get("title") or "", 300)
    if len(headline) < 2:
        return None
    return {
        "headline": headline,
        "body": raw.get("body") or raw.get("summary") or raw.get("text") or "",
        "significance": raw.get("significance") or raw.get("why_it_matters") or "",
        "evidence_ids": raw.get("evidence_ids")
        if raw.get("evidence_ids") is not None
        else raw.get("source_ids"),
    }


def _coerce_section(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name") or raw.get("section") or raw.get("title") or "", 200)
    if not name:
        return None
    items_raw = raw.get("summary_items")
    if items_raw is None:
        items_raw = raw.get("items") or raw.get("highlights") or []
    items = []
    if isinstance(items_raw, list):
        for entry in items_raw:
            coerced = _coerce_item(entry)
            if coerced is not None:
                items.append(coerced)
    return {
        "name": name,
        "summary": raw.get("summary") or raw.get("overview") or "",
        "summary_items": items,
    }


def coerce_research_payload(payload: Any) -> dict:
    """Normalise an agent's JSON into the shape :class:`ResearchResult` wants.

    Agents reliably produce *valid JSON in a slightly different shape* - the
    report body at the top level instead of under ``report``, ``items`` instead
    of ``summary_items``, a bare URL string instead of a source object. Repair
    handles that. It does **not** invent content: a payload with no report and
    no events stays empty and the caller decides what that means.
    """
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)

    # Some agents wrap everything once more.
    for wrapper in ("result", "research_result", "data"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and ("report" in inner or "events" in inner):
            data = dict(inner)
            break

    report_raw = data.get("report")
    if not isinstance(report_raw, dict):
        # The report body was emitted at the top level.
        report_raw = {
            key: data[key]
            for key in (
                "title", "focus_title", "focus_summary", "sections",
                "market_snapshot", "trend_analysis",
            )
            if key in data
        }

    sections = []
    raw_sections = report_raw.get("sections")
    if isinstance(raw_sections, list):
        for entry in raw_sections:
            coerced = _coerce_section(entry)
            if coerced is not None:
                sections.append(coerced)

    trends = []
    raw_trends = report_raw.get("trend_analysis") or report_raw.get("trends") or []
    if isinstance(raw_trends, list):
        for entry in raw_trends:
            if isinstance(entry, str):
                entry = {"title": entry}
            if not isinstance(entry, dict):
                continue
            title = _clean(entry.get("title") or entry.get("name") or "", 300)
            if len(title) < 2:
                continue
            trends.append(
                {
                    "title": title,
                    "analysis": entry.get("analysis") or entry.get("body") or "",
                    "confidence": entry.get("confidence", "medium"),
                    "evidence_ids": entry.get("evidence_ids")
                    if entry.get("evidence_ids") is not None
                    else entry.get("source_ids"),
                }
            )

    market = []
    raw_market = report_raw.get("market_snapshot") or report_raw.get("metrics") or []
    if isinstance(raw_market, list):
        for entry in raw_market:
            if not isinstance(entry, dict):
                continue
            label = _clean(
                entry.get("label") or entry.get("name") or entry.get("metric") or "", 200
            )
            if not label:
                continue
            market.append(
                {
                    "label": label,
                    "value": entry.get("value") or entry.get("display") or "",
                    "note": entry.get("note") or entry.get("unit") or "",
                    "evidence_ids": entry.get("evidence_ids")
                    if entry.get("evidence_ids") is not None
                    else entry.get("source_ids"),
                }
            )

    sources = []
    raw_sources = data.get("sources") or data.get("citations") or data.get("references") or []
    if isinstance(raw_sources, list):
        for index, entry in enumerate(raw_sources):
            coerced = _coerce_source(entry, fallback_id=index)
            if coerced is not None:
                sources.append(coerced)

    events = []
    raw_events = data.get("events") or data.get("developments") or []
    if isinstance(raw_events, list):
        for entry in raw_events:
            coerced = _coerce_event(entry)
            if coerced is not None:
                events.append(coerced)

    coverage_raw = data.get("coverage")
    if not isinstance(coverage_raw, dict):
        coverage_raw = {}
    # Accept the flat aliases too.
    coverage = {
        "status": coverage_raw.get("status", data.get("coverage_status", COVERAGE_COMPLETE)),
        "window_from": coverage_raw.get("window_from", coverage_raw.get("from", "")),
        "window_to": coverage_raw.get("window_to", coverage_raw.get("to", "")),
        "limitations": coverage_raw.get("limitations", coverage_raw.get("caveats")),
        "sources_examined": coverage_raw.get(
            "sources_examined", coverage_raw.get("examined", len(sources))
        ),
    }

    return {
        "coverage": coverage,
        "report": {
            "title": report_raw.get("title", ""),
            "focus_title": report_raw.get("focus_title", report_raw.get("headline", "")),
            "focus_summary": report_raw.get("focus_summary", report_raw.get("lede", "")),
            "sections": sections,
            "market_snapshot": market,
            "trend_analysis": trends,
        },
        "events": events,
        "sources": sources,
        "watch_next": data.get("watch_next") or data.get("watchlist") or [],
    }


def validate_research_payload(payload: Any) -> ResearchResult:
    """Repair, validate, then prune anything that cites evidence we do not have.

    The pruning matters as much as the validation. An event whose ``source_ids``
    point at sources the agent never listed is not evidence-backed, and AIOS's
    whole claim is that every report item is traceable - so it is dropped, not
    displayed with a broken citation.
    """
    coerced = coerce_research_payload(payload)
    try:
        result = ResearchResult.model_validate(coerced)
    except ValidationError as exc:
        raise ResearchResultError(_first_error(exc)) from exc

    # De-duplicate sources by URL, keeping the first id seen for each.
    unique_sources: list[ResearchSource] = []
    by_url: dict[str, int] = {}
    remap: dict[int, int] = {}
    for source in result.sources:
        existing = by_url.get(source.url)
        if existing is not None:
            remap[source.source_id] = existing
            continue
        by_url[source.url] = source.source_id
        remap[source.source_id] = source.source_id
        unique_sources.append(source)

    valid_ids = {source.source_id for source in unique_sources}

    def resolve(ids: list[int]) -> list[int]:
        out: list[int] = []
        for identifier in ids:
            mapped = remap.get(identifier, identifier)
            if mapped in valid_ids and mapped not in out:
                out.append(mapped)
        return out

    events = []
    for event in result.events:
        resolved = resolve(event.source_ids)
        if not resolved:
            # No traceable evidence - inadmissible, exactly as in the Classic
            # analyser's parse_topic_items().
            continue
        events.append(event.model_copy(update={"source_ids": resolved}))

    sections = []
    for section in result.report.sections:
        items = [
            item.model_copy(update={"evidence_ids": resolve(item.evidence_ids)})
            for item in section.summary_items
        ]
        sections.append(section.model_copy(update={"summary_items": items}))

    market = [
        row.model_copy(update={"evidence_ids": resolve(row.evidence_ids)})
        for row in result.report.market_snapshot
    ]
    trends = [
        trend.model_copy(update={"evidence_ids": resolve(trend.evidence_ids)})
        for trend in result.report.trend_analysis
    ]

    coverage = result.coverage
    if not coverage.sources_examined:
        coverage = coverage.model_copy(update={"sources_examined": len(unique_sources)})

    # A payload that produced *nothing in any dimension* is not a finding, it
    # is a non-answer - and it must not be allowed to travel onward wearing
    # ``complete``, because the pipeline would then publish an empty report
    # that reads as "今日没有重要动态". A genuine complete-but-empty result
    # still says something: a title, a focus summary, a section, a source, or
    # an explicit note about what limited it.
    said_nothing = (
        not events
        and not sections
        and not unique_sources
        and not result.report.title
        and not result.report.focus_title
        and not result.report.focus_summary
        and not coverage.limitations
    )
    if said_nothing and not coverage.is_failed:
        raise ResearchResultError(
            "研究服务没有返回任何结果内容（既没有事件，也没有报告正文或来源）。"
        )

    return result.model_copy(
        update={
            "coverage": coverage,
            "sources": unique_sources,
            "events": events,
            "report": result.report.model_copy(
                update={
                    "sections": sections,
                    "market_snapshot": market,
                    "trend_analysis": trends,
                }
            ),
        }
    )


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ())) or "result"
    return f"{location}: {error.get('msg', '结构不合法')}"


def failed_result(reason: str, limitations: Optional[list[str]] = None) -> ResearchResult:
    """A well-formed *failed* result.

    Used so a research failure travels through the same pipeline as a success
    and cannot be mistaken for "nothing happened today".
    """
    notes = [reason] if reason else []
    notes.extend(limitations or [])
    return ResearchResult(
        coverage=ResearchCoverage(status=COVERAGE_FAILED, limitations=notes[:8]),
    )


#: The schema handed to a text-only research agent as its output contract.
#: Kept as a literal example rather than a JSON Schema: models follow an
#: annotated example far more reliably, and the validator above is what
#: actually enforces correctness.
RESULT_SCHEMA_EXAMPLE: dict[str, Any] = {
    "coverage": {
        "status": "complete|partial|failed",
        "window_from": "2026-09-17",
        "window_to": "2026-09-20",
        "limitations": ["如果有来源无法访问，写在这里；没有就返回空数组"],
        "sources_examined": 26,
    },
    "report": {
        "title": "报告标题",
        "focus_title": "今日焦点的标题",
        "focus_summary": "120-220字，说明本期最重要的事以及为什么重要",
        "sections": [
            {
                "name": "领域名称，例如 移动智能终端侧",
                "summary": "60-140字本领域小结",
                "summary_items": [
                    {
                        "headline": "不夸张的标题",
                        "body": "120-260字事实陈述，只写来源明确写过的内容",
                        "significance": "40-100字，说明为什么重要（这是判断，不是事实）",
                        "evidence_ids": [0, 1],
                    }
                ],
            }
        ],
        "market_snapshot": [
            {"label": "指标含义", "value": "来源中出现的数字", "note": "口径说明", "evidence_ids": [0]}
        ],
        "trend_analysis": [
            {
                "title": "趋势标题",
                "analysis": "100-200字研判，必须明确这是研判",
                "confidence": "high|medium|low",
                "evidence_ids": [0],
            }
        ],
    },
    "events": [
        {
            "title": "事件标题",
            "summary": "这件事本身发生了什么",
            "entities": ["涉及的公司/机构/人"],
            "organization": "主体机构，例如 华为",
            "product_or_project": "产品或项目名，例如 HarmonyOS 6",
            "event_type": "product_launch|partnership|funding|deployment|research|policy|other",
            "event_date": "2026-09-19",
            "significance": "为什么值得记录",
            "importance": 1,
            "section": "对应上面 sections 里的领域名",
            "source_ids": [0],
        }
    ],
    "sources": [
        {
            "source_id": 0,
            "title": "文章标题",
            "publisher": "发布者名称",
            "url": "https://...",
            "published_at": "2026-09-19",
        }
    ],
    "watch_next": ["下一步值得盯的事"],
}
