"""本地专业版 must remain the complete v2.1 product.

v2.2 adds a second workflow; it removes nothing. This file is the guard against
the most likely way that promise gets broken - a Simple-mode convenience that
quietly takes something away from Professional mode.

Every assertion here is about *presence and function*, not appearance.
"""

from __future__ import annotations

import datetime as dt

import pytest

DEAD = {404, 405}


@pytest.fixture
def professional(client, session):
    """A client with 本地专业版 active."""
    client.post("/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False)
    session.expire_all()
    return client


@pytest.fixture
def seeded(client, session, make_event):
    """One of everything, so every id-bearing professional URL is reachable."""
    from aios.models import Report, ReportSection
    from aios.repositories import modules as modules_repo
    from aios.repositories import providers as providers_repo
    from aios.repositories import runs as runs_repo
    from aios.repositories import topics as topics_repo
    from aios.services import network_service

    module = modules_repo.get_module_by_key(session, "mobile")
    topic = module.topics[0]
    query = topic.queries[0]
    source = module.preferred_sources[0]
    keyword = topics_repo.add_excluded_keyword(session, "专业版测试词", topic_id=topic.id)
    event, _ = make_event(session, title="专业版回归事件")

    report = Report(report_date=dt.date(2026, 9, 15), title="T", model="m")
    session.add(report)
    session.flush()
    session.add(
        ReportSection(
            report_id=report.id, module_name="移动智能终端侧",
            module_key="mobile", status="watch",
        )
    )
    run = runs_repo.create_run(session, dt.date(2026, 9, 15), "manual")
    provider = providers_repo.get_by_provider_id(session, "deepseek")
    feed = network_service.add_feed(session, "https://pro.example.com/feed.xml", "回归")
    session.commit()

    return {
        "module_id": module.id,
        "topic_id": topic.id,
        "query_id": query.id,
        "source_id": source.id,
        "keyword_id": keyword.id,
        "event_id": event.id,
        "report_id": report.id,
        "run_id": run.id,
        "provider_id": provider.id,
        "feed_id": feed.id,
    }


class TestNavigationSurvives:
    def test_every_professional_page_still_renders(self, professional, seeded):
        ids = seeded
        urls = [
            "/",
            "/dashboard",
            "/dashboard/status",
            "/monitoring",
            f"/monitoring/modules/{ids['module_id']}",
            f"/monitoring/topics/{ids['topic_id']}",
            "/monitoring/ai/new",
            f"/monitoring/ai/modules/{ids['module_id']}/topic",
            "/reports",
            f"/reports/{ids['report_id']}",
            f"/reports/{ids['report_id']}?view=evidence",
            f"/reports/{ids['report_id']}?view=events",
            f"/reports/{ids['report_id']}/html",
            f"/reports/{ids['report_id']}/json",
            f"/reports/{ids['report_id']}/audit",
            "/compare",
            "/compare?date_a=2026-09-14&date_b=2026-09-15",
            "/events",
            f"/events/{ids['event_id']}",
            "/runs",
            f"/runs/{ids['run_id']}",
            f"/runs/{ids['run_id']}/status",
            f"/runs/{ids['run_id']}/live",
            f"/runs/{ids['run_id']}/logs?after=0",
            "/settings",
            "/settings/ai",
        ]
        broken = [(u, professional.get(u).status_code) for u in urls]
        broken = [pair for pair in broken if pair[1] != 200]
        assert not broken, f"professional pages not rendering: {broken}"

    def test_the_sidebar_still_lists_every_section(self, professional):
        body = professional.get("/").text
        for label in ("概览", "监测配置", "情报事件", "报告", "对比", "运行记录", "设置"):
            assert label in body

    def test_the_monitoring_tree_is_intact(self, professional, session):
        body = professional.get("/monitoring").text
        for name in (
            "移动智能终端侧", "PC 侧", "服务器侧", "智算超节点侧",
            "物联网侧", "无人飞行器侧", "具身智能侧", "太空智算侧",
        ):
            assert name in body

    def test_module_topics_and_queries_still_show(self, professional, session):
        from aios.repositories import modules as modules_repo

        module = modules_repo.get_module_by_key(session, "mobile")
        body = professional.get(f"/monitoring/modules/{module.id}").text
        assert "HarmonyOS" in body
        assert "Android" in body


class TestAdvancedControlsSurvive:
    def test_network_and_collector_settings_are_present(self, professional):
        body = professional.get("/settings").text
        for label in ("网络", "数据源", "代理"):
            assert label in body

    def test_collector_toggles_are_present(self, professional):
        body = professional.get("/settings").text
        for name in ("GDELT", "Google News", "RSS"):
            assert name in body, f"{name} toggle is missing from Professional settings"

    def test_task_model_routing_is_present(self, professional):
        body = professional.get("/settings/ai").text
        for label in ("主题情报分析", "事件归并判断", "日报综合研判"):
            assert label in body

    def test_source_health_is_present_on_the_run_view(self, professional, seeded):
        body = professional.get(f"/runs/{seeded['run_id']}").text
        assert "数据源" in body

    def test_the_classic_run_button_still_exists(self, professional):
        body = professional.get("/").text
        assert 'action="/run"' in body

    def test_the_advanced_scheduler_is_present(self, professional):
        body = professional.get("/settings").text
        assert "自动监测" in body

    def test_the_ai_configuration_accelerator_is_present(self, professional):
        body = professional.get("/monitoring/ai/new").text
        assert "AI" in body


class TestClassicWriteOperationsSurvive:
    def test_no_professional_post_route_is_dead(self, professional, seeded, monkeypatch):
        from aios.routers import providers as providers_router
        from aios.services import keyring_service, network_service, run_manager
        from aios.services.config_generator import ConfigGenerationError

        monkeypatch.setattr(keyring_service, "get_api_key", lambda **k: "sk-" + "x" * 20)
        monkeypatch.setattr(
            run_manager.RunManager, "start_run", lambda self, **k: seeded["run_id"]
        )
        monkeypatch.setattr(
            "aios.services.llm.service.test_provider_row",
            lambda row, **kw: {"ok": True, "model": "m", "latency_ms": 1},
        )
        monkeypatch.setattr(network_service, "run_diagnostics", lambda *a, **k: [])

        from aios.routers import config_ai

        class Offline:
            def __init__(self, *a, **k):
                pass

            def readiness_error(self) -> str:
                return ""

            def generate_module(self, *a, **k):
                raise ConfigGenerationError("route check only")

            generate_topic = generate_module

        monkeypatch.setattr(config_ai.config_generator, "ConfigGenerator", Offline)

        ids = seeded
        posts = [
            ("/run", {}),
            (f"/runs/{ids['run_id']}/cancel", {}),
            (f"/monitoring/modules/{ids['module_id']}/toggle", {}),
            (f"/monitoring/modules/{ids['module_id']}/topics", {"name": "回归主题"}),
            (f"/monitoring/topics/{ids['topic_id']}/queries", {"query": "回归检索式"}),
            (f"/monitoring/topics/{ids['topic_id']}/toggle", {}),
            (f"/monitoring/queries/{ids['query_id']}/toggle", {}),
            (f"/events/{ids['event_id']}/status", {"status": "watching"}),
            (f"/reports/{ids['report_id']}/export", {}),
            (f"/settings/ai/providers/{ids['provider_id']}/test", {}),
            ("/settings/network/mode", {"mode": "auto"}),
            ("/settings/network/transport", {"connection_mode": "system"}),
            ("/settings/scheduler", {"time": "06:00"}),
            ("/monitoring/ai/generate", {"description": "监测某个领域的动态。"}),
        ]
        dead = [
            (url, status)
            for url, data in posts
            if (status := professional.post(url, data=data, follow_redirects=False).status_code)
            in DEAD
        ]
        assert not dead, f"dead professional POST routes: {dead}"

    def test_a_classic_module_can_still_be_created_and_edited(
        self, professional, session
    ):
        from aios.repositories import modules as modules_repo

        response = professional.post(
            "/monitoring/modules",
            data={
                "key": "regression_mod", "name": "回归模块",
                "lookback_days": "3", "max_candidates": "18", "max_report_items": "2",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        session.expire_all()
        assert modules_repo.get_module_by_key(session, "regression_mod") is not None


class TestBothModesShareOneDatabase:
    def test_a_research_topic_does_not_appear_as_a_monitoring_module(
        self, professional, session
    ):
        """34: one database, but the two models stay distinct."""
        from aios.repositories import modules as modules_repo
        from aios.repositories import research_topics as topics_repo

        before = modules_repo.count_modules(session)
        topics_repo.create_topic(session, name="简易版主题")
        session.commit()

        session.expire_all()
        assert modules_repo.count_modules(session) == before
        assert "简易版主题" not in professional.get("/monitoring").text

    def test_agent_reports_appear_in_the_professional_report_list(
        self, professional, session
    ):
        """One report system: a research report is a first-class report."""
        from aios.models import RUN_ENGINE_AGENT, Report
        from aios.repositories import research_topics as topics_repo

        topic = topics_repo.create_topic(session, name="共享主题")
        session.flush()
        report = Report(
            report_date=dt.date(2026, 9, 20),
            title="研究报告 · 共享主题",
            model="m",
            research_topic_id=topic.id,
            engine=RUN_ENGINE_AGENT,
        )
        session.add(report)
        session.commit()

        # The list is keyed by date, so presence means "it is linked from here".
        body = professional.get("/reports").text
        assert f'/reports/{report.id}"' in body
        assert "2026-09-20" in body
        # ...and it opens with the professional chrome.
        assert professional.get(f"/reports/{report.id}").status_code == 200

    def test_agent_events_appear_in_the_professional_event_list(
        self, professional, session, make_event
    ):
        from aios.repositories import research_topics as topics_repo

        topic = topics_repo.create_topic(session, name="共享事件主题")
        session.flush()
        event, _ = make_event(session, title="Agent 发现的事件")
        event.research_topic_id = topic.id
        session.commit()

        assert "Agent 发现的事件" in professional.get("/events").text

    def test_a_simple_report_is_readable_in_professional_mode(
        self, professional, session
    ):
        from aios.models import RUN_ENGINE_AGENT, Report

        report = Report(
            report_date=dt.date(2026, 9, 20), title="T", model="m",
            engine=RUN_ENGINE_AGENT,
        )
        session.add(report)
        session.commit()

        body = professional.get(f"/reports/{report.id}").text
        assert "breadcrumb" in body  # the professional chrome, not the Simple shell


class TestClassicEngineIsIntact:
    def test_the_classic_pipeline_module_is_unchanged_in_shape(self):
        """39: the Classic engine stays valuable and is not rewritten."""
        from aios.services import pipeline

        assert hasattr(pipeline, "MonitoringPipeline")
        assert hasattr(pipeline, "run_pipeline")
        assert hasattr(pipeline, "build_plan")

    def test_a_classic_run_still_dispatches_to_the_classic_pipeline(
        self, session, monkeypatch
    ):
        from aios.models import RUN_ENGINE_CLASSIC
        from aios.repositories import runs as runs_repo
        from aios.services import run_manager
        from aios.timeutil import local_today

        called: list[str] = []
        monkeypatch.setattr(
            run_manager, "run_pipeline",
            lambda run_id, cancel_check=None: called.append("classic") or "completed",
        )

        run = runs_repo.create_run(session, local_today(), "manual")
        run.engine = RUN_ENGINE_CLASSIC
        session.commit()

        run_manager._dispatch(run.id, lambda: False)
        assert called == ["classic"]

    def test_an_agent_run_dispatches_to_the_research_pipeline(
        self, session, monkeypatch
    ):
        from aios.models import RUN_ENGINE_AGENT
        from aios.repositories import research_topics as topics_repo
        from aios.repositories import runs as runs_repo
        from aios.services import research_pipeline, run_manager
        from aios.timeutil import local_today

        called: list[str] = []
        monkeypatch.setattr(
            research_pipeline, "run_research_pipeline",
            lambda run_id, cancel_check=None: called.append("agent") or "completed",
        )

        topic = topics_repo.create_topic(session, name="调度主题")
        run = runs_repo.create_run(session, local_today(), "manual")
        run.engine = RUN_ENGINE_AGENT
        run.research_topic_id = topic.id
        session.commit()

        run_manager._dispatch(run.id, lambda: False)
        assert called == ["agent"]

    def test_a_run_with_no_engine_recorded_is_treated_as_classic(
        self, session, monkeypatch
    ):
        """Every run written before v2.2 has to keep working."""
        from sqlalchemy import text

        from aios.database import get_engine
        from aios.repositories import runs as runs_repo
        from aios.services import run_manager
        from aios.timeutil import local_today

        run = runs_repo.create_run(session, local_today(), "manual")
        run_id = run.id
        session.commit()

        with get_engine().begin() as connection:
            connection.execute(
                text("UPDATE monitoring_runs SET engine='' WHERE id=:i"), {"i": run_id}
            )

        called: list[str] = []
        monkeypatch.setattr(
            run_manager, "run_pipeline",
            lambda rid, cancel_check=None: called.append("classic") or "completed",
        )
        run_manager._dispatch(run_id, lambda: False)
        assert called == ["classic"]


class TestBackwardCompatibility:
    def test_a_legacy_report_without_coverage_still_renders(self, professional, session):
        """49: existing v2.1 reports must not be invalidated."""
        from aios.models import Report, ReportSection

        report = Report(
            report_date=dt.date(2026, 9, 1),
            title="v2.1 报告",
            model="deepseek-chat",
            coverage_json=None,
            engine="classic",
        )
        session.add(report)
        session.flush()
        session.add(
            ReportSection(
                report_id=report.id, module_name="移动智能终端侧",
                module_key="mobile", status="watch", coverage_state="",
            )
        )
        session.commit()

        assert professional.get(f"/reports/{report.id}").status_code == 200
        assert professional.get(f"/reports/{report.id}/html").status_code == 200
        assert professional.get(f"/reports/{report.id}/json").status_code == 200

    def test_a_legacy_event_without_identity_columns_still_matches(self, session):
        """Classic events leave the fingerprint empty and match lexically."""
        from aios.services.event_matcher import EventMatcher
        from aios.services.intelligence_analyzer import CandidateIntelligence

        class LegacyEvent:
            id = 1
            title = "HarmonyOS 7 生态进展"
            summary = "生态规模扩大。"
            topic_id = None
            first_seen_date = dt.date(2026, 9, 1)
            last_seen_date = dt.date(2026, 9, 1)
            observation_count = 1

        candidate = CandidateIntelligence(
            tag="情报", title="HarmonyOS 7 生态进展", fact_summary="生态规模扩大。",
            assessment="", importance=1, confidence="high", article_ids=[1],
        )
        decision = EventMatcher(client=None, use_llm=False).match(
            candidate, [LegacyEvent()]
        )
        assert decision.is_match is True
        assert decision.method == "rules"

    def test_the_legacy_importer_still_works(self, professional):
        import io
        import json

        payload = {
            "date": "2026-09-01",
            "sections": [
                {
                    "section": "移动智能终端侧", "status": "watch",
                    "items": [], "metrics": [], "_evidence": [],
                }
            ],
            "overview": {"headline": {}, "trends": [], "metrics": []},
        }
        files = {
            "file": (
                "legacy.json",
                io.BytesIO(json.dumps(payload).encode()),
                "application/json",
            )
        }
        response = professional.post(
            "/settings/import", files=files, follow_redirects=False
        )
        assert response.status_code == 303

    def test_backup_still_includes_the_new_tables(self, professional, db):
        professional.post("/settings/backup", follow_redirects=False)
        archives = list((db.data_dir / "backups").glob("aios-backup-*.zip"))
        assert archives
