"""The monitoring pipeline: from search queries to a published report.

Flow (one :class:`~aios.models.MonitoringRun`)::

    collect -> dedupe -> extract bodies -> rank -> persist articles
      -> topic analysis -> event matching -> observations
      -> module summary -> synthesis -> report -> HTML + JSON audit

Two structural rules:

* **Failure isolation.** A dead source, a bad topic or a whole failing module
  degrades that branch only. The run finishes as ``completed_with_errors``
  rather than losing everything that did work.
* **Short transactions.** The pipeline runs in a worker thread and commits at
  every checkpoint, so the dashboard polling from an HTTP thread always sees
  current progress. ORM objects are never carried across session boundaries -
  only ids.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import session_scope
from ..models import (
    EventObservation,
    IntelligenceEvent,
    ModuleRunStatus,
    RawArticle,
    RunStatus,
)
from ..repositories import articles as articles_repo
from ..repositories import events as events_repo
from ..repositories import modules as modules_repo
from ..repositories import reports as reports_repo
from ..repositories import runs as runs_repo
from ..repositories import sources as sources_repo
from ..timeutil import local_today, utcnow
from . import network_service, report_generator, settings_service
from .article_extractor import ArticleExtractor, canonicalize_url, content_hash, url_hash
from .collection_planner import (
    CollectionPlanner,
    TopicCollectionOutcome,
    TopicCollectionStatus,
    aggregate_module_status,
    shared_cache,
)
from .collector import Candidate, CollectorRegistry, window_for
from .deduplicator import dedupe_candidates, filter_excluded
from .llm import LLMError, ProviderNotConfigured
from .llm.service import build_llm_service
from .event_matcher import EventMatcher, slugify_event_key, unique_event_key
from .intelligence_analyzer import (
    CandidateIntelligence,
    IntelligenceAnalyzer,
    build_evidence,
)
from .keyring_service import redact
from .source_health import HealthState, SourceHealthTracker
from .source_scoring import ScoringContext, rank

logger = logging.getLogger(__name__)


class RunCancelled(Exception):
    """Raised internally when the user asks an in-flight run to stop."""


@dataclass
class TopicPlan:
    """Immutable snapshot of one topic's configuration, read before collection."""

    topic_id: int
    name: str
    description: str
    analysis_prompt: str
    queries: list[str]
    preferred_domains: list[tuple[str, int]]
    excluded_keywords: list[str]


@dataclass
class ModulePlan:
    """Immutable snapshot of one module's configuration."""

    module_id: int
    key: str
    name: str
    analysis_prompt: str
    lookback_days: int
    max_candidates: int
    max_report_items: int
    sort_order: int
    topics: list[TopicPlan] = field(default_factory=list)
    preferred_domains: list[tuple[str, int]] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)


def build_plan(session: Session) -> list[ModulePlan]:
    """Read the whole monitoring configuration into plain objects.

    Done once up front so the long-running pipeline never holds ORM instances
    across sessions, and so a config edit mid-run cannot corrupt the run.
    """
    plans: list[ModulePlan] = []
    for module in modules_repo.list_enabled_modules(session):
        module_domains = [
            (p.domain, p.priority) for p in module.preferred_sources if p.enabled
        ]
        module_excluded = [k.keyword for k in module.excluded_keywords if k.enabled]

        topics: list[TopicPlan] = []
        for topic in module.active_topics:
            queries = [q.query.strip() for q in topic.active_queries if q.query.strip()]
            if not queries:
                continue
            topics.append(
                TopicPlan(
                    topic_id=topic.id,
                    name=topic.name,
                    description=topic.description or "",
                    analysis_prompt=topic.analysis_prompt or "",
                    queries=queries,
                    preferred_domains=module_domains
                    + [(p.domain, p.priority) for p in topic.preferred_sources if p.enabled],
                    excluded_keywords=module_excluded
                    + [k.keyword for k in topic.excluded_keywords if k.enabled],
                )
            )

        if not topics:
            continue

        plans.append(
            ModulePlan(
                module_id=module.id,
                key=module.key,
                name=module.name,
                analysis_prompt=module.analysis_prompt or "",
                lookback_days=max(1, module.lookback_days or 3),
                max_candidates=max(1, module.max_candidates or 18),
                max_report_items=max(1, module.max_report_items or 2),
                sort_order=module.sort_order or 0,
                topics=topics,
                preferred_domains=module_domains,
                excluded_keywords=module_excluded,
            )
        )
    return plans


