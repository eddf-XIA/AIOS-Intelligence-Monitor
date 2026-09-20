"""Research performed by AIOS's own collectors, analysed by the user's model.

This is the ``BACKEND_LOCAL`` research backend - 本地检索研究（高级）. The
retrieval is done by the Classic collection engine, which really does fetch
live documents, and the configured model does the reading, cross-checking and
writing. For a user whose only provider is a chat endpoint it is the only way
to get a genuinely sourced report at all, which is why it exists and why it is
not going anywhere.

**It is not the Simple-mode architecture, and it is never a fallback.**

Simple mode means a remote Research Agent that searches and reads on the
vendor's side. This module runs on the user's machine, so it re-inherits every
anti-scraping, rate-limit, VPN and network-routing problem that Simple mode was
created to remove. Nothing here runs unless the user explicitly selected
本地检索研究（高级）: :func:`aios.services.research.catalog.default_agent_for_provider`
returns ``None`` rather than reaching for this module, so a user who chose a
remote agent and hit an error is told what failed instead of being quietly
handed a locally-collected report attributed to their remote agent.

The Classic engine itself is untouched: this module only calls it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import requests

from ...database import session_scope
from ...schemas.research import (
    COVERAGE_FAILED,
    COVERAGE_PARTIAL,
    ResearchCoverage,
    ResearchResult,
    ResearchResultError,
    validate_research_payload,
)
from ..article_extractor import ArticleExtractor
from ..collection_planner import (
    CollectionPlanner,
    TopicCollectionStatus,
    shared_cache,
)
from ..collector import Candidate
from ..deduplicator import dedupe_candidates, filter_excluded
from ..keyring_service import redact
from ..llm import LLMError
from ..llm.base import InvalidJSONResponse
from ..llm.service import LLMService, build_llm_service
from ..source_health import SourceHealthTracker
from ..source_scoring import ScoringContext, rank
from .base import (
    ProgressCallback,
    ResearchAgentSpec,
    ResearchError,
    ResearchRequest,
    ResearchService,
    STAGE_ORGANIZING,
    STAGE_READING,
    STAGE_REPORTING,
    STAGE_SEARCHING,
    STAGE_UNDERSTANDING,
)
from .prompts import (
    EVIDENCE_SYSTEM_PROMPT,
    REPAIR_INSTRUCTION,
    build_brief_block,
    build_evidence_prompt,
)

logger = logging.getLogger(__name__)

#: How many search expressions the brief is turned into. Every one is a real
#: network request against a public API, so this stays small on purpose - the
#: same budget discipline the Classic config generator applies.
MAX_QUERIES = 6
#: Candidates kept for analysis after ranking.
MAX_EVIDENCE = 28
#: Bodies are fetched only for the best few; extraction is the expensive part.
FETCH_BODY_TOP_N = 12
#: Characters of article text handed to the model per source.
EVIDENCE_CHARS = 5000

QUERY_SYSTEM_PROMPT = """你是情报检索策略助手。
把用户的研究目标转换成可以直接投给新闻搜索引擎的检索式。

规则：
1. 输出的是关键词组合，不是句子、不是问句。通常 2-6 个词。
2. 每条检索式覆盖不同角度，不要互相重叠。
3. 有国际相关性时，中英文检索式都要有。
4. 使用稳定的概念词（技术名、产品线、标准名、厂商名），
   不要写"最新""近期""2026年"这类会过期的措辞。
5. 最多 %d 条，宁少勿滥。

