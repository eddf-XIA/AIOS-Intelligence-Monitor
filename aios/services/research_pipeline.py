"""The Research Agent pipeline: from a brief to a tracked, comparable report.

    Research Agent is the eyes. AIOS remains the memory.

Flow (one :class:`~aios.models.MonitoringRun` with ``engine='agent'``)::

    Research Agent
      -> structured ResearchResult
      -> sources persisted as RawArticle (evidence)
      -> event matching (identity-aware)
      -> IntelligenceEvent / EventObservation / ObservationSource
      -> NEW / UPDATED
      -> Report / ReportSection / ReportItem
      -> history, compare, changes

The agent replaces **how information is discovered**. It does not replace the
intelligence core, and this module is where that promise is kept: every event
the agent describes goes through the same matcher, the same observation table
and the same report tables the Classic pipeline writes to, which is why
``/compare``, the timeline and the diff engine work identically for both.

Two rules carried over verbatim from :mod:`aios.services.pipeline`:

* **Short transactions.** The pipeline runs in a worker thread and commits at
  every checkpoint, so a browser polling from an HTTP thread always sees
  current progress. ORM objects never cross a session boundary - only ids.
* **Coverage honesty.** "We looked and found nothing" and "we could not look"
  are different outcomes with different wording and different run statuses.
  A failed research run never produces an empty "no news" report.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, Optional

import requests
from sqlalchemy import func, select

from ..database import session_scope
from ..models import (
    RUN_ENGINE_AGENT,
    EventObservation,
    IntelligenceEvent,
    RawArticle,
    RunStatus,
)
from ..repositories import articles as articles_repo
from ..repositories import events as events_repo
from ..repositories import reports as reports_repo
from ..repositories import research_topics as topics_repo
from ..repositories import runs as runs_repo
from ..schemas.research import (
    COVERAGE_COMPLETE,
    COVERAGE_FAILED,
    COVERAGE_PARTIAL,
    COVERAGE_PARTIAL_NOTICE,
    ResearchEvent,
    ResearchResult,
    ResearchSource,
)
from ..schemas.research_topic import ResearchBrief
from ..timeutil import local_today, utcnow
from . import report_generator, settings_service
from .article_extractor import canonicalize_url, content_hash, url_hash
from .event_identity import describe_identity, identity_from_candidate
from .event_matcher import EventMatcher, slugify_event_key, unique_event_key
from .intelligence_analyzer import CandidateIntelligence
from .keyring_service import redact
from .llm.service import build_llm_service
from .research import (
    STAGE_ORDER,
    STAGE_REPORTING,
    STAGE_UNDERSTANDING,
    ResearchAgentUnavailable,
    ResearchError,
    ResearchRequest,
    build_agent,
)

logger = logging.getLogger(__name__)

#: Stage prefix on ``MonitoringRun.stage`` for agent runs, so the live view can
#: tell a research stage from a Classic collection stage without guessing.
STAGE_PREFIX = "research"
#: Where an item with no section of its own goes.
DEFAULT_SECTION = "重要动态"
DEFAULT_SECTION_KEY = "research"


class ResearchRunCancelled(Exception):
    """Raised internally when the user asks an in-flight run to stop."""


def stage_value(stage: str) -> str:
    """``research:reading`` - what gets written to ``MonitoringRun.stage``."""
    return f"{STAGE_PREFIX}:{stage}"


def stage_of(raw: str) -> str:
    """The research stage key inside a stored stage value, or ''."""
    text = raw or ""
    if not text.startswith(f"{STAGE_PREFIX}:"):
        return ""
    return text.split(":", 1)[1]


class ResearchPipeline:
    """Executes one Research Agent run end to end."""

    def __init__(
        self,
        run_id: int,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.run_id = run_id
        self._cancel_check = cancel_check or (lambda: False)
        self._http = requests.Session()
        self.errors: list[str] = []
        self._usage_buffer: list[dict] = []
        self._deferred: list[tuple[str, str]] = []

    # -- logging ---------------------------------------------------------

    def log(self, message: str, level: str = "info") -> None:
        """Append a line to the run log. Secrets are scrubbed before storage.

        Must **not** be called while another :func:`session_scope` is open: the
        outer transaction holds SQLite's write lock, so the nested insert would
        stall for the full ``busy_timeout`` and then be dropped.
        """
        safe = redact(message)[:2000]
        try:
            with session_scope() as session:
                runs_repo.add_log(session, self.run_id, safe, level=level)
        except Exception:  # logging must never break a run
            logger.debug("Could not persist research run log", exc_info=True)
        logger.log(
            logging.ERROR if level == "error" else logging.INFO,
            "[research run %s] %s", self.run_id, safe,
        )

    def _defer_log(self, message: str, level: str = "info") -> None:
        self._deferred.append((message, level))

    def _flush_deferred(self) -> None:
        pending, self._deferred = self._deferred, []
        for message, level in pending:
            self.log(message, level=level)
        self._flush_usage()

    def _set_stage(self, stage: str) -> None:
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is not None:
                run.stage = stage

    def _check_cancel(self) -> None:
        if self._cancel_check():
            raise ResearchRunCancelled()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is not None and run.cancel_requested:
                raise ResearchRunCancelled()

    # -- entry point -----------------------------------------------------

    def execute(self) -> str:
        """Run the research pipeline. Returns the final run status."""
        started = utcnow()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is None:
                raise ValueError(f"run {self.run_id} does not exist")
            run.status = RunStatus.RUNNING
            run.started_at = started
            run.stage = stage_value(STAGE_UNDERSTANDING)
            run.engine = RUN_ENGINE_AGENT
            report_date = run.report_date
            topic_id = run.research_topic_id

        if topic_id is None:
            return self._finish(
                RunStatus.FAILED, error="这次运行没有关联研究主题。", coverage=COVERAGE_FAILED
            )

        with session_scope() as session:
            topic = topics_repo.get_topic(session, topic_id)
            if topic is None:
                return self._finish(
                    RunStatus.FAILED, error="研究主题已被删除。", coverage=COVERAGE_FAILED
                )
            request = self._build_request(topic, report_date)
            sections_hint = topics_repo.section_names(session, topic_id)
            topic_name = topic.name
            match_enabled = settings_service.get_bool(session, "event_match_enabled", True)
            match_lookback = settings_service.get_int(session, "event_match_lookback_days", 45)

        request.preferred_sections = sections_hint
        self.log(f"研究主题：{topic_name} · {request.window_text}")

        try:
            self._check_cancel()
            with session_scope() as session:
                agent = build_agent(
                    session,
                    progress=self._on_progress,
                    usage_sink=self._record_usage,
                    http_session=self._http,
                    log=lambda message, level="info": self._defer_log(message, level),
                )
            self.log(f"研究引擎：{agent.spec.display_name}")

            result = agent.run(request)
            self._flush_deferred()
            self._check_cancel()

            if result.coverage.is_failed:
                # An agent that reports failure must not produce a report: an
                # empty report would read as "nothing happened today".
                reason = "；".join(result.coverage.limitations) or "研究服务未返回结果。"
                self.log(f"研究未完成：{reason}", level="error")
                return self._finish(
                    RunStatus.FAILED, error=reason, coverage=COVERAGE_FAILED
                )

            self.log(
                f"研究返回：{len(result.events)} 个候选事件 · "
                f"{len(result.sources)} 个来源 · 覆盖度 {result.coverage.label}"
            )
            return self._persist(result, request, topic_id, report_date, match_enabled, match_lookback)

        except ResearchRunCancelled:
            self.log("已按用户请求取消", level="warn")
            return self._finish(RunStatus.CANCELLED)
        except (ResearchAgentUnavailable, ResearchError) as exc:
            message = redact(str(exc))
            self.log(f"研究未完成：{message}", level="error")
            return self._finish(RunStatus.FAILED, error=message, coverage=COVERAGE_FAILED)
        except Exception as exc:
            message = redact(str(exc))
            self.log(f"研究失败：{message}", level="error")
            logger.exception("Research run %s failed", self.run_id)
            return self._finish(RunStatus.FAILED, error=message, coverage=COVERAGE_FAILED)

    # -- request ---------------------------------------------------------

    @staticmethod
    def _build_request(topic, report_date: dt.date) -> ResearchRequest:
        brief = ResearchBrief.from_topic(topic)
        end = utcnow()
        start = end - dt.timedelta(hours=max(1, brief.window_hours))
        return ResearchRequest(
            topic_name=brief.name,
            brief=brief.brief,
            scope=brief.scope,
            focus_areas=brief.focus_areas,
            exclusions=brief.exclusions,
            keywords=brief.keywords,
            regions=brief.regions,
            window_from=start,
            window_to=end,
            window_hours=brief.window_hours,
            depth=brief.depth,
            report_date=report_date,
        )

    def _on_progress(self, stage: str, note: str = "") -> None:
        """Mirror the agent's human-readable stage onto the run row."""
        if stage not in STAGE_ORDER:
            return
        try:
            self._set_stage(stage_value(stage))
        except Exception:  # pragma: no cover - progress never breaks a run
            logger.debug("Could not record research stage", exc_info=True)
        if note:
            self._defer_log(note)

    # -- persistence -----------------------------------------------------

    def _persist(
        self,
        result: ResearchResult,
        request: ResearchRequest,
        topic_id: int,
        report_date: dt.date,
        match_enabled: bool,
        match_lookback: int,
    ) -> str:
        """Fold a validated result into the intelligence core and publish."""
        self._set_stage(stage_value("organizing"))

        article_ids = self._persist_sources(result.sources, topic_id)
        self._flush_deferred()

        client = build_llm_service(usage_sink=self._record_usage)
        matcher = EventMatcher(client, use_llm=match_enabled)

        recorded: list[dict] = []
        for event in result.events:
            self._check_cancel()
            try:
                item = self._record_event(
                    event, result, article_ids, topic_id, report_date, matcher, match_lookback
                )
                self._flush_deferred()
                if item is not None:
                    recorded.append(item)
            except Exception as exc:
                self._flush_deferred()
                message = redact(str(exc))[:300]
                self.errors.append(f"事件归并失败：{message}")
                self.log(
                    f"  事件归并失败「{event.title[:40]}」：{message}", level="error"
                )

        self._set_stage(stage_value(STAGE_REPORTING))
        self._publish(result, request, recorded, topic_id, report_date, client)

        with session_scope() as session:
            topics_repo.mark_run(session, topic_id)

        status = (
            RunStatus.COMPLETED_WITH_ERRORS
            if self.errors or result.coverage.is_partial
            else RunStatus.COMPLETED
        )
        notes = list(result.coverage.limitations)
        return self._finish(
            status,
            error="",
            coverage=result.coverage.status,
            sources_examined=result.coverage.sources_examined,
            notes=notes,
        )

    def _persist_sources(
        self, sources: list[ResearchSource], topic_id: int
    ) -> dict[int, int]:
        """Store the agent's sources as evidence articles.

        Returns ``{source_id -> article_id}``. Reuses an existing row when the
        same URL was already collected - by the Classic engine or a previous
        research run - so the evidence table does not grow a duplicate for
        every day a storyline is cited.
        """
        mapping: dict[int, int] = {}
        if not sources:
            return mapping

        with session_scope() as session:
            for source in sources:
                url = source.url
                canonical = canonicalize_url(url)
                digest = url_hash(canonical or url)
                existing = session.scalar(
                    select(RawArticle).where(RawArticle.url_hash == digest)
                )
                if existing is not None:
                    existing.last_seen_at = utcnow()
                    if not existing.title and source.title:
                        existing.title = source.title
                    mapping[source.source_id] = existing.id
                    continue

                article = RawArticle(
                    url=url,
                    canonical_url=canonical or url,
                    url_hash=digest,
                    content_hash=content_hash(source.title or url),
                    title=source.title or "",
                    source=source.publisher or "",
                    domain=_domain_of(canonical or url),
                    published_at=_as_datetime(source.published_date),
                    published_raw=source.published_at or "",
                    collected_at=utcnow(),
                    last_seen_at=utcnow(),
                    snippet="",
                    body_text="",
                    # Named for what it is. A research agent's citation is a
                    # different kind of evidence from a document AIOS fetched
                    # and parsed itself, and the audit trail should say so.
                    collector="agent",
                    trust_score=0.0,
                    run_id=self.run_id,
                    topic_id=None,
                    module_id=None,
                    metadata_json={"research_topic_id": topic_id},
                )
                session.add(article)
                session.flush()
                mapping[source.source_id] = article.id

        self._defer_log(f"已记录 {len(mapping)} 个来源作为证据")
        return mapping

    def _record_event(
        self,
        event: ResearchEvent,
        result: ResearchResult,
        article_ids: dict[int, int],
        topic_id: int,
        report_date: dt.date,
        matcher: EventMatcher,
        match_lookback: int,
    ) -> Optional[dict]:
        """Attach one agent event to a new or existing IntelligenceEvent."""
        linked = [article_ids[sid] for sid in event.source_ids if sid in article_ids]
        if not linked:
            # No traceable evidence. Inadmissible, exactly as in the Classic
            # analyser - a report item that cannot be sourced is not published.
            self._defer_log(f"  跳过无来源条目「{event.title[:40]}」", "warn")
            return None

        urls = [
            source.url
            for source in (result.source_by_id(sid) for sid in event.source_ids)
            if source is not None
        ]

        candidate = CandidateIntelligence(
            tag=_tag_for(event),
            title=event.title,
            fact_summary=event.summary or event.title,
            assessment=event.significance,
            importance=event.importance,
            confidence="high" if len(linked) > 1 else "medium",
            article_ids=linked,
            topic_name=event.section or "",
            organization=event.organization,
            product_or_project=event.product_or_project,
            event_type=event.event_type,
            event_date=event.event_date,
            entities=event.entities,
            source_urls=urls,
            section=event.section or DEFAULT_SECTION,
            research_topic_id=topic_id,
        )

        with session_scope() as session:
            existing = _candidate_events(session, topic_id, match_lookback)
            decision = matcher.match(candidate, existing)

            record = (
                session.get(IntelligenceEvent, decision.event_id)
                if decision.is_match
                else None
            )

            identity = describe_identity(identity_from_candidate(candidate))
            event_state = "updated"

            if record is None:
                base_key = slugify_event_key(
                    _key_scope(event, candidate), candidate.title
                )
                key = unique_event_key(
                    base_key, lambda k: events_repo.get_by_key(session, k) is not None
                )
                record = events_repo.create_event(
                    session,
                    event_key=key,
                    title=candidate.title,
                    summary=candidate.fact_summary[:1000],
                    module_id=None,
                    topic_id=None,
                    research_topic_id=topic_id,
                    first_seen_at=utcnow(),
                    last_seen_at=utcnow(),
                    first_seen_date=candidate.event_date or report_date,
                    last_seen_date=report_date,
                    observation_count=0,
                    **identity,
                )
                event_state = "new"
                self._defer_log(
                    f"  NEW 事件：{candidate.title[:60]}（{decision.method}）"
                )
            else:
                self._defer_log(
                    f"  UPDATED 事件 #{record.id}：{candidate.title[:50]}"
                    f"（{decision.method}, 置信度 {decision.confidence:.2f}）"
                )
                # Merge, never overwrite: the fingerprint accumulates so a
                # storyline gets easier to recognise the longer it runs.
                _merge_identity(record, identity)

            observation = events_repo.add_observation(
                session,
                event_id=record.id,
                run_id=self.run_id,
                observation_date=report_date,
                title=candidate.title,
                tag=candidate.tag,
                fact_summary=candidate.fact_summary,
                assessment=candidate.assessment,
                importance=candidate.importance,
                confidence=candidate.confidence,
                structured_data_json=None,
                is_correction=False,
                entities_json=candidate.entities[:12] or None,
                event_type=candidate.event_type or "",
                event_date=candidate.event_date,
            )
            events_repo.link_sources(session, observation.id, linked)

            record.last_seen_at = utcnow()
            record.last_seen_date = report_date
            record.observation_count = (record.observation_count or 0) + 1
            if event_state == "updated":
                record.summary = candidate.fact_summary[:1000]
            session.flush()

            return {
                "event_id": record.id,
                "observation_id": observation.id,
                "event_state": event_state,
                "tag": candidate.tag,
                "title": candidate.title,
                "fact_summary": candidate.fact_summary,
                "assessment": candidate.assessment,
                "importance": candidate.importance,
                "confidence": candidate.confidence,
                "section": candidate.section,
            }

    # -- publication -----------------------------------------------------

    def _publish(
        self,
        result: ResearchResult,
        request: ResearchRequest,
        recorded: list[dict],
        topic_id: int,
        report_date: dt.date,
        client,
    ) -> None:
        """Write the report through the existing report system.

        The agent supplied *structured* report content; AIOS renders it. No
        agent-generated HTML is ever trusted - that is what keeps the design
        consistent, historical reports re-renderable and the output safe.
        """
        sections = self._build_sections(result, recorded)
        overview = {
            "headline": (
                {
                    "title": result.report.focus_title,
                    "body": result.report.focus_summary,
                    "section": "",
                }
                if (result.report.focus_title or result.report.focus_summary)
                else {}
            ),
            "trends": [
                {
                    "title": trend.title,
                    "body": trend.analysis,
                    "confidence": trend.confidence,
                }
                for trend in result.report.trend_analysis
            ],
            "metrics": [
                {"value": row.value, "label": row.label, "note": row.note}
                for row in result.report.market_snapshot
                if row.value
            ],
        }

        title = result.report.title or f"{request.topic_name}情报研究报告"
        model_label = _model_label(client)

        with session_scope() as session:
            report = report_generator.persist_report(
                session,
                report_date=report_date,
                run_id=self.run_id,
                sections=sections,
                overview=overview,
                model=model_label,
                title=f"{title} · {report_date.isoformat()}",
                coverage=self._coverage_payload(result),
            )
            report.research_topic_id = topic_id
            report.engine = RUN_ENGINE_AGENT
            report.coverage_status = result.coverage.status
            session.flush()
            report_id = report.id

        written = None
        with session_scope() as session:
            stored = reports_repo.get_report(session, report_id)
            if stored is not None:
                written = report_generator.export_report(session, stored)
        if written is not None:
            self.log(f"报告已生成：{written[0].name}")

    def _build_sections(
        self, result: ResearchResult, recorded: list[dict]
    ) -> list[report_generator.SectionPayload]:
        """Map the agent's report sections onto ReportSection payloads.

        Items are keyed to the events actually recorded, so a section can never
        display a paragraph whose underlying event was rejected for lacking
        evidence.
        """
        by_section: dict[str, list[dict]] = {}
        for item in recorded:
            by_section.setdefault(item.get("section") or DEFAULT_SECTION, []).append(item)

        payloads: list[report_generator.SectionPayload] = []
        order = 0
        used: set[str] = set()

        for section in result.report.sections:
            items = by_section.get(section.name, [])
            used.add(section.name)
            order += 10
            payloads.append(
                report_generator.SectionPayload(
                    module_id=None,
                    module_key=_section_key(section.name),
                    module_name=section.name,
                    sort_order=order,
                    status="new" if items else "watch",
                    # Research coverage is recorded per report, not per
                    # section: the agent reports one verdict for the whole
                    # pass, so claiming a per-section state would invent one.
                    coverage_state=(
                        "partial_collection"
                        if result.coverage.is_partial
                        else ("ok" if result.coverage.status == COVERAGE_COMPLETE else "")
                    ),
                    summary=section.summary,
                    metrics=[],
                    items=sorted(items, key=lambda i: i.get("importance", 3)),
                )
            )

        # Events the agent placed in a section it never declared still have to
        # reach the report: dropping them would lose intelligence over a
        # labelling inconsistency.
        for name, items in by_section.items():
            if name in used:
                continue
            order += 10
            payloads.append(
                report_generator.SectionPayload(
                    module_id=None,
                    module_key=_section_key(name),
                    module_name=name,
                    sort_order=order,
                    status="new",
                    coverage_state="ok" if result.coverage.status == COVERAGE_COMPLETE else "",
                    summary="",
                    metrics=[],
                    items=sorted(items, key=lambda i: i.get("importance", 3)),
                )
            )

        if not payloads:
            # A complete pass that found nothing is a real result and still
            # gets a report - with wording that says so truthfully.
            payloads.append(
                report_generator.SectionPayload(
                    module_id=None,
                    module_key=DEFAULT_SECTION_KEY,
                    module_name=DEFAULT_SECTION,
                    sort_order=10,
                    status="watch",
                    coverage_state=(
                        "partial_collection"
                        if result.coverage.is_partial
                        else ("ok" if result.coverage.status == COVERAGE_COMPLETE else "")
                    ),
                    summary="",
                    metrics=[],
                    items=[],
                )
            )
        return payloads

    def _coverage_payload(self, result: ResearchResult) -> dict:
        """What the report records about this pass's coverage."""
        notes = list(result.coverage.limitations)
        if result.watch_next:
            notes = notes  # watch_next is editorial, not a coverage caveat
        return {
            "degraded": result.coverage.is_partial or bool(self.errors),
            "recorded": True,
            "engine": RUN_ENGINE_AGENT,
            "status": result.coverage.status,
            "sources": [],
            "notes": notes[:6],
            "failed_topics": [],
            "sources_examined": result.coverage.sources_examined,
            "window": {
                "from": result.coverage.window_from,
                "to": result.coverage.window_to,
            },
            "watch_next": list(result.watch_next)[:8],
        }

    # -- bookkeeping -----------------------------------------------------

    def _record_usage(self, fields: dict) -> None:
        """Buffer one model/agent call record until the transaction closes."""
        self._usage_buffer.append(fields)

    def _flush_usage(self) -> None:
        pending, self._usage_buffer = self._usage_buffer, []
        if not pending:
            return
        try:
            with session_scope() as session:
                for fields in pending:
                    runs_repo.add_usage(session, run_id=self.run_id, **fields)
        except Exception:
            logger.debug("Could not record research usage", exc_info=True)

    def _finish(
        self,
        status: str,
        error: str = "",
        coverage: str = "",
        sources_examined: int = 0,
        notes: Optional[list[str]] = None,
    ) -> str:
        self._flush_deferred()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is None:
                return status
            run.status = status
            run.finished_at = utcnow()
            run.stage = status
            run.engine = RUN_ENGINE_AGENT
            if coverage:
                run.coverage_status = coverage
            if sources_examined:
                run.total_sources_examined = sources_examined

            messages = list(self.errors)
            messages.extend(notes or [])
            if error:
                run.error_message = error[:2000]
            elif messages:
                run.error_message = "; ".join(messages)[:2000]

            run.total_articles = articles_repo.count_for_run(session, self.run_id)
            run.total_candidates = run.total_articles

            observations = list(
                session.scalars(
                    select(EventObservation).where(EventObservation.run_id == self.run_id)
                )
            )
            touched = {o.event_id for o in observations}
            run.total_events = len(touched)
            # An event whose only observation is this run's is new today.
            run.total_new_events = (
                session.scalar(
                    select(func.count())
                    .select_from(IntelligenceEvent)
                    .where(
                        IntelligenceEvent.id.in_(touched or {-1}),
                        IntelligenceEvent.observation_count == 1,
                    )
                )
                or 0
            )
            run.total_updated_events = max(0, run.total_events - run.total_new_events)

            report = reports_repo.get_by_run(session, self.run_id)
            run.total_report_items = report.item_count if report else 0

        self.log(f"运行结束：{status}")
        return status


