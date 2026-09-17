"""Import historical reports produced by the original ``aios_daily.py``.

The legacy audit JSON looks like::

    {"date": "2026-09-15",
     "sections": [{"section": "移动智能终端侧", "status": "new",
                   "items": [{"tag", "title", "summary", "assessment",
                              "importance", "confidence", "source_ids"}],
                   "metrics": [...],
                   "_evidence": [{"id", "title", "source", "published_at", "url", "text"}]}],
     "overview": {"headline": {...}, "trends": [...], "metrics": [...]}}

Everything that maps cleanly is imported: report, sections, items, and the
evidence articles behind each item. Legacy data has no event history, so each
imported item gets a single-observation event flagged ``legacy_import=True``
rather than being force-fitted into the matching pipeline. Diffs against
imported days therefore show real content but no cross-day event continuity,
which is the honest representation of what that data actually contains.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import Report, ReportItem, ReportSection
from ..repositories import articles as articles_repo
from ..repositories import events as events_repo
from ..repositories import modules as modules_repo
from ..repositories import reports as reports_repo
from ..timeutil import utcnow
from .article_extractor import canonicalize_url, content_hash, url_hash
from .collector import domain_of
from .event_matcher import slugify_event_key, unique_event_key
from .report_generator import export_report

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """What an import actually created."""

    report_id: Optional[int] = None
    report_date: Optional[dt.date] = None
    sections: int = 0
    items: int = 0
    articles: int = 0
    events: int = 0
    skipped: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report_id is not None

    def summary(self) -> str:
        if self.skipped:
            return f"{self.report_date}: already imported, skipped."
        if not self.ok:
            return "; ".join(self.messages) or "Import failed."
        return (
            f"{self.report_date}: {self.sections} sections, {self.items} items, "
            f"{self.articles} sources, {self.events} events."
        )


def detect_format(payload: dict) -> str:
    """``legacy`` for the old script output, ``v2`` for this app's own audit."""
    if payload.get("schema_version") == 2 or "report_items" in payload:
        return "v2"
    if "sections" in payload and "overview" in payload:
        return "legacy"
    return "unknown"


def _parse_published(value: str) -> Optional[dt.datetime]:
    """Legacy evidence stores RFC822 or GDELT strings; both are best-effort."""
    if not value:
        return None
    import email.utils

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return parsed
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _import_evidence(
    session: Session, evidence: list[dict], module_id: Optional[int]
) -> dict[int, int]:
    """Store legacy evidence as articles; returns evidence-id -> article-id."""
    mapping: dict[int, int] = {}
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        evidence_id = entry.get("id")
        if not isinstance(evidence_id, int):
            continue
        body = entry.get("text") or ""
        payload = {
            "url": url,
            "canonical_url": canonicalize_url(url),
            "url_hash": url_hash(url),
            "content_hash": content_hash(body),
            "title": entry.get("title") or "",
            "source": (entry.get("source") or domain_of(url))[:300],
            "domain": domain_of(url)[:200],
            "published_at": _parse_published(entry.get("published_at") or ""),
            "published_raw": (entry.get("published_at") or "")[:120],
            "collected_at": utcnow(),
            "last_seen_at": utcnow(),
            "snippet": "",
            "body_text": body,
            "language": "",
            "trust_score": 0.0,
            "collector": "legacy_import",
            "module_id": module_id,
            "metadata_json": {"legacy_import": True},
        }
        try:
            article, _ = articles_repo.upsert(session, payload)
            mapping[evidence_id] = article.id
        except Exception as exc:
            logger.debug("Skipping legacy evidence %s: %s", url[:80], exc)
    return mapping


