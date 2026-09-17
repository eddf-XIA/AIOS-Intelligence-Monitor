"""Rate limiting, backoff, circuit breaking and parse-error classification.

The behaviour under test is what stops the application abusing free public
services: one global pacer per source, ``Retry-After`` respected, a bounded
number of retries, and a circuit that opens rather than generating forty more
predictable 429s.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
import requests

from aios.services.collector import (
    CollectorStatus,
    GDELTCollector,
    GoogleNewsCollector,
    RSSCollector,
    FeedSpec,
    classify_exception,
)
from aios.services.net_policy import (
    CircuitBreaker,
    RateLimiter,
    backoff_delay,
    parse_retry_after,
)

START = dt.datetime(2026, 9, 12)
END = dt.datetime(2026, 9, 15, 23, 59)


class Resp:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        import json

        return json.loads(self.text)


class RecordingSession:
    """Serves a scripted list of responses/exceptions and counts requests."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.times: list[float] = []
        self.lock = threading.Lock()
        self.proxies: dict = {}
        self.trust_env = True

    def get(self, url, **kwargs):
        with self.lock:
            self.calls += 1
            self.times.append(time.monotonic())
            item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method, url, **kwargs):
        return self.get(url, **kwargs)


class NoPauseLimiter(RateLimiter):
    """A limiter that records global pauses instead of enforcing them.

    ``time.sleep`` is faked in these tests, so a real pause would turn the
    limiter's wait loop into a busy spin. Global pausing is asserted directly in
    :meth:`TestGdeltRateLimiting.test_retry_after_pauses_every_worker_not_just_this_one`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pauses: list[float] = []

    def pause_for(self, seconds: float) -> None:
        self.pauses.append(seconds)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff is asserted on, not waited out."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    return slept


# --- 13 & 14. 429 handling --------------------------------------------------

class TestGdeltRateLimiting:
    def test_429_triggers_backoff_and_is_not_reported_as_empty(self, no_real_sleeping):
        http = RecordingSession([Resp(429), Resp(429), Resp(429)])
        collector = GDELTCollector(session=http, limiter=RateLimiter(0, 1))

        result = collector.fetch("humanoid robot OS", START, END, 10)

        assert result.status == CollectorStatus.RATE_LIMITED
        assert result.candidates == []
        assert result.failed and not result.ok
        assert not result.zero_results, "a 429 is not a zero-result search"
        assert result.attempts == 3
        assert len(no_real_sleeping) >= 2, "each retry waited"
        assert all(delay > 0 for delay in no_real_sleeping)

    def test_retry_after_is_respected(self, no_real_sleeping):
        limiter = NoPauseLimiter(0, 1)
        http = RecordingSession([Resp(429, headers={"Retry-After": "7"})])
        collector = GDELTCollector(session=http, limiter=limiter)

        result = collector.fetch("query", START, END, 10)

        assert result.status == CollectorStatus.RATE_LIMITED
        assert "Retry-After 7" in result.error
        # The header decided the wait, not our own backoff curve.
        assert 7.0 in no_real_sleeping, no_real_sleeping
        assert limiter.pauses[0] == 7.0

    def test_retry_after_pauses_every_worker_not_just_this_one(self, no_real_sleeping):
        limiter = RateLimiter(0, 4)
        http = RecordingSession([Resp(429, headers={"Retry-After": "12"})])
        collector = GDELTCollector(session=http, limiter=limiter, max_attempts=1)

        collector.fetch("query", START, END, 10)
        assert limiter._paused_until > time.monotonic() + 10

    @pytest.mark.parametrize(
        "header,expected", [("30", 30.0), ("0", 0.0), ("not-a-number", None), (None, None)]
    )
    def test_retry_after_parsing(self, header, expected):
        assert parse_retry_after(header) == expected

    def test_retry_after_is_capped(self):
        assert parse_retry_after("100000", cap=120) == 120

    def test_backoff_is_bounded_and_jittered(self):
        delays = [backoff_delay(attempt) for attempt in range(1, 8)]
        assert all(0 < d <= 60 for d in delays)
        assert delays[0] < delays[3]
        # Jitter means two calls at the same attempt should not be identical.
        spread = {round(backoff_delay(3), 6) for _ in range(20)}
        assert len(spread) > 1


# --- 15 & 16. circuit breaking ---------------------------------------------

class TestCircuitBreaker:
    def test_repeated_failures_open_the_circuit(self, no_real_sleeping):
        http = RecordingSession([requests.exceptions.ConnectionError("refused")])
        collector = GDELTCollector(
            session=http,
            limiter=RateLimiter(0, 1),
            breaker=CircuitBreaker(threshold=3, reset_after=300, name="gdelt"),
            max_attempts=1,
        )

        for _ in range(3):
            collector.fetch("query", START, END, 10)
        assert collector.breaker.is_open

    def test_an_open_circuit_stops_further_requests(self, no_real_sleeping):
        http = RecordingSession([requests.exceptions.ConnectionError("refused")])
        collector = GDELTCollector(
            session=http,
            limiter=RateLimiter(0, 1),
            breaker=CircuitBreaker(threshold=3, reset_after=300, name="gdelt"),
            max_attempts=1,
        )

        for _ in range(3):
            collector.fetch("query", START, END, 10)
        calls_when_opened = http.calls

        # Forty more queries, as a real run would produce.
        results = [collector.fetch(f"query {i}", START, END, 10) for i in range(40)]

        assert http.calls == calls_when_opened, "no further network traffic"
        assert all(r.status == CollectorStatus.CIRCUIT_OPEN for r in results)
        assert all(r.attempts == 0 for r in results)

    def test_the_circuit_reopens_for_a_probe_after_its_window(self, no_real_sleeping):
        http = RecordingSession([Resp(200, '{"articles": []}')])
        breaker = CircuitBreaker(threshold=1, reset_after=60.0, name="gdelt")
        breaker.trip()
        collector = GDELTCollector(session=http, limiter=RateLimiter(0, 1), breaker=breaker)

        assert collector.fetch("q", START, END, 5).status == CollectorStatus.CIRCUIT_OPEN

        # Simulate the reset window elapsing, rather than waiting it out.
        breaker._opened_at = time.monotonic() - 61.0

        result = collector.fetch("q", START, END, 5)
        assert result.ok, "the breaker lets one probe through once its window passes"
        assert not collector.breaker.is_open, "a successful probe closes it"

    def test_success_resets_the_failure_count(self, no_real_sleeping):
        script = [
            requests.exceptions.ConnectionError("x"),
            requests.exceptions.ConnectionError("x"),
            Resp(200, '{"articles": []}'),
            requests.exceptions.ConnectionError("x"),
            requests.exceptions.ConnectionError("x"),
        ]
        http = RecordingSession(script)
        collector = GDELTCollector(
            session=http, limiter=RateLimiter(0, 1),
            breaker=CircuitBreaker(threshold=3, name="gdelt"), max_attempts=1,
        )
        for _ in range(5):
            collector.fetch("q", START, END, 5)
        assert not collector.breaker.is_open, "an intervening success cleared the streak"


# --- 17 & 18. Google News ---------------------------------------------------

class TestGoogleNews:
    def test_repeated_timeouts_open_the_circuit_quickly(self, no_real_sleeping):
        """Fifty queries must not cost fifty connect timeouts."""
        http = RecordingSession([requests.exceptions.ConnectTimeout("timed out")])
        collector = GoogleNewsCollector(
            session=http, limiter=RateLimiter(0, 3),
            breaker=CircuitBreaker(threshold=3, reset_after=600, name="google_news"),
        )

        results = [collector.fetch(f"query {i}", START, END, 10) for i in range(50)]

        assert collector.breaker.is_open
        # Three failures at one attempt each: a timeout is never retried.
        assert http.calls == 3, http.calls
        assert results[0].status == CollectorStatus.TIMEOUT
        assert results[-1].status == CollectorStatus.CIRCUIT_OPEN

    def test_a_timeout_is_not_retried_but_a_reset_is(self, no_real_sleeping):
        http = RecordingSession([requests.exceptions.ConnectTimeout("t")])
        collector = GoogleNewsCollector(session=http, limiter=RateLimiter(0, 1))
        collector.fetch("q", START, END, 5)
        assert http.calls == 1, "a timeout is already the answer"

        http = RecordingSession([
            requests.exceptions.ConnectionError("reset"),
            Resp(200, "<rss><channel></channel></rss>"),
        ])
        collector = GoogleNewsCollector(session=http, limiter=RateLimiter(0, 1))
        result = collector.fetch("q", START, END, 5)
        assert http.calls == 2
        assert result.ok

    def test_a_healthy_source_survives_another_collector_failing(self, no_real_sleeping):
        """One dead source must not take a working one down with it."""
        from aios.services.collection_planner import CollectionPlanner
        from aios.services.collector import CollectorRegistry

        dead = GoogleNewsCollector(
            session=RecordingSession([requests.exceptions.ConnectTimeout("t")]),
            limiter=RateLimiter(0, 1),
            breaker=CircuitBreaker(threshold=1, reset_after=600, name="google_news"),
        )
        rss_feed = (
            "<rss><channel><title>Vendor</title><item>"
            "<title>HarmonyOS 发布新版本</title>"
            "<link>https://vendor.com/a</link>"
            "<description>正文</description></item></channel></rss>"
        )
        alive = RSSCollector(
            session=RecordingSession([Resp(200, rss_feed)]),
            limiter=RateLimiter(0, 1),
            feeds=[FeedSpec(url="https://vendor.com/feed.xml", title="Vendor")],
        )

        registry = CollectorRegistry(collectors=[dead, alive], network_mode="auto")
        planner = CollectionPlanner(registry=registry, min_candidates=1)
        outcome = planner.collect_query("HarmonyOS", START, END, 10)

        statuses = {r.collector: r.status for r in outcome.results}
        assert statuses["google_news"] == CollectorStatus.TIMEOUT
        assert statuses["rss"] == CollectorStatus.OK
        assert len(outcome.candidates) == 1
        assert outcome.succeeded

    def test_google_news_uses_a_shorter_timeout_than_the_default(self, session, db):
        from aios.services import network_service

        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])
        google = registry.by_id("google_news")
        gdelt = registry.by_id("gdelt")
        assert google.timeout < gdelt.timeout


# --- parse errors are not "no results" --------------------------------------

class TestParseErrors:
    def test_html_body_is_a_parse_error_not_an_empty_result(self, no_real_sleeping):
        html = "<!DOCTYPE html><html><body>Too Many Requests</body></html>"
        http = RecordingSession([Resp(200, html)])
        collector = GDELTCollector(session=http, limiter=RateLimiter(0, 1))

        result = collector.fetch("q", START, END, 10)

        assert result.status == CollectorStatus.PARSE_ERROR
        assert result.failed
        assert not result.zero_results
        # Diagnostic, but not a dump of the whole page.
        assert "HTML" in result.error
        assert len(result.error) < 200
        assert "Too Many Requests" not in result.error

    def test_the_classic_expecting_value_error_is_classified(self):
        import json

        status, message = classify_exception(
            json.JSONDecodeError("Expecting value", "doc", 0)
        )
        assert status == CollectorStatus.PARSE_ERROR
        assert "JSON" in message

    def test_broken_xml_is_a_parse_error(self, no_real_sleeping):
        http = RecordingSession([Resp(200, "<rss><channel>")])
        collector = GoogleNewsCollector(session=http, limiter=RateLimiter(0, 1))
        result = collector.fetch("q", START, END, 10)
        assert result.status == CollectorStatus.PARSE_ERROR

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (requests.exceptions.ProxyError("x"), CollectorStatus.PROXY_ERROR),
            (requests.exceptions.SSLError("x"), CollectorStatus.SSL_ERROR),
            (requests.exceptions.ConnectTimeout("x"), CollectorStatus.TIMEOUT),
            (requests.exceptions.ReadTimeout("x"), CollectorStatus.TIMEOUT),
            (requests.exceptions.ConnectionError("x"), CollectorStatus.NETWORK_ERROR),
        ],
    )
    def test_every_transport_failure_has_its_own_status(self, exc, expected):
        assert classify_exception(exc)[0] == expected


# --- global pacing ----------------------------------------------------------

class TestGlobalPacing:
    def test_one_limiter_paces_every_thread(self):
        """Pacing is global, not per topic: the source sees one client."""
        limiter = RateLimiter(min_interval=0.05, max_concurrency=1)
        stamps: list[float] = []

        def work():
            with limiter.slot():
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=work) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(gap >= 0.04 for gap in gaps), gaps

    def test_concurrency_is_capped(self):
        limiter = RateLimiter(min_interval=0.0, max_concurrency=1)
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def work():
            nonlocal in_flight, peak
            with limiter.slot():
                with lock:
                    in_flight += 1
                    peak = max(peak, in_flight)
                time.monotonic()
                with lock:
                    in_flight -= 1

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak == 1

    def test_gdelt_defaults_are_conservative(self, session, db):
        from aios.services import network_service

        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])
        gdelt = registry.by_id("gdelt")
        assert gdelt.limiter.max_concurrency == 1
        assert gdelt.limiter.min_interval >= 1.0
