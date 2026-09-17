"""Run status, report wording and the live Run Detail region.

The rule these tests defend, end to end: a run in which the sources failed must
never be presented the same way as a run in which the sources worked and there
was simply no news.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import stub_collectors

from aios.models import ModuleRunStatus, RunStatus
from aios.services.collector import CollectorStatus
from aios.services.report_generator import (
    DEGRADED_BANNER,
    EMPTY_SECTION_FAILED,
    EMPTY_SECTION_OK,
    coverage_notice,
    empty_section_text,
)

from test_pipeline_e2e import ScriptedClient, make_candidates, only_mobile


@pytest.fixture
def scripted(db, monkeypatch):
    """A pipeline whose model is scripted and whose network is stubbed out."""
    from aios.services import pipeline as pipeline_module

    client = ScriptedClient()
    monkeypatch.setattr(
        pipeline_module, "build_llm_service", lambda usage_sink=None: client
    )
    monkeypatch.setattr(
        pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文内容。" * 30
    )
    return client


def run_once(report_date=dt.date(2026, 9, 15)):
    from aios.database import session_scope
    from aios.repositories import runs as runs_repo
    from aios.services.pipeline import MonitoringPipeline

    with session_scope() as s:
        run_id = runs_repo.create_run(s, report_date, "manual").id
    status = MonitoringPipeline(run_id).execute()
    return run_id, status


# --- 24 & 25. run status ----------------------------------------------------

class TestRunStatus:
    def test_a_healthy_zero_result_run_is_completed(self, scripted, session, monkeypatch):
        """Sources answered, found nothing. That is a complete run."""
        only_mobile(session)
        stub_collectors(monkeypatch, [], status=CollectorStatus.OK)

        run_id, status = run_once()
        assert status == RunStatus.COMPLETED

        from aios.database import session_scope
        from aios.repositories import sources as sources_repo

        with session_scope() as s:
            stats = sources_repo.stats_for_run(s, run_id)
            assert stats, "source health is recorded even for an empty run"
            assert all(row.state in ("healthy", "unknown", "disabled") for row in stats)
            assert sources_repo.degraded_sources(s, run_id) == []

    def test_significant_collector_failure_gives_completed_with_errors(
        self, scripted, session, monkeypatch
    ):
        only_mobile(session)
        stub_collectors(
            monkeypatch, [], status=CollectorStatus.TIMEOUT, error="连接超时"
        )

        run_id, status = run_once()
        assert status == RunStatus.COMPLETED_WITH_ERRORS

        from aios.database import session_scope
        from aios.repositories import runs as runs_repo
        from aios.repositories import sources as sources_repo

        with session_scope() as s:
            run = runs_repo.get_run(s, run_id)
            assert run.error_message, "the reason is recorded, not swallowed"
            degraded = sources_repo.degraded_sources(s, run_id)
            assert degraded, "the failing sources are named"
            assert all(row.state == "unreachable" for row in degraded)

    def test_a_failing_source_does_not_fail_the_module(
        self, scripted, session, monkeypatch
    ):
        """Optional-source failure with usable evidence is a warning, not a failure."""
        from aios.services.collector import CollectorResult, SourceCollector

        only_mobile(session)

        def fetch(self, query, start, end, limit=20):
            if self.name == "gdelt":
                return CollectorResult(
                    collector=self.name, status=CollectorStatus.RATE_LIMITED,
                    error="HTTP 429",
                )
            return CollectorResult(
                collector=self.name, status=CollectorStatus.OK,
                candidates=make_candidates(2),
            )

        monkeypatch.setattr(SourceCollector, "fetch", fetch)

        run_id, status = run_once()

        from aios.database import session_scope
        from aios.repositories import runs as runs_repo

        with session_scope() as s:
            module_runs = runs_repo.module_runs_for(s, run_id)
            assert module_runs
            row = module_runs[0]
            assert row.status == ModuleRunStatus.COMPLETED_WITH_WARNINGS
            assert row.status != ModuleRunStatus.FAILED
            assert row.collection_status == "partial_collection"
            assert row.article_count > 0, "the working source's evidence was used"
        assert status == RunStatus.COMPLETED_WITH_ERRORS

    def test_module_run_records_its_collection_status_separately(
        self, scripted, session, monkeypatch
    ):
        only_mobile(session)
        stub_collectors(monkeypatch, [], status=CollectorStatus.OK)
        run_id, _ = run_once()

        from aios.database import session_scope
        from aios.repositories import runs as runs_repo

        with session_scope() as s:
            row = runs_repo.module_runs_for(s, run_id)[0]
            assert row.status == ModuleRunStatus.COMPLETED
            assert row.collection_status == "no_candidates"


# --- 26. report wording -----------------------------------------------------

class TestReportWording:
    def test_a_degraded_run_says_the_data_may_be_incomplete(
        self, scripted, session, monkeypatch
    ):
        only_mobile(session)
        stub_collectors(monkeypatch, [], status=CollectorStatus.TIMEOUT, error="连接超时")

        run_id, status = run_once()
        assert status == RunStatus.COMPLETED_WITH_ERRORS

        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.services.report_generator import render_html

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            notice = coverage_notice(report)
            assert notice is not None
            assert notice["headline"] == DEGRADED_BANNER
            assert notice["notes"], "the affected sources are named"

            html = render_html(report)
            assert DEGRADED_BANNER in html
            assert EMPTY_SECTION_FAILED in html
            assert EMPTY_SECTION_OK not in html, (
                "a failed run must never claim there was no news"
            )
            assert "Traceback" not in html and "requests.exceptions" not in html

    def test_a_healthy_empty_run_says_there_was_no_qualifying_news(
        self, scripted, session, monkeypatch
    ):
        only_mobile(session)
        stub_collectors(monkeypatch, [], status=CollectorStatus.OK)
        run_id, status = run_once()
        assert status == RunStatus.COMPLETED

        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.services.report_generator import render_html

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            assert coverage_notice(report) is None
            html = render_html(report)
            assert EMPTY_SECTION_OK in html
            assert EMPTY_SECTION_FAILED not in html
            assert DEGRADED_BANNER not in html

    @pytest.mark.parametrize(
        "state,expected",
        [
            ("", EMPTY_SECTION_OK),
            ("ok", EMPTY_SECTION_OK),
            ("no_candidates", EMPTY_SECTION_OK),
            ("collection_failed", EMPTY_SECTION_FAILED),
        ],
    )
    def test_empty_section_wording_table(self, state, expected):
        assert empty_section_text(state) == expected

    def test_partial_collection_has_its_own_wording(self):
        text = empty_section_text("partial_collection")
        assert text not in (EMPTY_SECTION_OK, EMPTY_SECTION_FAILED)
        assert "部分" in text

    def test_a_report_without_coverage_data_claims_nothing(self, session, db):
        """Reports written before coverage tracking must not be relabelled."""
        from aios.models import Report

        report = Report(report_date=dt.date(2026, 9, 1), title="旧报告", model="m")
        session.add(report)
        session.flush()
        assert report.coverage_json is None
        assert coverage_notice(report) is None

    def test_the_web_report_view_shows_the_banner(
        self, client, scripted, session, monkeypatch
    ):
        only_mobile(session)
        session.commit()
        stub_collectors(monkeypatch, [], status=CollectorStatus.TIMEOUT, error="连接超时")
        run_id, _ = run_once()

        from aios.database import session_scope
        from aios.repositories import reports as reports_repo

        with session_scope() as s:
            report_id = reports_repo.get_by_run(s, run_id).id

        page = client.get(f"/reports/{report_id}")
        assert page.status_code == 200
        assert DEGRADED_BANNER in page.text
        assert EMPTY_SECTION_FAILED in page.text