def import_legacy_payload(
    session: Session, payload: dict, source_name: str = "", overwrite: bool = False
) -> ImportResult:
    """Import one legacy audit JSON document into the database."""
    result = ImportResult()

    raw_date = payload.get("date") or payload.get("report_date")
    if not raw_date:
        result.messages.append("The file has no 'date' field.")
        return result
    try:
        report_date = dt.datetime.strptime(str(raw_date).strip(), "%Y-%m-%d").date()
    except ValueError:
        result.messages.append(f"Unrecognised date: {raw_date!r}")
        return result
    result.report_date = report_date

    existing = reports_repo.get_by_date(session, report_date)
    if existing is not None:
        if not overwrite:
            result.skipped = True
            result.report_id = existing.id
            result.messages.append(
                f"A report already exists for {report_date}; import skipped."
            )
            return result
        reports_repo.delete_report(session, existing)
        session.flush()

    overview = payload.get("overview") or {}
    report = Report(
        report_date=report_date,
        run_id=None,
        title=f"全球智能终端操作系统监测日报 · {report_date.isoformat()}",
        headline_json=overview.get("headline") or {},
        trends_json=overview.get("trends") or [],
        metrics_json=overview.get("metrics") or [],
        model=payload.get("model", "") or "legacy",
        legacy_import=True,
        created_at=utcnow(),
    )
    session.add(report)
    session.flush()
    result.report_id = report.id

    known_modules = {m.name: m for m in modules_repo.list_modules(session, include_archived=True)}

    for order, raw_section in enumerate(payload.get("sections") or []):
        if not isinstance(raw_section, dict):
            continue
        module_name = raw_section.get("section") or f"未命名领域 {order + 1}"
        module = known_modules.get(module_name)

        section = ReportSection(
            report_id=report.id,
            module_id=module.id if module else None,
            module_key=module.key if module else "",
            module_name=module_name,
            sort_order=order * 10,
            status=raw_section.get("status") or "watch",
            summary="",
            metrics_json=raw_section.get("metrics") or [],
        )
        session.add(section)
        session.flush()
        result.sections += 1

        evidence_map = _import_evidence(
            session, raw_section.get("_evidence") or [], module.id if module else None
        )
        result.articles += len(evidence_map)

        for item_order, raw_item in enumerate(raw_section.get("items") or []):
            if not isinstance(raw_item, dict):
                continue
            title = (raw_item.get("title") or "").strip()
            fact = (raw_item.get("fact_summary") or raw_item.get("summary") or "").strip()
            if not title:
                continue

            article_ids = [
                evidence_map[sid]
                for sid in (raw_item.get("source_ids") or [])
                if isinstance(sid, int) and sid in evidence_map
            ]

            # One event + one observation per legacy item, explicitly flagged.
            base_key = slugify_event_key(
                module.key if module else "legacy", title, suffix="legacy"
            )
            key = unique_event_key(
                base_key, lambda k: events_repo.get_by_key(session, k) is not None
            )
            event = events_repo.create_event(
                session,
                event_key=key,
                title=title,
                summary=fact[:1000],
                module_id=module.id if module else None,
                topic_id=None,
                first_seen_at=utcnow(),
                last_seen_at=utcnow(),
                first_seen_date=report_date,
                last_seen_date=report_date,
                observation_count=1,
                legacy_import=True,
            )
            observation = events_repo.add_observation(
                session,
                event_id=event.id,
                run_id=None,
                observation_date=report_date,
                title=title,
                tag=(raw_item.get("tag") or "")[:64],
                fact_summary=fact,
                assessment=(raw_item.get("assessment") or "").strip(),
                importance=_safe_int(raw_item.get("importance"), 3),
                confidence=(raw_item.get("confidence") or "medium"),
                structured_data_json=raw_item.get("structured_data") or None,
                legacy_import=True,
            )
            if article_ids:
                events_repo.link_sources(session, observation.id, article_ids)

            session.add(
                ReportItem(
                    report_section_id=section.id,
                    event_id=event.id,
                    observation_id=observation.id,
                    tag=(raw_item.get("tag") or "")[:64],
                    title=title,
                    fact_summary=fact,
                    assessment=(raw_item.get("assessment") or "").strip(),
                    importance=_safe_int(raw_item.get("importance"), 3),
                    confidence=(raw_item.get("confidence") or "medium"),
                    event_state="new",
                    sort_order=item_order,
                )
            )
            result.items += 1
            result.events += 1

    session.flush()
    if source_name:
        result.messages.append(f"Imported from {source_name}.")
    logger.info("Imported legacy report %s (%s items)", report_date, result.items)
    return result


def import_file(session: Session, path: Path, overwrite: bool = False) -> ImportResult:
    """Read a JSON file from disk and import it."""
    result = ImportResult()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.messages.append(f"File not found: {path}")
        return result
    except json.JSONDecodeError as exc:
        result.messages.append(f"Not valid JSON: {exc}")
        return result

    if not isinstance(payload, dict):
        result.messages.append("The JSON root must be an object.")
        return result

    kind = detect_format(payload)
    if kind == "unknown":
        result.messages.append(
            "Unrecognised report format - expected the legacy 'sections'/'overview' shape."
        )
        return result

    imported = import_legacy_payload(
        session, payload, source_name=Path(path).name, overwrite=overwrite
    )
    if imported.ok and not imported.skipped:
        stored = reports_repo.get_report(session, imported.report_id)
        if stored is not None:
            try:
                export_report(session, stored)
            except Exception as exc:  # export is a convenience, not a requirement
                imported.messages.append(f"Could not write export files: {exc}")
    return imported


def _safe_int(value: Any, default: int) -> int:
    try:
        return max(1, min(int(value), 5))
    except (TypeError, ValueError):
        return default