# --- helpers ----------------------------------------------------------------

def _candidate_events(session, topic_id: int, lookback_days: int):
    """Recent events for one research topic - the matcher's stage-1 scope."""
    from ..models import EventStatus

    cutoff = utcnow() - dt.timedelta(days=max(lookback_days, 1))
    stmt = (
        select(IntelligenceEvent)
        .where(
            IntelligenceEvent.research_topic_id == topic_id,
            IntelligenceEvent.status.in_([EventStatus.ACTIVE, EventStatus.WATCHING]),
            IntelligenceEvent.last_seen_at >= cutoff,
        )
        .order_by(IntelligenceEvent.last_seen_at.desc())
        .limit(60)
    )
    return list(session.scalars(stmt))


def _merge_identity(record: IntelligenceEvent, identity: dict) -> None:
    """Enrich a matched event's fingerprint without discarding what it knew."""
    for field in ("organization", "product_or_project", "event_type"):
        value = identity.get(field) or ""
        if value and not getattr(record, field, ""):
            setattr(record, field, value)

    for field, cap in (("entities_json", 12), ("canonical_urls_json", 40)):
        incoming = identity.get(field) or []
        if not incoming:
            continue
        merged: list[str] = []
        seen: set[str] = set()
        for value in list(getattr(record, field, None) or []) + list(incoming):
            text = str(value)
            if text and text not in seen:
                seen.add(text)
                merged.append(text)
            if len(merged) >= cap:
                break
        setattr(record, field, merged)


