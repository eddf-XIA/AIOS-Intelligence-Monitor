"""Acceptance TEST 2: full runs on a network where Google is unreachable.

These drive the *real* :class:`MonitoringPipeline` - real planner, real
registry, real circuit breakers, real health accounting - with only the HTTP
transport faked. It is the closest thing to a mainland-network run that can be
asserted on a machine that is not actually on one.

Two scenarios, because they have genuinely different correct answers:

* **Sources that work.** Staged collection is satisfied before the chain ever
  reaches Google News, so Google is not contacted at all and nothing about the
  run is degraded. Reporting that as a clean ``completed`` is the truth.
* **Sources that answer but find nothing.** The chain falls through to Google
  News, which times out. Now the run *is* degraded, and has to say so - while
  still being bounded to a handful of attempts rather than one per query.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
import requests

from aios.models import ModuleRunStatus, RunStatus
from aios.services import network_service

from test_pipeline_e2e import ScriptedClient, only_mobile


GDELT_WITH_RESULTS = """{"articles": [
  {"url": "https://tech.cn/a1", "title": "国产操作系统发布新版本", "domain": "tech.cn",
   "seendate": "20260915T080000Z", "language": "chinese"},
  {"url": "https://tech.cn/a2", "title": "终端厂商公布生态数据", "domain": "tech.cn",
   "seendate": "20260915T090000Z", "language": "chinese"}
]}"""

GDELT_EMPTY = '{"articles": []}'

RSS_WITH_RESULTS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>厂商官方博客</title>'
    "<item><title>鸿蒙操作系统开发者大会公布进展</title>"
    "<link>https://vendor.cn/post</link>"
    "<description>大会公布了系统版本与生态进展。</description>"
    "<pubDate>Mon, 14 Sep 2026 08:00:00 GMT</pubDate></item>"
    "</channel></rss>"
)

RSS_EMPTY = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>厂商官方博客</title>'
    "</channel></rss>"
)


class Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict = {}

    def json(self):
        import json

        return json.loads(self.text)


class MainlandNetwork:
    """A transport where Google is unreachable and everything else works.

    Google requests raise ``ConnectTimeout`` *after* recording how long the
    caller was prepared to wait, so a test can assert on the total timeout
    budget the run would really have spent.
    """

    def __init__(self, gdelt_body=GDELT_WITH_RESULTS, rss_body=RSS_WITH_RESULTS):
        self.gdelt_body = gdelt_body
        self.rss_body = rss_body
        self.lock = threading.Lock()
        self.calls: list[tuple[str, float]] = []
        self.proxies: dict = {}
        self.trust_env = True

    def get(self, url, **kwargs):
        timeout = float(kwargs.get("timeout") or 0)
        with self.lock:
            self.calls.append((url, timeout))
        if "google.com" in url:
            raise requests.exceptions.ConnectTimeout("connection timed out")
        if "gdeltproject.org" in url:
            return Resp(200, self.gdelt_body)
        return Resp(200, self.rss_body)

    def request(self, method, url, **kwargs):
        return self.get(url, **kwargs)

    def urls_for(self, needle: str) -> list[str]:
        return [url for url, _ in self.calls if needle in url]

    def timeout_budget_for(self, needle: str) -> float:
        return sum(t for url, t in self.calls if needle in url)


def _install(monkeypatch, session, transport):
    from aios.services import pipeline as pipeline_module

    only_mobile(session)
    network_service.set_network_mode(session, network_service.MODE_CHINA)
    network_service.add_feed(session, "https://vendor.cn/feed.xml", "厂商官方博客")
    session.commit()

    monkeypatch.setattr(
        network_service, "build_session", lambda settings=None, session=None: transport
    )
    client = ScriptedClient()
    monkeypatch.setattr(
        pipeline_module, "build_llm_service", lambda usage_sink=None: client
    )
    monkeypatch.setattr(
        pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文内容。" * 30
    )
    return transport


@pytest.fixture
def mainland(db, monkeypatch, session):
    """China mode where the directly-reachable sources return real results."""
    return _install(monkeypatch, session, MainlandNetwork())


@pytest.fixture
def mainland_sparse(db, monkeypatch, session):
    """China mode where local sources answer honestly but find nothing."""
    return _install(
        monkeypatch,
        session,
        MainlandNetwork(gdelt_body=GDELT_EMPTY, rss_body=RSS_EMPTY),
    )


def run_pipeline_once():
    from aios.database import session_scope
    from aios.repositories import runs as runs_repo
    from aios.services.pipeline import MonitoringPipeline

    with session_scope() as s:
        run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
    return run_id, MonitoringPipeline(run_id).execute()


# --- sources that work ------------------------------------------------------

class TestMainlandWithWorkingSources:
    def test_google_news_is_never_contacted_when_it_is_not_needed(self, mainland):
        """The strongest form of "not required": zero requests, zero seconds."""
        run_pipeline_once()

        assert mainland.urls_for("google.com") == [], (
            "staged collection was satisfied before the chain reached Google News"
        )
        assert mainland.timeout_budget_for("google.com") == 0

    def test_directly_reachable_sources_are_asked_first(self, mainland):
        run_pipeline_once()

        ordered = [
            url for url, _ in mainland.calls
            if any(h in url for h in ("vendor.cn", "gdeltproject.org", "google.com"))
        ]
        assert ordered, "something was collected"
        assert "google.com" not in ordered[0], (
            "Google News must never be the primary source in china mode"
        )

    def test_the_run_is_clean_because_nothing_actually_failed(self, mainland):
        """Not asking a source is not the same as a source failing."""
        from aios.database import session_scope
        from aios.repositories import sources as sources_repo

        run_id, status = run_pipeline_once()

        assert status == RunStatus.COMPLETED

        with session_scope() as s:
            stats = {r.collector: r for r in sources_repo.stats_for_run(s, run_id)}
            assert stats["google_news"].requests_attempted == 0
            assert stats["google_news"].state == "unknown", (
                "a source we chose not to ask is 未检测, not 不可用"
            )
            assert sources_repo.degraded_sources(s, run_id) == []

    def test_a_report_is_produced_from_the_reachable_sources(self, mainland):
        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.services.report_generator import DEGRADED_BANNER, render_html

        run_id, _ = run_pipeline_once()

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            assert report is not None
            assert report.sections
            assert DEGRADED_BANNER not in render_html(report), (
                "nothing was degraded, so nothing should be claimed"
            )

    def test_the_module_completes_without_warnings(self, mainland):
        from aios.database import session_scope
        from aios.repositories import runs as runs_repo

        run_id, _ = run_pipeline_once()

        with session_scope() as s:
            module_run = runs_repo.module_runs_for(s, run_id)[0]
            assert module_run.status == ModuleRunStatus.COMPLETED
            assert module_run.collection_status == "ok"
            assert module_run.article_count > 0


# --- sources that answer but find nothing -----------------------------------

class TestMainlandFallingThroughToGoogle:
    def test_google_news_is_bounded_not_asked_once_per_query(self, mainland_sparse):
        """The 25-seconds-per-query problem, stated as a hard budget."""
        run_pipeline_once()

        google_calls = mainland_sparse.urls_for("google.com")
        assert google_calls, "the chain did fall through to Google News"
        # Default breaker threshold is 3, and a timeout is never retried.
        assert len(google_calls) <= 4, (
            f"Google News was asked {len(google_calls)} times while unreachable"
        )
        assert mainland_sparse.timeout_budget_for("google.com") <= 60, (
            "the run would still have burned a minute of connect timeouts"
        )

    def test_the_run_reports_the_failure_truthfully(self, mainland_sparse):
        from aios.database import session_scope
        from aios.repositories import runs as runs_repo
        from aios.repositories import sources as sources_repo

        run_id, status = run_pipeline_once()

        assert status == RunStatus.COMPLETED_WITH_ERRORS, (
            "a source that never answered must not read as a clean run"
        )

        with session_scope() as s:
            stats = {r.collector: r for r in sources_repo.stats_for_run(s, run_id)}
            assert stats["google_news"].requests_succeeded == 0
            assert stats["google_news"].state in ("unreachable", "circuit_open")
            assert stats["gdelt"].state == "healthy"
            assert stats["gdelt"].requests_succeeded > 0

            run = runs_repo.get_run(s, run_id)
            assert "Google News" in run.error_message

    def test_the_empty_report_does_not_claim_there_was_no_news(self, mainland_sparse):
        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.services.report_generator import (
            DEGRADED_BANNER,
            EMPTY_SECTION_OK,
            coverage_notice,
            render_html,
        )

        run_id, _ = run_pipeline_once()

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            notice = coverage_notice(report)
            assert notice is not None
            assert notice["headline"] == DEGRADED_BANNER
            assert any("Google News" in note for note in notice["notes"])

            html = render_html(report)
            assert DEGRADED_BANNER in html
            assert EMPTY_SECTION_OK not in html, (
                "coverage was incomplete, so 'no qualifying news' is not sayable"
            )
            assert "Traceback" not in html and "requests.exceptions" not in html

    def test_the_working_sources_were_still_used(self, mainland_sparse):
        run_pipeline_once()
        assert mainland_sparse.urls_for("gdeltproject.org"), "GDELT was still asked"
        assert mainland_sparse.urls_for("vendor.cn"), "the RSS feed was still read"

    def test_every_run_is_bounded_not_just_the_first(self, mainland_sparse):
        """Breakers are per-run, so each run must re-bound its own cost.

        The bound is the breaker threshold plus the limiter's concurrency
        headroom: with three requests allowed in flight, a fourth can start
        before the third records its failure. That race is benign - what must
        never happen is one timeout per query.
        """
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo

        with session_scope() as s:
            module = modules_repo.get_module_by_key(s, "mobile")
            query_count = module.query_count
        assert query_count >= 4, "the module has enough queries for this to mean something"

        run_pipeline_once()
        first = len(mainland_sparse.urls_for("google.com"))

        run_pipeline_once()
        second = len(mainland_sparse.urls_for("google.com")) - first

        for label, count in (("first", first), ("second", second)):
            assert count <= 5, f"the {label} run asked Google News {count} times"
            assert count < query_count, (
                f"the {label} run approached one timeout per query ({count}/{query_count})"
            )