class _PreferredRow:
    """Adapter so plan tuples can reuse :meth:`ScoringContext.from_rows`."""

    __slots__ = ("domain", "priority", "enabled")

    def __init__(self, domain: str, priority: int) -> None:
        self.domain = domain
        self.priority = priority
        self.enabled = True


class MonitoringPipeline:
    """Executes one monitoring run end to end."""

    def __init__(
        self,
        run_id: int,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.run_id = run_id
        self._cancel_check = cancel_check or (lambda: False)
        self._http = requests.Session()
        self.errors: list[str] = []
        self._deferred: list[tuple[str, str, str]] = []
        self._usage_buffer: list[dict] = []
        #: Real per-source counters for this run. Everything the UI shows about
        #: source health is derived from these, never estimated.
        self.health = SourceHealthTracker()
        self.planner: Optional[CollectionPlanner] = None
        #: Topics whose sources could not be reached at all. Distinct from
        #: topics where the sources answered and found nothing.
        self.collection_failures: list[str] = []

    # -- logging -------------------------------------------------------------

    def log(self, message: str, level: str = "info", module_key: str = "") -> None:
        """Append a line to the run log. Secrets are scrubbed before storage.

        Must **not** be called while another :func:`session_scope` is open: the
        outer transaction holds SQLite's write lock, so the nested insert would
        stall for the full ``busy_timeout`` and then be dropped. Use
        :meth:`_defer_log` inside a session block instead.
        """
        safe = redact(message)[:2000]
        try:
            with session_scope() as session:
                runs_repo.add_log(session, self.run_id, safe, level=level, module_key=module_key)
        except Exception:  # logging must never break the run
            logger.debug("Could not persist run log", exc_info=True)
        logger.log(logging.ERROR if level == "error" else logging.INFO, "[run %s] %s", self.run_id, safe)

    def _defer_log(self, message: str, level: str = "info", module_key: str = "") -> None:
        """Queue a log line to be written once the current transaction closes."""
        self._deferred.append((message, level, module_key))

    def _flush_deferred(self) -> None:
        """Write queued log lines and LLM usage. Safe only outside a session scope."""
        pending, self._deferred = self._deferred, []
        for message, level, module_key in pending:
            self.log(message, level=level, module_key=module_key)
        self._flush_usage()

    def _set_stage(self, stage: str) -> None:
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run:
                run.stage = stage

    def _check_cancel(self) -> None:
        if self._cancel_check():
            raise RunCancelled()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run and run.cancel_requested:
                raise RunCancelled()

    # -- entry point ---------------------------------------------------------

    def execute(self) -> str:
        """Run the pipeline. Returns the final run status."""
        started = utcnow()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is None:
                raise ValueError(f"run {self.run_id} does not exist")
            run.status = RunStatus.RUNNING
            run.started_at = started
            run.stage = "planning"
            report_date = run.report_date

        self.log("Started")

        try:
            with session_scope() as session:
                plans = build_plan(session)
                article_max_chars = settings_service.get_int(session, "article_max_chars", 7000)
                http_timeout = settings_service.get_int(session, "http_timeout", 20)
                workers = max(1, settings_service.get_int(session, "collect_workers", 6))
                fetch_top_n = max(1, settings_service.get_int(session, "fetch_body_top_n", 8))
                match_enabled = settings_service.get_bool(session, "event_match_enabled", True)
                match_lookback = settings_service.get_int(session, "event_match_lookback_days", 45)

                collection_config = network_service.collection_settings(session)
                feeds = network_service.feed_specs(session)
                precheck = settings_service.get_bool(session, "network_precheck_enabled", True)

                for plan in plans:
                    runs_repo.create_module_run(session, self.run_id, plan.module_id, plan.name)

            if not plans:
                self.log("No enabled module has any enabled topic with queries.", level="warn")
                return self._finish(RunStatus.COMPLETED, report_date)

            # One router for the whole run: it resolves provider + model per
            # task, so a module can analyse with one vendor and synthesise with
            # another without this class knowing either of them.
            client = build_llm_service(usage_sink=self._record_usage)
            readiness = client.readiness_error()
            if readiness:
                raise ProviderNotConfigured(readiness)

            analyzer = IntelligenceAnalyzer(client)
            matcher = EventMatcher(client, use_llm=match_enabled)
            extractor = ArticleExtractor(
                max_chars=article_max_chars, timeout=http_timeout, session=self._http
            )
            registry = network_service.build_registry(collection_config, feeds=feeds)
            self.planner = CollectionPlanner(
                registry=registry,
                health=self.health,
                cache=shared_cache(collection_config.cache_ttl_minutes * 60),
                network_mode=collection_config.network_mode,
                min_candidates=collection_config.min_candidates,
                log=lambda message, level="info": self.log(message, level=level),
            )

            self.log(
                f"Plan: {len(plans)} modules, "
                f"{sum(len(p.topics) for p in plans)} topics, "
                f"{sum(len(t.queries) for p in plans for t in p.topics)} queries"
            )
            self._prepare_network(registry, collection_config, precheck)

            section_payloads: list[report_generator.SectionPayload] = []
            for plan in plans:
                self._check_cancel()
                payload = self._run_module(
                    plan=plan,
                    report_date=report_date,
                    registry=registry,
                    extractor=extractor,
                    analyzer=analyzer,
                    matcher=matcher,
                    workers=workers,
                    fetch_top_n=fetch_top_n,
                    match_lookback=match_lookback,
                )
                if payload is not None:
                    section_payloads.append(payload)

            self._check_cancel()
            self._set_stage("synthesizing")
            self.log("Synthesising report overview")
            overview = self._synthesize(analyzer, section_payloads, report_date)

            self._set_stage("generating_report")
            self._publish(
                section_payloads, overview, report_date, self._model_label(client)
            )

            return self._finish(self._final_status(), report_date)

        except RunCancelled:
            self.log("Cancelled by user", level="warn")
            return self._finish(RunStatus.CANCELLED, None)
        except Exception as exc:
            message = redact(str(exc))
            self.log(f"Run failed: {message}", level="error")
            logger.exception("Run %s failed", self.run_id)
            return self._finish(RunStatus.FAILED, None, error=message)

    # -- per-module ----------------------------------------------------------

    def _run_module(
        self,
        plan: ModulePlan,
        report_date: dt.date,
        registry: CollectorRegistry,
        extractor: ArticleExtractor,
        analyzer: IntelligenceAnalyzer,
        matcher: EventMatcher,
        workers: int,
        fetch_top_n: int,
        match_lookback: int,
    ) -> Optional[report_generator.SectionPayload]:
        """Execute one module. Never raises: failures are recorded and isolated."""
        module_run_id = self._module_run_id(plan.module_id)
        payload = report_generator.SectionPayload(
            module_id=plan.module_id,
            module_key=plan.key,
            module_name=plan.name,
            sort_order=plan.sort_order,
        )

        try:
            self._set_stage(f"collecting:{plan.key}")
            self._update_module_run(
                module_run_id, status=ModuleRunStatus.COLLECTING, collect_started_at=utcnow()
            )
            self.log(f"Collecting {plan.name}", module_key=plan.key)

            start, end = window_for(report_date, plan.lookback_days)
            topic_articles: dict[int, list[int]] = {}
            outcomes: list[TopicCollectionOutcome] = []
            total_candidates = 0
            total_articles = 0

            for topic in plan.topics:
                self._check_cancel()
                outcome = self._collect_topic(registry, topic, start, end, plan, workers)
                outcomes.append(outcome)
                total_candidates += len(outcome.candidates)

                if outcome.collection_failed:
                    # Sources could not be reached. This is emphatically not
                    # "no news": we do not know whether there was any.
                    self.collection_failures.append(f"{plan.name}/{topic.name}")
                    self.log(
                        f"  {topic.name}: {outcome.describe()}",
                        level="warn",
                        module_key=plan.key,
                    )
                    continue

                if not outcome.candidates:
                    self.log(f"  {topic.name}: {outcome.describe()}", module_key=plan.key)
                    continue

                self._update_module_run(module_run_id, status=ModuleRunStatus.EXTRACTING)
                selected = self._prepare_candidates(
                    outcome.candidates, topic, plan, extractor, fetch_top_n
                )
                article_ids = self._persist_articles(selected, plan, topic)
                topic_articles[topic.topic_id] = article_ids
                total_articles += len(article_ids)
                self.log(
                    f"  {topic.name}: {len(outcome.candidates)} candidates -> "
                    f"{len(article_ids)} articles"
                    + (f"（{'、'.join(outcome.failed_sources)} 异常）"
                       if outcome.failed_sources else ""),
                    module_key=plan.key,
                )

            coverage = aggregate_module_status(outcomes)
            payload.coverage_state = coverage
            self._update_module_run(
                module_run_id,
                collect_finished_at=utcnow(),
                candidate_count=total_candidates,
                article_count=total_articles,
                collection_status=coverage,
            )
            self._persist_health()

            if not topic_articles:
                # Truthful empty-module wording: only claim there was no news
                # when the sources actually answered.
                degraded = coverage in (
                    TopicCollectionStatus.FAILED,
                    TopicCollectionStatus.PARTIAL,
                )
                self._update_module_run(
                    module_run_id,
                    status=(
                        ModuleRunStatus.COMPLETED_WITH_WARNINGS
                        if degraded
                        else ModuleRunStatus.COMPLETED
                    ),
                )
                payload.status = "watch"
                payload.summary = (
                    "本期数据源访问异常，无法确认是否存在新增动态。"
                    if coverage == TopicCollectionStatus.FAILED
                    else (
                        "本期部分数据源访问异常，未采集到可用来源。"
                        if degraded
                        else "本期未采集到相关来源，延续观察。"
                    )
                )
                return payload

            # --- analysis ---
            self._check_cancel()
            self._set_stage(f"analyzing:{plan.key}")
            self._update_module_run(
                module_run_id, status=ModuleRunStatus.ANALYZING, analysis_started_at=utcnow()
            )
            self.log(f"模型分析 {plan.name}", module_key=plan.key)

            candidates_intel = self._analyze_topics(
                plan, topic_articles, report_date, analyzer
            )

            # --- event matching ---
            self._check_cancel()
            self._set_stage(f"matching:{plan.key}")
            self._update_module_run(module_run_id, status=ModuleRunStatus.MATCHING)
            items = self._match_and_record(
                plan, candidates_intel, report_date, matcher, match_lookback
            )

            payload.items = items
            payload.status = "new" if items else "watch"

            summary = analyzer.analyze_module(plan.name, report_date.isoformat(), candidates_intel)
            payload.summary = summary.get("summary", "")
            payload.metrics = summary.get("metrics", [])
            if not items:
                payload.status = "watch"

            self._update_module_run(
                module_run_id,
                status=(
                    ModuleRunStatus.COMPLETED_WITH_WARNINGS
                    if coverage == TopicCollectionStatus.PARTIAL
                    else ModuleRunStatus.COMPLETED
                ),
                analysis_finished_at=utcnow(),
                selected_count=len(items),
            )
            self._flush_usage()
            self.log(f"{plan.name} completed: {len(items)} report items", module_key=plan.key)
            return payload

        except RunCancelled:
            self._update_module_run(module_run_id, status=ModuleRunStatus.SKIPPED)
            raise
        except Exception as exc:
            message = redact(str(exc))[:500]
            self.errors.append(f"{plan.name}: {message}")
            self._update_module_run(
                module_run_id, status=ModuleRunStatus.FAILED, error_message=message
            )
            self.log(f"{plan.name} failed: {message}", level="error", module_key=plan.key)
            logger.exception("Module %s failed", plan.key)
            # Keep the section so the report still shows the module as observed.
            payload.status = "watch"
            payload.summary = "本领域本期执行失败，未产出情报。"
            return payload

    # -- collection ----------------------------------------------------------

    def _collect_topic(
        self,
        registry: CollectorRegistry,
        topic: TopicPlan,
        start: dt.datetime,
        end: dt.datetime,
        plan: ModulePlan,
        workers: int,
    ) -> TopicCollectionOutcome:
        """Collect one topic through the planner.

        The planner decides which sources to ask and in which order, skips a
        fallback that nothing needs, reuses a query another topic already ran,
        and - crucially - reports *why* it has no candidates.
        """
        per_query = max(4, plan.max_candidates)
        planner = self.planner
        if planner is None:  # pragma: no cover - execute() always builds one
            planner = CollectionPlanner(registry=registry, health=self.health)
            self.planner = planner

        try:
            return planner.collect_topic(
                queries=topic.queries,
                start=start,
                end=end,
                limit=per_query,
                topic_name=topic.name,
                workers=workers,
            )
        except Exception as exc:  # pragma: no cover - planner isolates failures
            self.log(
                f"  {topic.name} 采集异常：{redact(str(exc))[:200]}",
                level="error",
                module_key=plan.key,
            )
            return TopicCollectionOutcome(
                topic_name=topic.name, status=TopicCollectionStatus.FAILED
            )

    def _prepare_candidates(
        self,
        candidates: list[Candidate],
        topic: TopicPlan,
        plan: ModulePlan,
        extractor: ArticleExtractor,
        fetch_top_n: int,
    ) -> list[Candidate]:
        """Filter, rank, fetch bodies for the best few, then re-rank."""
        context = ScoringContext.from_rows(
            [_PreferredRow(domain, priority) for domain, priority in topic.preferred_domains]
        )

        filtered = filter_excluded(candidates, topic.excluded_keywords)
        deduped = dedupe_candidates(filtered)
        ranked = rank(deduped, context)[: plan.max_candidates]

        # Body extraction is the expensive part; only do it for the top slice.
        head = ranked[:fetch_top_n]
        if head:
            with ThreadPoolExecutor(max_workers=min(len(head), 6)) as pool:
                futures = {pool.submit(extractor.fetch, c.url): c for c in head}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        candidate.body_text = future.result()
                    except Exception:
                        candidate.body_text = ""

        # Re-rank now that we know which articles actually yielded text.
        return rank(ranked, context)

    def _persist_articles(
        self, candidates: list[Candidate], plan: ModulePlan, topic: TopicPlan
    ) -> list[int]:
        """Store candidates as :class:`RawArticle` rows, de-duplicated by URL."""
        article_ids: list[int] = []
        with session_scope() as session:
            for candidate in candidates:
                if not candidate.url:
                    continue
                payload = {
                    "url": candidate.url,
                    "canonical_url": canonicalize_url(candidate.url),
                    "url_hash": url_hash(candidate.url),
                    "content_hash": content_hash(candidate.body_text),
                    "title": candidate.title,
                    "source": (candidate.source or candidate.domain)[:300],
                    "domain": (candidate.domain or "")[:200],
                    "published_at": candidate.published_at,
                    "published_raw": (candidate.published_raw or "")[:120],
                    "collected_at": utcnow(),
                    "last_seen_at": utcnow(),
                    "snippet": candidate.snippet,
                    "body_text": candidate.body_text,
                    "language": candidate.language,
                    "trust_score": candidate.trust_score,
                    "collector": candidate.collector,
                    "topic_id": topic.topic_id,
                    "module_id": plan.module_id,
                    "run_id": self.run_id,
                    "metadata_json": candidate.metadata or None,
                }
                try:
                    article, _created = articles_repo.upsert(session, payload)
                    article_ids.append(article.id)
                except Exception as exc:
                    logger.debug("Skipping article %s: %s", candidate.url[:80], exc)
        return article_ids

    # -- analysis ------------------------------------------------------------

    def _analyze_topics(
        self,
        plan: ModulePlan,
        topic_articles: dict[int, list[int]],
        report_date: dt.date,
        analyzer: IntelligenceAnalyzer,
    ) -> list[CandidateIntelligence]:
        """Run per-topic model analysis; a failing topic is skipped, not fatal."""
        results: list[CandidateIntelligence] = []
        topics_by_id = {t.topic_id: t for t in plan.topics}

        for topic_id, article_ids in topic_articles.items():
            topic = topics_by_id.get(topic_id)
            if topic is None or not article_ids:
                continue
            self._check_cancel()

            with session_scope() as session:
                rows = [session.get(RawArticle, aid) for aid in article_ids]
                rows = [r for r in rows if r is not None]
                rows.sort(key=lambda r: (r.trust_score or 0), reverse=True)
                evidence = build_evidence(rows[: plan.max_candidates])

            if not evidence:
                continue

            instructions = "\n".join(
                part for part in (topic.description, topic.analysis_prompt, plan.analysis_prompt)
                if part and part.strip()
            )
            try:
                items, _raw = analyzer.analyze_topic(
                    topic_name=topic.name,
                    module_name=plan.name,
                    report_date=report_date.isoformat(),
                    evidence=evidence,
                    max_items=plan.max_report_items,
                    extra_instructions=instructions,
                )
            except LLMError as exc:
                message = redact(str(exc))[:300]
                self.errors.append(f"{plan.name}/{topic.name}: {message}")
                self.log(
                    f"  {topic.name} analysis failed: {message}",
                    level="error",
                    module_key=plan.key,
                )
                continue

            for item in items:
                item.topic_id = topic.topic_id
                item.topic_name = topic.name
                item.module_id = plan.module_id
            results.extend(items)
            self._flush_usage()
            self.log(f"  {topic.name}: {len(items)} candidate intelligence", module_key=plan.key)

        results.sort(key=lambda i: (i.importance, -len(i.article_ids)))
        return results[: plan.max_report_items * 2]

    # -- event matching ------------------------------------------------------

    def _match_and_record(
        self,
        plan: ModulePlan,
        candidates: list[CandidateIntelligence],
        report_date: dt.date,
        matcher: EventMatcher,
        match_lookback: int,
    ) -> list[dict]:
        """Attach each candidate to a new or existing event and store observations."""
        items: list[dict] = []
        for candidate in candidates:
            self._check_cancel()
            try:
                item = self._record_one(plan, candidate, report_date, matcher, match_lookback)
                self._flush_deferred()
                if item:
                    items.append(item)
            except Exception as exc:
                self._flush_deferred()
                message = redact(str(exc))[:300]
                self.errors.append(f"{plan.name} event matching: {message}")
                self.log(
                    f"  event matching failed for '{candidate.title[:40]}': {message}",
                    level="error",
                    module_key=plan.key,
                )
        items.sort(key=lambda i: i.get("importance", 3))
        return items[: plan.max_report_items]

    def _record_one(
        self,
        plan: ModulePlan,
        candidate: CandidateIntelligence,
        report_date: dt.date,
        matcher: EventMatcher,
        match_lookback: int,
    ) -> Optional[dict]:
        with session_scope() as session:
            existing = events_repo.candidate_events(
                session, plan.module_id, candidate.topic_id, match_lookback
            )
            decision = matcher.match(candidate, existing)

            event = (
                session.get(IntelligenceEvent, decision.event_id)
                if decision.is_match
                else None
            )

            event_state = "updated"
            if event is None:
                base_key = slugify_event_key(plan.key, candidate.title)
                key = unique_event_key(
                    base_key, lambda k: events_repo.get_by_key(session, k) is not None
                )
                event = events_repo.create_event(
                    session,
                    event_key=key,
                    title=candidate.title,
                    summary=candidate.fact_summary[:1000],
                    module_id=plan.module_id,
                    topic_id=candidate.topic_id,
                    first_seen_at=utcnow(),
                    last_seen_at=utcnow(),
                    first_seen_date=report_date,
                    last_seen_date=report_date,
                    observation_count=0,
                )
                event_state = "new"
                self._defer_log(
                    f"  NEW event: {candidate.title[:60]} ({decision.method})",
                    module_key=plan.key,
                )
            else:
                self._defer_log(
                    f"  UPDATED event #{event.id}: {candidate.title[:50]} "
                    f"({decision.method}, confidence {decision.confidence:.2f})",
                    module_key=plan.key,
                )

            observation = events_repo.add_observation(
                session,
                event_id=event.id,
                run_id=self.run_id,
                observation_date=report_date,
                title=candidate.title,
                tag=candidate.tag,
                fact_summary=candidate.fact_summary,
                assessment=candidate.assessment,
                importance=candidate.importance,
                confidence=candidate.confidence,
                structured_data_json=candidate.structured_data or None,
                is_correction=candidate.is_correction,
            )
            events_repo.link_sources(session, observation.id, candidate.article_ids)

            event.last_seen_at = utcnow()
            event.last_seen_date = report_date
            event.observation_count = (event.observation_count or 0) + 1
            if event_state == "updated":
                event.summary = candidate.fact_summary[:1000]
            session.flush()

            return {
                "event_id": event.id,
                "observation_id": observation.id,
                "event_state": event_state,
                "tag": candidate.tag,
                "title": candidate.title,
                "fact_summary": candidate.fact_summary,
                "assessment": candidate.assessment,
                "importance": candidate.importance,
                "confidence": candidate.confidence,
            }

    # -- synthesis and publication ------------------------------------------

    def _synthesize(
        self,
        analyzer: IntelligenceAnalyzer,
        sections: list[report_generator.SectionPayload],
        report_date: dt.date,
    ) -> dict:
        compact = [
            {
                "section": section.module_name,
                "status": section.status,
                "summary": section.summary,
                "items": [
                    {
                        "title": item["title"],
                        "fact_summary": item["fact_summary"],
                        "assessment": item["assessment"],
                        "importance": item["importance"],
                        "confidence": item["confidence"],
                    }
                    for item in section.items
                ],
                "metrics": section.metrics,
            }
            for section in sections
        ]
        try:
            return analyzer.synthesize_report(compact, report_date.isoformat())
        except Exception as exc:
            message = redact(str(exc))[:300]
            self.errors.append(f"synthesis: {message}")
            self.log(f"Synthesis failed: {message}", level="error")
            return {"headline": {}, "trends": [], "metrics": []}

    def _publish(
        self,
        sections: list[report_generator.SectionPayload],
        overview: dict,
        report_date: dt.date,
        model: str,
    ) -> None:
        with session_scope() as session:
            report = report_generator.persist_report(
                session,
                report_date=report_date,
                run_id=self.run_id,
                sections=sections,
                overview=overview,
                model=model,
                coverage=self._coverage_payload(),
            )
            report_id = report.id

        written: Optional[tuple] = None
        with session_scope() as session:
            stored = reports_repo.get_report(session, report_id)
            if stored is not None:
                written = report_generator.export_report(session, stored)

        if written is not None:
            html_path, json_path = written
            self.log(f"Report written: {html_path.name} and {json_path.name}")

    # -- network and source health -------------------------------------------

    def _prepare_network(
        self, registry: CollectorRegistry, config, precheck: bool
    ) -> None:
        """Choose a collection strategy before spending a run discovering it.

        In auto mode a handful of cheap probes decide which sources are worth
        asking. Without this step, an unreachable Google News costs one full
        connect timeout *per query* before the circuit breaker notices.
        """
        mode_label = network_service.MODE_LABELS.get(
            config.network_mode, config.network_mode
        )
        self.log(f"网络模式：{mode_label} · 连接方式：{config.transport.describe()}")

        probes = None
        if precheck and config.network_mode == network_service.MODE_AUTO:
            self._set_stage("checking_sources")
            try:
                with session_scope() as session:
                    probes = network_service.probe_sources(session, config)
            except Exception as exc:  # a failed probe must not fail the run
                self.log(f"连接检测未完成：{redact(str(exc))[:160]}", level="warn")
                probes = None
            else:
                for probe in probes:
                    self.log(
                        f"  {probe.display_name}：{probe.as_dict()['status_label']}"
                        + (f" · {probe.latency_ms} ms" if probe.reachable else "")
                        + (f" · {probe.detail}" if probe.detail and not probe.reachable else "")
                    )

        for note in network_service.apply_mode_policy(registry, config, probes):
            self.log(note)

        for collector in registry.collectors:
            self.health.register(
                collector.name,
                display_name=collector.info.display_name,
                disabled=collector.name in registry.disabled,
            )
        self._persist_health()

    def _persist_health(self) -> None:
        """Write current source counters so the live run view can read them.

        Never raises: source accounting is reporting, not the product.
        """
        try:
            payloads = self.health.as_dicts()
        except Exception:  # pragma: no cover - defensive
            return
        if not payloads:
            return
        try:
            with session_scope() as session:
                sources_repo.replace_stats(session, self.run_id, payloads)
        except Exception:
            logger.debug("Could not persist source health", exc_info=True)

    def _coverage_payload(self) -> dict:
        """What the report records about this run's source coverage."""
        sources = self.health.as_dicts()
        impaired = [
            s for s in sources if s.get("state") in HealthState.IMPAIRED
        ]
        return {
            "degraded": bool(impaired) or bool(self.collection_failures),
            "recorded": True,
            "sources": sources,
            "notes": self.health.summary_lines(),
            "failed_topics": self.collection_failures[:20],
        }

    def _final_status(self) -> str:
        """``completed`` only when coverage was actually complete.

        A run in which a source never answered is ``completed_with_errors`` even
        if every module finished: calling it ``completed`` would tell the user
        the absence of news was a finding.
        """
        if self.errors:
            return RunStatus.COMPLETED_WITH_ERRORS
        if self.collection_failures:
            return RunStatus.COMPLETED_WITH_ERRORS
        if self.health.has_broken_source():
            return RunStatus.COMPLETED_WITH_ERRORS
        return RunStatus.COMPLETED

    # -- bookkeeping ---------------------------------------------------------

    @staticmethod
    def _model_label(client) -> str:
        """What to stamp on the report as the model that produced it.

        With task routing a single run may use several models, so the label
        names the synthesis engine and flags when others took part.
        """
        try:
            routing = client.describe_routing()
        except Exception:  # pragma: no cover - labelling must not fail a run
            return ""
        if not routing:
            return ""
        synthesis = routing.get("synthesis") or next(iter(routing.values()))
        label = f"{synthesis.display_name} / {synthesis.model}"
        distinct = {(r.provider_id, r.model) for r in routing.values()}
        if len(distinct) > 1:
            label += f" (+{len(distinct) - 1} more)"
        return label

    def _module_run_id(self, module_id: int) -> Optional[int]:
        with session_scope() as session:
            for module_run in runs_repo.module_runs_for(session, self.run_id):
                if module_run.module_id == module_id:
                    return module_run.id
        return None

    def _update_module_run(self, module_run_id: Optional[int], **fields) -> None:
        if module_run_id is None:
            return
        with session_scope() as session:
            module_run = runs_repo.get_module_run(session, module_run_id)
            if module_run is None:
                return
            for key, value in fields.items():
                setattr(module_run, key, value)

    def _record_usage(self, fields: dict) -> None:
        """Buffer one LLM call record (provider, model, tokens, latency).

        Buffered rather than written immediately because the client is often
        invoked from inside a transaction; writing here would nest a second
        session inside it. :meth:`_flush_usage` persists the buffer once the
        surrounding transaction has closed.
        """
        self._usage_buffer.append(fields)

    def _flush_usage(self) -> None:
        """Persist buffered LLM usage. Accounting failures never break a run."""
        pending, self._usage_buffer = self._usage_buffer, []
        if not pending:
            return
        try:
            with session_scope() as session:
                for fields in pending:
                    runs_repo.add_usage(session, run_id=self.run_id, **fields)
        except Exception:
            logger.debug("Could not record LLM usage", exc_info=True)

    def _finish(
        self, status: str, report_date: Optional[dt.date], error: str = ""
    ) -> str:
        self._flush_deferred()
        self._persist_health()
        with session_scope() as session:
            run = runs_repo.get_run(session, self.run_id)
            if run is None:
                return status
            run.status = status
            run.finished_at = utcnow()
            run.stage = status
            if error:
                run.error_message = error[:2000]
            messages = list(self.errors)
            if self.collection_failures:
                messages.append(
                    "数据源访问失败：" + "、".join(self.collection_failures[:6])
                )
            messages.extend(self.health.summary_lines())
            if messages and not error:
                run.error_message = "; ".join(messages)[:2000]

            run.total_articles = articles_repo.count_for_run(session, self.run_id)

            observations = list(
                session.scalars(
                    select(EventObservation).where(EventObservation.run_id == self.run_id)
                )
            )
            touched_event_ids = {o.event_id for o in observations}
            run.total_events = len(touched_event_ids)
            # An event whose only observation is this run's is new today.
            run.total_new_events = session.scalar(
                select(func.count())
                .select_from(IntelligenceEvent)
                .where(
                    IntelligenceEvent.id.in_(touched_event_ids or {-1}),
                    IntelligenceEvent.observation_count == 1,
                )
            ) or 0
            run.total_updated_events = max(0, run.total_events - run.total_new_events)

            report = reports_repo.get_by_run(session, self.run_id)
            run.total_report_items = report.item_count if report else 0

            module_runs = runs_repo.module_runs_for(session, self.run_id)
            run.total_candidates = sum(m.candidate_count for m in module_runs)

        self.log(f"Finished with status: {status}")
        return status


def run_pipeline(run_id: int, cancel_check: Optional[Callable[[], bool]] = None) -> str:
    """Convenience entry point used by the run manager and the scheduler."""
    return MonitoringPipeline(run_id, cancel_check=cancel_check).execute()


def create_run(session: Session, trigger_type: str = "manual", report_date: Optional[dt.date] = None):
    """Create a queued run for today (local date)."""
    return runs_repo.create_run(
        session, report_date=report_date or local_today(), trigger_type=trigger_type
    )