def _tag_for(event: ResearchEvent) -> str:
    """The short chip shown above a report card."""
    from .event_identity import normalize_type

    labels = {
        "product_launch": "产品发布",
        "partnership": "合作",
        "funding": "融资",
        "deployment": "商业落地",
        "research": "技术研究",
        "policy": "政策",
        "regulatory": "监管",
        "acquisition": "并购",
        "personnel": "人事",
        "incident": "风险",
        "financial": "财务",
    }
    return labels.get(normalize_type(event.event_type), "情报")[:32]


def _key_scope(event: ResearchEvent, candidate: CandidateIntelligence) -> str:
    """A readable prefix for the event key, so keys stay human-scannable."""
    from .event_identity import normalize_name

    for value in (event.product_or_project, event.organization, candidate.section):
        key = normalize_name(value)
        if key:
            return key[:24]
    return "research"


def _section_key(name: str) -> str:
    """A stable-ish key for a report section the agent named.

    Sections are identified by their denormalised ``module_name`` in reports,
    so this key only needs to be readable, not unique across all time.
    """
    from .event_identity import normalize_name

    return (normalize_name(name) or DEFAULT_SECTION_KEY)[:60]


def _domain_of(url: str) -> str:
    from .collector import domain_of

    try:
        return domain_of(url)
    except Exception:  # pragma: no cover - defensive
        return ""


def _as_datetime(value: Optional[dt.date]) -> Optional[dt.datetime]:
    if value is None:
        return None
    return dt.datetime(value.year, value.month, value.day)


def _model_label(client) -> str:
    """What to stamp on the report as the engine that produced it."""
    try:
        routing = client.describe_routing()
    except Exception:  # pragma: no cover - labelling must not fail a run
        return ""
    if not routing:
        return ""
    first = next(iter(routing.values()))
    return f"{first.display_name} / {first.model}"


def run_research_pipeline(
    run_id: int, cancel_check: Optional[Callable[[], bool]] = None
) -> str:
    """Convenience entry point used by the run manager and the scheduler."""
    return ResearchPipeline(run_id, cancel_check=cancel_check).execute()


def create_research_run(session, topic_id: int, trigger_type: str = "manual"):
    """Create a queued research run for one topic."""
    run = runs_repo.create_run(
        session, report_date=local_today(), trigger_type=trigger_type
    )
    run.engine = RUN_ENGINE_AGENT
    run.research_topic_id = topic_id
    session.flush()
    return run
