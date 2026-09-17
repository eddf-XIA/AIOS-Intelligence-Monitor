"""Staged collection, deduplication, caching and honest outcome classification.

The single most important property here: "we searched and found nothing" and
"we could not search" are different results and must never collapse into one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.services.collection_planner import (
    CollectionPlanner,
    QueryCache,
    TopicCollectionStatus,
    aggregate_module_status,
    classify_topic_outcome,
)
from aios.services.collector import (
    Candidate,
    CollectorInfo,
    CollectorRegistry,
    CollectorResult,
    CollectorStatus,
    FeedSpec,
    RSSCollector,
    SourceCollector,
)
from aios.services.source_health import HealthState, SourceHealthTracker, derive_state

START = dt.datetime(2026, 9, 12)
END = dt.datetime(2026, 9, 15, 23, 59)


class ScriptedCollector(SourceCollector):
    """A collector that answers from a script and counts its calls."""

    def __init__(self, name, outcomes, display_name=None):
        super().__init__()
        self.name = name
        self.info = CollectorInfo(collector_id=name, display_name=display_name or name)
        #: status or (status, candidate count), consumed one per call.
        self.outcomes = list(outcomes)
        self.queries: list[str] = []

    def _fetch(self, query, start, end, limit):
        self.queries.append(query)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        status, count = outcome if isinstance(outcome, tuple) else (outcome, 0)
        return CollectorResult(
            collector=self.name,
            status=status,
            candidates=[
                Candidate(title=f"{self.name} {i}", url=f"https://{self.name}.com/{query}/{i}")
                for i in range(count)
            ],
            error="" if status == CollectorStatus.OK else f"{status} from {self.name}",
        )


def planner_for(collectors, **kwargs):
    registry = CollectorRegistry(collectors=collectors, network_mode="auto")
    kwargs.setdefault("cache", QueryCache(ttl_seconds=0))
    return CollectionPlanner(registry=registry, **kwargs)


# --- 19-21. zero results vs failure -----------------------------------------

class TestOutcomeSemantics:
    def test_valid_zero_results_is_not_a_collector_failure(self):
        a = ScriptedCollector("gdelt", [CollectorStatus.OK])
        b = ScriptedCollector("google_news", [CollectorStatus.OK])
        outcome = planner_for([a, b]).collect_topic(["q1"], START, END, 10)

        assert outcome.status == TopicCollectionStatus.NO_CANDIDATES
        assert not outcome.collection_failed
        assert not outcome.degraded
        assert outcome.failed_sources == []
        assert "无匹配结果" in outcome.describe()

    def test_both_collectors_failing_is_collection_failed(self):
        a = ScriptedCollector("gdelt", [CollectorStatus.RATE_LIMITED])
        b = ScriptedCollector("google_news", [CollectorStatus.TIMEOUT])
        outcome = planner_for([a, b]).collect_topic(["q1"], START, END, 10)

        assert outcome.status == TopicCollectionStatus.FAILED
        assert outcome.collection_failed
        assert sorted(outcome.failed_sources) == ["gdelt", "google_news"]
        assert outcome.candidates == []
        assert "采集失败" in outcome.describe()

    def test_one_success_one_failure_is_partial_collection(self):
        a = ScriptedCollector("gdelt", [CollectorStatus.TIMEOUT])
        b = ScriptedCollector("google_news", [(CollectorStatus.OK, 3)])
        outcome = planner_for([a, b]).collect_topic(["q1"], START, END, 10)

        assert outcome.status == TopicCollectionStatus.PARTIAL
        assert outcome.degraded
        assert not outcome.collection_failed
        assert len(outcome.candidates) == 3, "usable results are still used"
        assert outcome.failed_sources == ["gdelt"]
        assert outcome.succeeded_sources == ["google_news"]

    @pytest.mark.parametrize(
        "succeeded,failed,candidates,expected",
        [
            (set(), set(), [], TopicCollectionStatus.NOT_ATTEMPTED),
            (set(), {"gdelt"}, [], TopicCollectionStatus.FAILED),
            ({"gdelt"}, set(), [], TopicCollectionStatus.NO_CANDIDATES),
            ({"gdelt"}, set(), ["x"], TopicCollectionStatus.OK),
            ({"gdelt"}, {"rss"}, ["x"], TopicCollectionStatus.PARTIAL),
            ({"gdelt"}, {"rss"}, [], TopicCollectionStatus.PARTIAL),
        ],
    )
    def test_classification_table(self, succeeded, failed, candidates, expected):
        assert classify_topic_outcome(succeeded, failed, candidates) == expected

    def test_a_circuit_open_source_counts_as_a_failure_not_a_zero_result(self):
        from aios.services.net_policy import CircuitBreaker

        a = ScriptedCollector("gdelt", [CollectorStatus.OK])
        a.breaker = CircuitBreaker(threshold=1, reset_after=300)
        a.breaker.trip()
        b = ScriptedCollector("google_news", [CollectorStatus.OK])

        outcome = planner_for([a, b]).collect_topic(["q1"], START, END, 10)
        statuses = {r.collector: r.status for r in outcome.queries[0].results}
        assert statuses["gdelt"] == CollectorStatus.CIRCUIT_OPEN
        assert outcome.status == TopicCollectionStatus.PARTIAL

    def test_module_status_aggregates_topic_outcomes(self):
        def outcome(status):
            from aios.services.collection_planner import TopicCollectionOutcome

            return TopicCollectionOutcome(status=status)

        assert aggregate_module_status([]) == TopicCollectionStatus.NOT_ATTEMPTED
        assert aggregate_module_status(
            [outcome(TopicCollectionStatus.FAILED), outcome(TopicCollectionStatus.FAILED)]
        ) == TopicCollectionStatus.FAILED
        assert aggregate_module_status(
            [outcome(TopicCollectionStatus.OK), outcome(TopicCollectionStatus.FAILED)]
        ) == TopicCollectionStatus.PARTIAL
        assert aggregate_module_status(
            [outcome(TopicCollectionStatus.OK), outcome(TopicCollectionStatus.NO_CANDIDATES)]
        ) == TopicCollectionStatus.OK
        assert aggregate_module_status(
            [outcome(TopicCollectionStatus.NO_CANDIDATES)]
        ) == TopicCollectionStatus.NO_CANDIDATES


# --- staged collection ------------------------------------------------------

class TestStagedCollection:
    def test_a_satisfied_primary_source_skips_the_fallbacks(self):
        primary = ScriptedCollector("gdelt", [(CollectorStatus.OK, 5)])
        fallback = ScriptedCollector("google_news", [(CollectorStatus.OK, 5)])
        last = ScriptedCollector("rss", [(CollectorStatus.OK, 5)])

        planner_for([primary, fallback, last]).collect_topic(["q1"], START, END, 10)

        assert primary.queries == ["q1"]
        assert fallback.queries == [], "no fallback request was needed"
        assert last.queries == []

    def test_a_failing_primary_falls_back_immediately(self):
        primary = ScriptedCollector("gdelt", [CollectorStatus.TIMEOUT])
        fallback = ScriptedCollector("google_news", [(CollectorStatus.OK, 2)])

        outcome = planner_for([primary, fallback]).collect_topic(["q1"], START, END, 10)

        assert fallback.queries == ["q1"]
        assert len(outcome.candidates) == 2

    def test_an_empty_primary_still_tries_the_fallback(self):
        primary = ScriptedCollector("gdelt", [CollectorStatus.OK])
        fallback = ScriptedCollector("google_news", [(CollectorStatus.OK, 1)])

        outcome = planner_for([primary, fallback]).collect_topic(["q1"], START, END, 10)

        assert fallback.queries == ["q1"], "zero results are worth a second opinion"
        assert outcome.status == TopicCollectionStatus.OK

    def test_request_count_for_a_typical_generated_module(self):
        """3 topics x 2 queries with a healthy primary = 6 requests, not 18."""
        primary = ScriptedCollector("gdelt", [(CollectorStatus.OK, 4)])
        fallback = ScriptedCollector("google_news", [(CollectorStatus.OK, 4)])
        rss = ScriptedCollector("rss", [(CollectorStatus.OK, 4)])
        planner = planner_for([primary, fallback, rss])

        for topic in range(3):
            planner.collect_topic(
                [f"topic{topic} q0", f"topic{topic} q1"], START, END, 10, workers=2
            )

        assert len(primary.queries) == 6
        assert fallback.queries == []
        assert rss.queries == []


# --- 22. deduplication ------------------------------------------------------

class TestDeduplication:
    def test_the_same_query_from_two_topics_is_sent_once(self):
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 2)])
        planner = planner_for([collector])

        planner.collect_topic(["HarmonyOS NEXT"], START, END, 10)
        planner.collect_topic(["harmonyos   next"], START, END, 10)

        assert collector.queries == ["HarmonyOS NEXT"], collector.queries

    def test_deduplication_still_returns_the_results_to_both_topics(self):
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 2)])
        planner = planner_for([collector])

        first = planner.collect_topic(["同一个检索式"], START, END, 10)
        second = planner.collect_topic(["同一个检索式"], START, END, 10)

        assert len(first.candidates) == 2
        assert len(second.candidates) == 2
        assert collector.queries == ["同一个检索式"]

    def test_different_queries_are_not_collapsed(self):
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 1)])
        planner = planner_for([collector])
        planner.collect_topic(["query one", "query two"], START, END, 10, workers=1)
        assert sorted(collector.queries) == ["query one", "query two"]


# --- 23. short-term cache ---------------------------------------------------

class TestQueryCache:
    def test_a_repeat_run_is_served_from_the_cache(self):
        cache = QueryCache(ttl_seconds=900)
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 3)])

        first = planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)
        second = planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)

        assert collector.queries == ["q"], "the second run made no request"
        assert len(first.candidates) == 3
        assert len(second.candidates) == 3
        assert second.cache_hits == 1

    def test_failures_are_never_cached(self):
        cache = QueryCache(ttl_seconds=900)
        collector = ScriptedCollector(
            "gdelt", [CollectorStatus.TIMEOUT, (CollectorStatus.OK, 2)]
        )

        planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)
        second = planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)

        assert len(collector.queries) == 2, "a network that recovered must be retried"
        assert len(second.candidates) == 2

    def test_a_different_window_is_a_different_cache_entry(self):
        cache = QueryCache(ttl_seconds=900)
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 1)])
        other_end = END + dt.timedelta(days=1)

        planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)
        planner_for([collector], cache=cache).collect_topic(["q"], START, other_end, 10)

        assert len(collector.queries) == 2, "yesterday's window must not answer today's"

    def test_the_cache_ttl_is_capped_so_reports_cannot_go_stale(self):
        assert QueryCache(ttl_seconds=60 * 60 * 24).ttl == QueryCache.MAX_TTL_SECONDS
        assert QueryCache.MAX_TTL_SECONDS <= 3600

    def test_a_zero_ttl_disables_caching(self):
        cache = QueryCache(ttl_seconds=0)
        collector = ScriptedCollector("gdelt", [(CollectorStatus.OK, 1)])
        planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)
        planner_for([collector], cache=cache).collect_topic(["q"], START, END, 10)
        assert len(collector.queries) == 2

    def test_expired_entries_are_dropped(self):
        import time

        cache = QueryCache(ttl_seconds=0.01)
        key = QueryCache.key("gdelt", "q", START, END, 10)
        cache.put(key, CollectorResult(collector="gdelt", status=CollectorStatus.OK))
        assert cache.get(key) is not None
        time.sleep(0.02)
        assert cache.get(key) is None


# --- source health ----------------------------------------------------------

class TestSourceHealth:
    def test_counters_come_from_real_events_only(self):
        tracker = SourceHealthTracker()
        tracker.register("gdelt", "GDELT")
        for status, count in (
            (CollectorStatus.OK, 3),
            (CollectorStatus.RATE_LIMITED, 2),
            (CollectorStatus.TIMEOUT, 1),
        ):
            for _ in range(count):
                tracker.record(
                    CollectorResult(collector="gdelt", status=status, latency_ms=100)
                )

        stats = {s.collector: s for s in tracker.snapshot()}["gdelt"]
        assert stats.requests_attempted == 6
        assert stats.requests_succeeded == 3
        assert stats.rate_limited == 2
        assert stats.timeouts == 1
        assert stats.failures == 3
        assert stats.success_rate == pytest.approx(0.5)

    def test_no_attempts_means_no_success_rate_rather_than_a_hundred_percent(self):
        tracker = SourceHealthTracker()
        tracker.register("rss", "RSS / Atom")
        stats = tracker.snapshot()[0]
        assert stats.success_rate is None
        assert stats.avg_latency_ms is None
        assert derive_state(stats) == HealthState.UNKNOWN

    @pytest.mark.parametrize(
        "events,expected",
        [
            ([CollectorStatus.OK], HealthState.HEALTHY),
            ([CollectorStatus.OK, CollectorStatus.TIMEOUT], HealthState.DEGRADED),
            ([CollectorStatus.OK, CollectorStatus.RATE_LIMITED], HealthState.RATE_LIMITED),
            ([CollectorStatus.TIMEOUT, CollectorStatus.TIMEOUT], HealthState.UNREACHABLE),
            ([CollectorStatus.RATE_LIMITED], HealthState.RATE_LIMITED),
        ],
    )
    def test_state_derivation(self, events, expected):
        tracker = SourceHealthTracker()
        for status in events:
            tracker.record(CollectorResult(collector="x", status=status))
        assert tracker.state_of("x") == expected

    def test_summary_lines_are_short_and_human(self):
        tracker = SourceHealthTracker()
        tracker.register("google_news", "Google News")
        tracker.register("gdelt", "GDELT")
        tracker.record(CollectorResult(collector="google_news", status=CollectorStatus.TIMEOUT))
        tracker.record(CollectorResult(collector="gdelt", status=CollectorStatus.OK))
        tracker.record(CollectorResult(collector="gdelt", status=CollectorStatus.RATE_LIMITED))

        lines = tracker.summary_lines()
        assert "Google News：连接失败" in lines
        assert any("GDELT" in line and "429" in line for line in lines)
        assert tracker.is_degraded()
        assert tracker.has_broken_source()

    def test_a_healthy_run_is_not_degraded(self):
        tracker = SourceHealthTracker()
        tracker.record(CollectorResult(collector="gdelt", status=CollectorStatus.OK))
        assert not tracker.is_degraded()
        assert not tracker.has_broken_source()
        assert tracker.summary_lines() == []


# --- RSS collector ----------------------------------------------------------

class TestRSSCollector:
    FEED = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Vendor Blog</title>'
        "<item><title>HarmonyOS 6 正式发布</title><link>https://vendor.com/a</link>"
        "<description>&lt;p&gt;设备数量更新&lt;/p&gt;</description>"
        "<pubDate>Mon, 14 Sep 2026 08:00:00 GMT</pubDate></item>"
        "<item><title>Unrelated cooking post</title><link>https://vendor.com/b</link>"
        "<description>recipe</description>"
        "<pubDate>Mon, 14 Sep 2026 09:00:00 GMT</pubDate></item>"
        "</channel></rss>"
    )
    ATOM = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Project Feed</title>"
        "<entry><title>ROS 2 Jazzy released</title>"
        '<link rel="alternate" href="https://project.org/x"/>'
        "<summary>release notes</summary>"
        "<updated>2026-09-14T10:00:00Z</updated></entry></feed>"
    )

    def _collector(self, body, feeds=None):
        class Session:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)

                class R:
                    status_code = 200
                    content = body.encode("utf-8")
                    text = body
                    headers: dict = {}

                return R()

        session = Session()
        collector = RSSCollector(
            session=session,
            feeds=feeds or [FeedSpec(url="https://vendor.com/feed.xml", title="Vendor Blog")],
        )
        return collector, session

    def test_rss_items_matching_the_query_are_returned(self):
        collector, _ = self._collector(self.FEED)
        result = collector.fetch("HarmonyOS", START, END, 10)
        assert result.ok
        assert [c.title for c in result.candidates] == ["HarmonyOS 6 正式发布"]
        assert result.candidates[0].source == "Vendor Blog"
        assert result.candidates[0].collector == "rss"

    def test_atom_feeds_are_supported(self):
        collector, _ = self._collector(
            self.ATOM, feeds=[FeedSpec(url="https://project.org/atom.xml")]
        )
        result = collector.fetch("ROS 2", START, END, 10)
        assert result.ok
        assert result.candidates[0].url == "https://project.org/x"

    def test_each_feed_is_fetched_once_per_window(self):
        collector, session = self._collector(self.FEED)
        collector.fetch("HarmonyOS", START, END, 10)
        collector.fetch("设备数量", START, END, 10)
        collector.fetch("something else", START, END, 10)
        assert len(session.urls) == 1, session.urls

    def test_no_feeds_is_not_configured_rather_than_a_failure(self):
        collector = RSSCollector(feeds=[])
        result = collector.fetch("q", START, END, 10)
        assert result.status == CollectorStatus.NOT_CONFIGURED
        assert result.skipped
        assert not result.failed

    def test_query_tokens_ignore_boolean_operators(self):
        from aios.services.collector import query_tokens

        tokens = query_tokens('"HarmonyOS NEXT" OR (鸿蒙 AND 设备)')
        assert "or" not in tokens and "and" not in tokens
        assert "harmonyos" in tokens
        assert "鸿蒙" in tokens


class TestFeedQueryMatching:
    """Feed-side filtering: precise enough to be useful, loose enough to be fair.

    Feeds cannot be searched, so relevance is decided here. The bar that matters
    is precision on short Latin terms - every false candidate costs a body fetch
    and analysis tokens downstream.
    """

    def _candidate(self, title, snippet=""):
        return Candidate(title=title, url="https://example.com/a", snippet=snippet)

    def test_a_short_latin_term_does_not_match_inside_a_longer_word(self):
        """The regression: "ai" matched inside "available"."""
        from aios.services.collector import matches_query, query_tokens

        item = self._candidate(
            "ROS 2 Humble Hawksbill - Patch Release 15",
            "This patch release is available for all maintained platforms.",
        )
        assert not matches_query(item, query_tokens("Gemini AI model"))

    @pytest.mark.parametrize(
        "token,text,expected",
        [
            ("ai", "available for download", False),
            ("ai", "our new AI model", True),
            ("ai", "AI-powered assistant", True),
            ("ros", "across the platform", False),
            ("ros", "ROS 2 released", True),
            ("pc", "the pc market", True),
            ("pc", "upcoming", False),
        ],
    )
    def test_latin_terms_match_on_word_boundaries(self, token, text, expected):
        from aios.services.collector import token_matches

        assert token_matches(token, text.lower()) is expected

    def test_cjk_terms_still_match_without_word_boundaries(self):
        """Chinese compounds are written without spaces; substring is correct."""
        from aios.services.collector import token_matches

        assert token_matches("鸿蒙", "华为发布鸿蒙操作系统新版本")
        assert token_matches("操作系统", "华为发布鸿蒙操作系统新版本")
        assert not token_matches("安卓", "华为发布鸿蒙操作系统新版本")

    def test_a_genuine_hit_still_matches(self):
        from aios.services.collector import matches_query, query_tokens

        assert matches_query(
            self._candidate("Gemini 2.0 is our new AI model"),
            query_tokens("Gemini AI model"),
        )
        assert matches_query(
            self._candidate("ROS 2 Humble Hawksbill - Patch Release 15"),
            query_tokens("ROS 2 release"),
        )
        assert matches_query(
            self._candidate("华为发布鸿蒙操作系统新版本"),
            query_tokens("鸿蒙 操作系统 发布"),
        )

    def test_a_long_query_needs_more_than_one_generic_hit(self):
        from aios.services.collector import matches_query, query_tokens

        tokens = query_tokens("国产 AI PC 操作系统 端侧")
        assert len(tokens) >= 4
        assert not matches_query(self._candidate("AI 助手上线"), tokens)
        assert matches_query(self._candidate("联想发布新款 AI PC 产品线"), tokens)

    def test_a_short_query_still_needs_only_one_hit(self):
        from aios.services.collector import matches_query, query_tokens

        assert matches_query(
            self._candidate("HarmonyOS NEXT 正式发布"), query_tokens("HarmonyOS")
        )

    def test_duplicate_query_terms_are_collapsed(self):
        """Repeating a term must not let it satisfy a multi-match threshold."""
        from aios.services.collector import query_tokens

        # Single characters are dropped as noise, so "2" is not a term here.
        assert query_tokens("ROS ros ROS release") == ["ros", "release"]
        assert query_tokens("鸿蒙 鸿蒙 操作系统") == ["鸿蒙", "操作系统"]

    def test_an_empty_query_matches_everything(self):
        from aios.services.collector import matches_query

        assert matches_query(self._candidate("anything"), [])
