"""Decides *which* source is asked *which* query, and how often.

The naive shape - every query against every collector - is what produced a run
with fifty-one queries, dozens of 429s and a wall of 25-second Google News
timeouts. This layer sits between the pipeline and the collectors and applies
four rules:

1. **Staged, not fanned out.** Collectors are tried in network-mode order and
   the chain stops as soon as one returns enough candidates. A source that
   would only have added duplicates is never asked.
2. **Deduplicated.** Two topics asking the same normalised query inside one run
   produce one request, not two.
3. **Cached briefly.** Re-running monitoring twice in ten minutes must not
   double the load on a public API.
4. **Truthful.** The outcome distinguishes "asked and found nothing" from
   "could not ask". :class:`TopicCollectionOutcome` carries which one happened.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..schemas.config_generation import normalize_query
from .collector import (
    Candidate,
    CollectorRegistry,
    CollectorResult,
    CollectorStatus,
    SourceCollector,
)
from .source_health import SourceHealthTracker

logger = logging.getLogger(__name__)


class TopicCollectionStatus:
    """What collection for one topic actually achieved."""

    #: At least one source answered and returned candidates.
    OK = "ok"
    #: Every source that was asked answered successfully, all with zero results.
    NO_CANDIDATES = "no_candidates"
    #: Some sources answered, others failed. Usable evidence was still collected.
    PARTIAL = "partial_collection"
    #: Every source that was asked failed. We do not know whether news exists.
    FAILED = "collection_failed"
    #: Nothing was asked at all (every source disabled or unconfigured).
    NOT_ATTEMPTED = "not_attempted"


TOPIC_STATUS_LABELS = {
    TopicCollectionStatus.OK: "采集正常",
    TopicCollectionStatus.NO_CANDIDATES: "无新增结果",
    TopicCollectionStatus.PARTIAL: "部分数据源异常",
    TopicCollectionStatus.FAILED: "采集失败",
    TopicCollectionStatus.NOT_ATTEMPTED: "未执行采集",
}


@dataclass
class QueryOutcome:
    """Every collector attempt made for one query."""

    query: str
    results: list[CollectorResult] = field(default_factory=list)

    @property
    def candidates(self) -> list[Candidate]:
        merged: list[Candidate] = []
        for result in self.results:
            merged.extend(result.candidates)
        return merged

    @property
    def succeeded(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def failed_sources(self) -> list[str]:
        return [r.collector for r in self.results if r.failed]


@dataclass
class TopicCollectionOutcome:
    """The result of collecting one topic, with honest provenance."""

    topic_name: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    queries: list[QueryOutcome] = field(default_factory=list)
    status: str = TopicCollectionStatus.NOT_ATTEMPTED
    failed_sources: list[str] = field(default_factory=list)
    succeeded_sources: list[str] = field(default_factory=list)
    requests_made: int = 0
    cache_hits: int = 0

    @property
    def label(self) -> str:
        return TOPIC_STATUS_LABELS.get(self.status, self.status)

    @property
    def collection_failed(self) -> bool:
        return self.status == TopicCollectionStatus.FAILED

    @property
    def degraded(self) -> bool:
        return self.status in (
            TopicCollectionStatus.PARTIAL,
            TopicCollectionStatus.FAILED,
        )

    def describe(self) -> str:
        """One line for the run log."""
        if self.status == TopicCollectionStatus.FAILED:
            sources = "、".join(self.failed_sources) or "全部数据源"
            return f"采集失败（{sources}）"
        if self.status == TopicCollectionStatus.PARTIAL:
            return (
                f"{len(self.candidates)} 条候选"
                f"（{'、'.join(self.failed_sources)} 异常）"
            )
        if self.status == TopicCollectionStatus.NO_CANDIDATES:
            return "数据源正常，本期无匹配结果"
        if self.status == TopicCollectionStatus.NOT_ATTEMPTED:
            return "没有可用的数据源"
        return f"{len(self.candidates)} 条候选"


# --- short-term cache -------------------------------------------------------

@dataclass
class _CacheEntry:
    result: CollectorResult
    expires_at: float


class QueryCache:
    """A small TTL cache keyed by what actually determines a result.

    The key includes the time window, so a cached entry can never leak
    yesterday's window into today's report; the TTL is capped in minutes, so a
    multi-day report cannot be assembled from stale responses. It exists to stop
    a user who clicks "运行监测" three times in five minutes from tripling the
    load on a free public API.
    """

    MAX_TTL_SECONDS = 60 * 60

    def __init__(self, ttl_seconds: float = 1800.0, max_entries: int = 512) -> None:
        self.ttl = max(0.0, min(float(ttl_seconds), self.MAX_TTL_SECONDS))
        self.max_entries = max(16, int(max_entries))
        self._lock = threading.Lock()
        self._entries: dict[tuple, _CacheEntry] = {}

    @staticmethod
    def key(
        collector: str, query: str, start: dt.datetime, end: dt.datetime, limit: int, locale: str = ""
    ) -> tuple:
        # Windows are bucketed to the hour: two runs minutes apart ask for
        # slightly different `end` timestamps but want the same answer.
        return (
            collector,
            normalize_query(query),
            start.strftime("%Y%m%d%H"),
            end.strftime("%Y%m%d%H"),
            min(max(limit, 1), 250),
            locale,
        )

    def get(self, key: tuple) -> Optional[CollectorResult]:
        if self.ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            cached = entry.result
        return CollectorResult(
            collector=cached.collector,
            status=cached.status,
            candidates=list(cached.candidates),
            error=cached.error,
            latency_ms=cached.latency_ms,
            attempts=cached.attempts,
            from_cache=True,
        )

    def put(self, key: tuple, result: CollectorResult) -> None:
        # Only successful answers are cached. Caching a timeout would hide a
        # network that has since come back.
        if self.ttl <= 0 or not result.ok:
            return
        with self._lock:
            if len(self._entries) >= self.max_entries:
                oldest = min(self._entries.items(), key=lambda kv: kv[1].expires_at)[0]
                self._entries.pop(oldest, None)
            self._entries[key] = _CacheEntry(
                result=result, expires_at=time.monotonic() + self.ttl
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:  # pragma: no cover - diagnostics only
        with self._lock:
            return len(self._entries)


#: Process-wide cache shared across runs, so two manual runs minutes apart do
#: not hit the same endpoints twice. Replaced per-test via the constructor.
_shared_cache: Optional[QueryCache] = None
_shared_cache_lock = threading.Lock()


def shared_cache(ttl_seconds: Optional[float] = None) -> QueryCache:
    """The process-wide query cache, created on first use."""
    global _shared_cache
    with _shared_cache_lock:
        if _shared_cache is None:
            _shared_cache = QueryCache(ttl_seconds if ttl_seconds is not None else 1800.0)
        elif ttl_seconds is not None and ttl_seconds != _shared_cache.ttl:
            _shared_cache.ttl = max(0.0, min(float(ttl_seconds), QueryCache.MAX_TTL_SECONDS))
        return _shared_cache


def reset_shared_cache() -> None:
    """Drop the process-wide cache (used by tests and by settings changes)."""
    global _shared_cache
    with _shared_cache_lock:
        _shared_cache = None


# --- the planner ------------------------------------------------------------

class CollectionPlanner:
    """Runs a topic's queries against the right sources, once each."""

    def __init__(
        self,
        registry: CollectorRegistry,
        health: Optional[SourceHealthTracker] = None,
        cache: Optional[QueryCache] = None,
        network_mode: str = "auto",
        min_candidates: int = 1,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.registry = registry
        self.health = health or SourceHealthTracker()
        self.cache = cache if cache is not None else shared_cache()
        self.network_mode = network_mode
        #: How many candidates count as "enough" before fallbacks are skipped.
        self.min_candidates = max(1, int(min_candidates))
        self._log = log or (lambda message, level="info": None)
        self._lock = threading.Lock()
        #: Normalised query -> outcome, so one run asks each query once.
        self._seen: dict[tuple, QueryOutcome] = {}

        for collector in self.registry.collectors:
            self.health.register(
                collector.name,
                display_name=collector.info.display_name,
                disabled=collector.name in self.registry.disabled,
            )

    # -- one query -----------------------------------------------------------

    def collect_query(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int
    ) -> QueryOutcome:
        """Staged collection for one query, deduplicated within the run."""
        dedupe_key = (normalize_query(query), start.strftime("%Y%m%d%H"), end.strftime("%Y%m%d%H"))
        with self._lock:
            existing = self._seen.get(dedupe_key)
        if existing is not None:
            return existing

        outcome = QueryOutcome(query=query)
        collectors = self.registry.ordered(self.network_mode)
        if not collectors:
            with self._lock:
                self._seen[dedupe_key] = outcome
            return outcome

        found = 0
        for collector in collectors:
            result = self._run_one(collector, query, start, end, limit)
            outcome.results.append(result)
            self.health.record(result)
            found += len(result.candidates)
            if found >= self.min_candidates:
                # Enough evidence; every further request would be waste.
                break

        with self._lock:
            self._seen[dedupe_key] = outcome
        return outcome

    def _run_one(
        self,
        collector: SourceCollector,
        query: str,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> CollectorResult:
        cache_key = QueryCache.key(collector.name, query, start, end, limit)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            result = collector.fetch(query, start, end, limit)
        except Exception as exc:  # a broken source must not kill the run
            from .collector import classify_exception

            status, message = classify_exception(exc)
            logger.warning("Collector %s raised: %s", collector.name, exc)
            return CollectorResult(collector=collector.name, status=status, error=message)

        if result.status == CollectorStatus.CIRCUIT_OPEN:
            self._log(f"  {collector.info.display_name}：已暂停请求（连续失败）", "warn")
        elif result.failed:
            self._log(
                f"  {collector.info.display_name} 请求失败（{result.label}）：{result.error}",
                "warn",
            )

        self.cache.put(cache_key, result)
        return result

    # -- one topic -----------------------------------------------------------

    def collect_topic(
        self,
        queries: list[str],
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        topic_name: str = "",
        workers: int = 4,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TopicCollectionOutcome:
        """Collect every query of a topic and classify the overall outcome."""
        outcome = TopicCollectionOutcome(topic_name=topic_name)
        usable = [q for q in queries if (q or "").strip()]
        if not usable:
            return outcome

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = max(1, min(int(workers), len(usable)))
        query_outcomes: list[QueryOutcome] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.collect_query, query, start, end, limit): query
                for query in usable
            }
            for future in as_completed(futures):
                query = futures[future]
                try:
                    query_outcomes.append(future.result())
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Query %s failed unexpectedly: %s", query[:40], exc)
                    query_outcomes.append(QueryOutcome(query=query))

        outcome.queries = query_outcomes
        merged: list[Candidate] = []
        succeeded: set[str] = set()
        failed: set[str] = set()
        attempted = 0
        cache_hits = 0

        for query_outcome in query_outcomes:
            for result in query_outcome.results:
                if result.from_cache:
                    cache_hits += 1
                if result.ok:
                    succeeded.add(result.collector)
                    if not result.from_cache:
                        attempted += 1
                elif result.failed:
                    failed.add(result.collector)
                    if result.status != CollectorStatus.CIRCUIT_OPEN:
                        attempted += 1
                merged.extend(result.candidates)

        outcome.candidates = merged
        outcome.succeeded_sources = sorted(succeeded)
        outcome.failed_sources = sorted(failed)
        outcome.requests_made = attempted
        outcome.cache_hits = cache_hits
        outcome.status = classify_topic_outcome(succeeded, failed, merged)
        return outcome


def classify_topic_outcome(
    succeeded: set[str], failed: set[str], candidates: list[Candidate]
) -> str:
    """The rule that keeps "no news" separate from "no answer".

    * nothing asked                      -> not_attempted
    * every asked source failed          -> collection_failed
    * some succeeded, some failed        -> partial_collection
    * all succeeded, nothing found       -> no_candidates
    * all succeeded, something found     -> ok
    """
    if not succeeded and not failed:
        return TopicCollectionStatus.NOT_ATTEMPTED
    if not succeeded:
        return TopicCollectionStatus.FAILED
    if failed:
        return TopicCollectionStatus.PARTIAL
    return (
        TopicCollectionStatus.OK if candidates else TopicCollectionStatus.NO_CANDIDATES
    )


def aggregate_module_status(outcomes: list[TopicCollectionOutcome]) -> str:
    """Roll topic outcomes up to a module-level collection status."""
    considered = [o for o in outcomes if o.status != TopicCollectionStatus.NOT_ATTEMPTED]
    if not considered:
        return TopicCollectionStatus.NOT_ATTEMPTED
    if all(o.status == TopicCollectionStatus.FAILED for o in considered):
        return TopicCollectionStatus.FAILED
    if any(o.degraded for o in considered):
        return TopicCollectionStatus.PARTIAL
    if any(o.status == TopicCollectionStatus.OK for o in considered):
        return TopicCollectionStatus.OK
    return TopicCollectionStatus.NO_CANDIDATES