只输出合法 JSON：{"queries": ["检索式1", "检索式2"]}""" % MAX_QUERIES


class LocalCollectionAgent(ResearchService):
    """Collect with the Classic engine, analyse with the configured model."""

    def __init__(
        self,
        spec: ResearchAgentSpec,
        progress: Optional[ProgressCallback] = None,
        client: Optional[LLMService] = None,
        usage_sink: Optional[Callable[[dict], None]] = None,
        http_session: Optional[requests.Session] = None,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        super().__init__(spec, progress=progress)
        self.agent_id = spec.agent_id
        self._usage_sink = usage_sink
        self.client = client or build_llm_service(usage_sink=usage_sink)
        self._http = http_session or requests.Session()
        self._log = log or (lambda message, level="info": None)
        #: Real per-source counters, so the run's source accounting is not
        #: estimated - exactly as the Classic pipeline does it.
        self.health = SourceHealthTracker()
        self.failed_sources: list[str] = []

    # -- readiness -------------------------------------------------------

    def readiness_error(self) -> str:
        base = super().readiness_error()
        if base:
            return base
        try:
            return self.client.readiness_error()
        except Exception as exc:  # pragma: no cover - defensive
            return str(exc)

    # -- the research pass -----------------------------------------------

    def run(self, request: ResearchRequest) -> ResearchResult:
        error = self.readiness_error()
        if error:
            raise ResearchError(error, retryable=False)

        self.report_progress(STAGE_UNDERSTANDING, "把研究目标转换为检索方向")
        queries = self._plan_queries(request)
        if not queries:
            raise ResearchError(
                "无法从研究主题生成检索方向，请把主题描述得更具体一些。",
                retryable=False,
            )
        self._log(f"检索方向：{'、'.join(queries)}", "info")

        self.report_progress(STAGE_SEARCHING, f"检索 {len(queries)} 个方向")
        candidates, collection_status = self._collect(request, queries)

        if collection_status == TopicCollectionStatus.FAILED:
            # The sources could not be reached. We do not know whether there
            # was news, and must not imply that there was none.
            raise ResearchError(
                "公开来源暂时无法访问，本次研究没有完成。"
                + (f"（{'、'.join(self.failed_sources[:4])}）" if self.failed_sources else "")
            )

        if not candidates:
            # The sources answered and had nothing in the window. That is a
            # real, complete finding - coverage stays honest about which.
            partial = collection_status == TopicCollectionStatus.PARTIAL
            return ResearchResult(
                coverage=ResearchCoverage(
                    status=COVERAGE_PARTIAL if partial else "complete",
                    window_from=_fmt(request.window_from),
                    window_to=_fmt(request.window_to),
                    limitations=(
                        [f"部分来源访问异常：{'、'.join(self.failed_sources[:4])}"]
                        if partial and self.failed_sources
                        else []
                    ),
                    sources_examined=0,
                )
            )

        self.report_progress(STAGE_READING, f"阅读 {len(candidates)} 篇来源")
        evidence = self._build_evidence(candidates)

        self.report_progress(STAGE_ORGANIZING, "整理事件与证据")
        result = self._analyze(request, evidence)

        self.report_progress(STAGE_REPORTING, "撰写报告")
        return self._apply_collection_coverage(result, collection_status, len(evidence))

    # -- stage 1: brief -> queries ---------------------------------------

    def _plan_queries(self, request: ResearchRequest) -> list[str]:
        """Turn the research brief into search expressions.

        Falls back to a deterministic construction from the brief's own nouns
        when the model is unavailable: losing the LLM should cost query
        *quality*, not the ability to research at all.
        """
        user = (
            f"{build_brief_block(request)}\n\n"
            f"请生成最多 {MAX_QUERIES} 条检索式。"
        )
        try:
            response = self.client.complete_json(
                [
                    {"role": "system", "content": QUERY_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                purpose="research_queries",
                max_tokens=800,
            )
        except LLMError as exc:
            logger.info("Query planning unavailable, using the brief directly: %s", exc)
            return self._fallback_queries(request)

        raw = response.data.get("queries")
        queries: list[str] = []
        seen: set[str] = set()
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    entry = entry.get("query") or entry.get("text") or ""
                text = " ".join(str(entry or "").split())[:200]
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                queries.append(text)
                if len(queries) >= MAX_QUERIES:
                    break
        return queries or self._fallback_queries(request)

    @staticmethod
    def _fallback_queries(request: ResearchRequest) -> list[str]:
        """Deterministic queries built from the brief's own named things."""
        queries: list[str] = []
        seen: set[str] = set()

        def add(text: str) -> None:
            cleaned = " ".join(str(text or "").split())[:200]
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                queries.append(cleaned)

        add(request.topic_name)
        for keyword in request.keywords[:MAX_QUERIES]:
            add(keyword)
        for focus in request.focus_areas[:2]:
            add(f"{request.topic_name} {focus}")
        return queries[:MAX_QUERIES]

    # -- stage 2: collection ---------------------------------------------

    def _collect(
        self, request: ResearchRequest, queries: list[str]
    ) -> tuple[list[Candidate], str]:
        """Run the Classic collection engine over the planned queries."""
        from .. import network_service, settings_service

        with session_scope() as session:
            config = network_service.collection_settings(session)
            feeds = network_service.feed_specs(session)
            workers = max(1, settings_service.get_int(session, "collect_workers", 6))
            precheck = settings_service.get_bool(session, "network_precheck_enabled", True)

        registry = network_service.build_registry(config, feeds=feeds)

        probes = None
        if precheck and config.network_mode == network_service.MODE_AUTO:
            try:
                with session_scope() as session:
                    probes = network_service.probe_sources(session, config)
            except Exception as exc:  # a failed probe must not fail the run
                self._log(f"连接检测未完成：{redact(str(exc))[:160]}", "warn")
                probes = None

        for note in network_service.apply_mode_policy(registry, config, probes):
            self._log(note, "info")

        for collector in registry.collectors:
            self.health.register(
                collector.name,
                display_name=collector.info.display_name,
                disabled=collector.name in registry.disabled,
            )

        planner = CollectionPlanner(
            registry=registry,
            health=self.health,
            cache=shared_cache(config.cache_ttl_minutes * 60),
            network_mode=config.network_mode,
            min_candidates=config.min_candidates,
            log=self._log,
        )

        start, end = self._window(request)
        try:
            outcome = planner.collect_topic(
                queries=queries,
                start=start,
                end=end,
                limit=max(8, MAX_EVIDENCE // 2),
                topic_name=request.topic_name,
                workers=workers,
            )
        except Exception as exc:  # pragma: no cover - planner isolates failures
            self._log(f"采集异常：{redact(str(exc))[:200]}", "error")
            return [], TopicCollectionStatus.FAILED

        self.failed_sources = list(outcome.failed_sources or [])
        prepared = self._prepare(outcome.candidates, request)
        return prepared, outcome.status

    @staticmethod
    def _window(request: ResearchRequest) -> tuple[dt.datetime, dt.datetime]:
        from ...timeutil import utcnow

        end = request.window_to or utcnow()
        start = request.window_from or (
            end - dt.timedelta(hours=max(1, request.window_hours or 72))
        )
        return start, end

    def _prepare(
        self, candidates: list[Candidate], request: ResearchRequest
    ) -> list[Candidate]:
        """Exclude, de-duplicate, rank, then fetch bodies for the best few."""
        if not candidates:
            return []

        context = ScoringContext.from_rows([])
        filtered = filter_excluded(candidates, request.exclusions)
        ranked = rank(dedupe_candidates(filtered), context)[:MAX_EVIDENCE]

        head = ranked[:FETCH_BODY_TOP_N]
        if head:
            extractor = ArticleExtractor(
                max_chars=EVIDENCE_CHARS, timeout=20, session=self._http
            )
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

    # -- stage 3: analysis ------------------------------------------------

    @staticmethod
    def _build_evidence(candidates: list[Candidate]) -> list[dict]:
        """Number the evidence so the model can cite it by id."""
        evidence: list[dict] = []
        for index, candidate in enumerate(candidates):
            text = (candidate.body_text or candidate.snippet or "")[:EVIDENCE_CHARS]
            evidence.append(
                {
                    "id": index,
                    "title": candidate.title or "",
                    "publisher": candidate.source or candidate.domain or "",
                    "url": candidate.url or "",
                    "published_at": candidate.published_raw
                    or (
                        candidate.published_at.isoformat()
                        if candidate.published_at
                        else ""
                    ),
                    "text": text,
                }
            )
        return evidence

    def _analyze(self, request: ResearchRequest, evidence: list[dict]) -> ResearchResult:
        """Ask the configured model for a ResearchResult over fixed evidence."""
        messages = [
            {"role": "system", "content": EVIDENCE_SYSTEM_PROMPT},
            {"role": "user", "content": build_evidence_prompt(request, evidence)},
        ]
        last_error: Optional[Exception] = None

        for attempt in (1, 2):
            try:
                response = self.client.complete_json(
                    messages, purpose="research", max_tokens=8000, retries=2
                )
                result = validate_research_payload(response.data)
                return self._attach_sources(result, evidence)
            except (ResearchResultError, InvalidJSONResponse) as exc:
                last_error = exc
                logger.info("Research analysis payload unusable (attempt %s): %s", attempt, exc)
                messages = messages[:2] + [
                    {"role": "user", "content": REPAIR_INSTRUCTION}
                ]
            except LLMError as exc:
                raise ResearchError(f"分析模型调用失败：{redact(str(exc))}") from exc

        raise ResearchError(
            f"分析结果无法解析：{redact(str(last_error) if last_error else '未知错误')}"
        )

    @staticmethod
    def _attach_sources(result: ResearchResult, evidence: list[dict]) -> ResearchResult:
        """Guarantee the result's sources are the documents we actually fetched.

        The model is asked to echo the source list back, but the authoritative
        record is the evidence AIOS collected - so any source the model cited is
        replaced with the real fetched document at that id. A model that
        paraphrased a URL cannot thereby break the evidence chain.
        """
        by_id = {int(item["id"]): item for item in evidence}
        cited: set[int] = set()
        for event in result.events:
            cited.update(event.source_ids)
        for section in result.report.sections:
            for item in section.summary_items:
                cited.update(item.evidence_ids)
        for row in result.report.market_snapshot:
            cited.update(row.evidence_ids)
        for trend in result.report.trend_analysis:
            cited.update(trend.evidence_ids)

        from ...schemas.research import ResearchSource

        sources: list[ResearchSource] = []
        for source_id in sorted(cited):
            record = by_id.get(source_id)
            if record is None:
                continue
            try:
                sources.append(
                    ResearchSource(
                        source_id=source_id,
                        title=record.get("title", ""),
                        publisher=record.get("publisher", ""),
                        url=record["url"],
                        published_at=record.get("published_at", ""),
                    )
                )
            except Exception:
                continue

        coverage = result.coverage.model_copy(
            update={"sources_examined": len(evidence)}
        )
        return result.model_copy(update={"sources": sources, "coverage": coverage})

    def _apply_collection_coverage(
        self, result: ResearchResult, collection_status: str, examined: int
    ) -> ResearchResult:
        """Let a degraded collection downgrade the model's coverage claim.

        The model only sees the evidence it was given; it has no way to know a
        collector was down. AIOS does, so the truthful verdict is the worse of
        the two - never the better.
        """
        coverage = result.coverage
        limitations = list(coverage.limitations)
        status = coverage.status

        if collection_status == TopicCollectionStatus.PARTIAL:
            status = COVERAGE_PARTIAL if status != COVERAGE_FAILED else status
            if self.failed_sources:
                note = f"部分来源访问异常：{'、'.join(self.failed_sources[:4])}"
                if note not in limitations:
                    limitations.append(note)
        if self.health.has_broken_source() and status == "complete":
            status = COVERAGE_PARTIAL
            for line in self.health.summary_lines()[:3]:
                if line not in limitations:
                    limitations.append(line)

        return result.model_copy(
            update={
                "coverage": coverage.model_copy(
                    update={
                        "status": status,
                        "limitations": limitations[:8],
                        "sources_examined": examined or coverage.sources_examined,
                    }
                )
            }
        )


def _fmt(value: Optional[dt.datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""
