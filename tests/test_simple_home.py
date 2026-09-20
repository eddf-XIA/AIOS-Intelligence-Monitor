"""The Simple home page as a workflow: run, watch, read, come back.

Simple mode is one-page-first, so the assertions here are mostly about what the
page does *not* make the user do: no navigation to start a run, no redirect to a
technical run view while it executes, no lost context after reading a report,
and no exposure of the machinery underneath.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.models import RUN_ENGINE_AGENT, RunStatus
from aios.repositories import providers as providers_repo
from aios.repositories import reports as reports_repo
from aios.repositories import research_topics as topics_repo
from aios.repositories import runs as runs_repo
from aios.routers.simple import ACTIVE_TOPIC_KEY
from aios.services import settings_service
from aios.services.research import AGENT_LOCAL, set_selection
from aios.services.research_pipeline import create_research_run, stage_value
from aios.timeutil import utcnow

REAL_KEY = "sk-" + "b" * 32


@pytest.fixture
def engine_ready(session):
    """A usable research engine, so 开始研究 is not blocked."""
    from aios.services import keyring_service
    from aios.services.provider_migration import sync_key_state

    row = providers_repo.get_by_provider_id(session, "deepseek")
    row.default_model = "deepseek-chat"
    row.enabled = True
    session.flush()
    keyring_service.set_provider_key("deepseek", REAL_KEY)
    sync_key_state(session, "deepseek")
    providers_repo.set_default(session, row)
    set_selection(session, "deepseek", AGENT_LOCAL)
    session.commit()
    return row


@pytest.fixture
def topic(session):
    topic = topics_repo.create_topic(
        session,
        name="全球智能终端",
        brief="跟踪智能终端操作系统的重要进展。",
        focus_areas=["技术进展"],
        keywords=["HarmonyOS"],
        regions="中国 + 全球",
        window_hours=72,
    )
    session.commit()
    return topic


@pytest.fixture
def no_worker(monkeypatch):
    """Create the run row but never execute a pipeline in a test."""
    started: dict[str, int] = {}

    def fake_start(self, topic_id, trigger_type="manual"):
        from aios.database import session_scope

        with session_scope() as session:
            run = create_research_run(session, topic_id, trigger_type)
            run.status = RunStatus.RUNNING
            run.started_at = utcnow()
            run.stage = stage_value("searching")
            started["run_id"] = run.id
            started["topic_id"] = topic_id
        return started["run_id"]

    from aios.services.run_manager import RunManager

    monkeypatch.setattr(RunManager, "start_research_run", fake_start)
    return started


class TestStartResearch:
    def test_start_research_works_from_the_simple_home(
        self, client, session, engine_ready, topic, no_worker
    ):
        response = client.post(
            "/simple/research", data={"topic_id": topic.id}, follow_redirects=False
        )
        assert response.status_code == 303
        # Stays on the home page rather than redirecting to a technical run view.
        assert response.headers["location"].startswith("/?")
        assert no_worker["topic_id"] == topic.id

    def test_a_temporary_brief_can_be_run_without_saving_first(
        self, client, session, engine_ready, no_worker
    ):
        """22: the user may run a new topic directly."""
        before = topics_repo.count_topics(session)
        response = client.post(
            "/simple/research",
            data={
                "name": "临时主题",
                "brief": "看看最近有什么重要进展",
                "scope": "",
                "focus_areas": "技术进展",
                "exclusions": "",
                "keywords": "",
                "regions": "全球",
                "window_hours": "72",
                "depth": "standard",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        session.expire_all()
        # Persisted because a run must point at a topic to be trackable.
        assert topics_repo.count_topics(session) == before + 1
        assert topics_repo.get_by_name(session, "临时主题") is not None

    def test_an_unready_engine_sends_the_user_to_setup_not_into_a_run(
        self, client, session, topic
    ):
        response = client.post(
            "/simple/research", data={"topic_id": topic.id}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/simple/engine")
        session.expire_all()
        assert runs_repo.latest_run(session) is None

    def test_an_unknown_topic_is_refused(self, client, engine_ready):
        response = client.post(
            "/simple/research", data={"topic_id": "99999"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "不存在" in response.headers["location"] or "%" in response.headers["location"]

    def test_a_concurrent_run_is_refused_cleanly(
        self, client, session, engine_ready, topic, monkeypatch
    ):
        from aios.services.run_manager import RunAlreadyActive, RunManager

        def busy(self, topic_id, trigger_type="manual"):
            raise RunAlreadyActive(1)

        monkeypatch.setattr(RunManager, "start_research_run", busy)
        response = client.post(
            "/simple/research", data={"topic_id": topic.id}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/?")

    def test_the_home_page_has_one_obvious_primary_action(
        self, client, session, engine_ready, topic
    ):
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/").text
        assert "开始研究" in body
        assert body.count("btn-primary") >= 1


class TestLiveRun:
    @pytest.fixture
    def running(self, session, topic):
        run = create_research_run(session, topic.id)
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        run.stage = stage_value("reading")
        run.total_articles = 17
        run.total_events = 6
        session.commit()
        settings_service.set_value(session, ACTIVE_TOPIC_KEY, str(topic.id))
        session.commit()
        return run

    def test_the_home_page_transforms_inline_while_running(
        self, client, session, engine_ready, topic, running
    ):
        body = client.get("/").text
        assert "研究进行中" in body
        assert "本次研究" in body
        # The topic editor is out of the way while research runs.
        assert "AI 完善主题" not in body

    def test_human_stages_are_shown(self, client, session, engine_ready, topic, running):
        body = client.get("/").text
        for label in ("理解研究目标", "搜索公开信息", "阅读与交叉验证", "整理关键事件", "生成报告"):
            assert label in body

    def test_technical_stages_are_not_shown(
        self, client, session, engine_ready, topic, running
    ):
        """24: collector diagnostics belong in Professional mode."""
        body = client.get("/").text
        for word in (
            "GDELT", "Google News", "RSS", "collector", "parse_error",
            "circuit", "query 17", "RawArticle", "ObservationSource",
        ):
            assert word not in body

    def test_real_counters_are_shown(self, client, session, engine_ready, topic, running):
        body = client.get("/").text
        assert "已研究" in body and "17" in body
        assert "发现" in body and "6" in body

    def test_the_running_state_polls_itself(
        self, client, session, engine_ready, topic, running
    ):
        body = client.get("/").text
        assert f'hx-get="/simple/run/{running.id}"' in body
        assert 'hx-trigger="every 2s"' in body
        assert 'hx-swap="outerHTML"' in body

    def test_the_fragment_is_servable_on_its_own(
        self, client, session, engine_ready, topic, running
    ):
        response = client.get(f"/simple/run/{running.id}")
        assert response.status_code == 200
        body = response.text
        assert "研究进行中" in body
        # A fragment, not a whole page.
        assert "<!DOCTYPE html>" not in body

    def test_the_elapsed_timer_has_what_the_browser_needs(
        self, client, session, engine_ready, topic, running
    ):
        body = client.get(f"/simple/run/{running.id}").text
        assert "data-elapsed" in body
        assert 'data-elapsed-live="1"' in body
        assert "data-elapsed-start=" in body
        assert 'id="run-elapsed-sync"' in body

    def test_the_start_timestamp_carries_an_explicit_offset(
        self, client, session, engine_ready, topic, running
    ):
        """A naive timestamp would be read as local time - a whole-timezone bug."""
        import re

        body = client.get(f"/simple/run/{running.id}").text
        match = re.search(r'data-elapsed-start="([^"]+)"', body)
        assert match
        assert match.group(1).endswith("+00:00")

    @pytest.mark.parametrize(
        "status",
        [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ],
    )
    def test_a_terminal_state_stops_polling(
        self, client, session, engine_ready, topic, running, status
    ):
        running.status = status
        running.finished_at = utcnow()
        session.commit()

        body = client.get(f"/simple/run/{running.id}").text
        assert "hx-trigger" not in body
        assert 'data-elapsed-live="0"' in body
        assert 'data-run-finished="1"' in body

    def test_a_running_run_can_be_stopped(
        self, client, session, engine_ready, topic, running, monkeypatch
    ):
        cancelled: list[int] = []
        from aios.services.run_manager import RunManager

        monkeypatch.setattr(
            RunManager, "cancel", lambda self, run_id: cancelled.append(run_id) or True
        )
        response = client.post(
            f"/simple/run/{running.id}/cancel", follow_redirects=False
        )
        assert response.status_code == 303
        assert cancelled == [running.id]

    def test_an_unknown_run_fragment_is_a_404(self, client):
        assert client.get("/simple/run/99999").status_code == 404


class TestResultCard:
    @pytest.fixture
    def finished(self, session, topic):
        from aios.models import Report, ReportItem, ReportSection

        run = create_research_run(session, topic.id)
        run.status = RunStatus.COMPLETED
        run.started_at = utcnow() - dt.timedelta(seconds=161)
        run.finished_at = utcnow()
        run.stage = RunStatus.COMPLETED
        run.coverage_status = "complete"
        session.flush()

        report = Report(
            report_date=dt.date(2026, 9, 20),
            run_id=run.id,
            title="智能终端操作系统监测日报",
            research_topic_id=topic.id,
            engine=RUN_ENGINE_AGENT,
            coverage_status="complete",
            headline_json={"title": "HarmonyOS 6 正式发布", "body": "华为发布 HarmonyOS 6。"},
        )
        session.add(report)
        session.flush()
        section = ReportSection(
            report_id=report.id, module_name="移动智能终端侧",
            module_key="mobile", status="new",
        )
        session.add(section)
        session.flush()
        for index in range(3):
            session.add(
                ReportItem(
                    report_section_id=section.id,
                    title=f"动态 {index}",
                    fact_summary="事实。",
                    event_state="new" if index < 2 else "updated",
                )
            )
        session.commit()
        settings_service.set_value(session, ACTIVE_TOPIC_KEY, str(topic.id))
        session.commit()
        return {"run": run, "report": report}

    def test_the_result_card_appears_on_the_home_page(
        self, client, session, engine_ready, topic, finished
    ):
        body = client.get("/").text
        assert "研究完成" in body
        assert "项重要动态" in body
        assert "3" in body

    def test_the_result_card_shows_new_and_updated_counts(
        self, client, session, engine_ready, topic, finished
    ):
        body = client.get("/").text
        assert "新增 2" in body
        assert "更新 1" in body

    def test_the_result_card_shows_todays_focus(
        self, client, session, engine_ready, topic, finished
    ):
        body = client.get("/").text
        assert "今日焦点" in body
        assert "HarmonyOS 6 正式发布" in body

    def test_the_elapsed_time_is_shown_as_a_duration(
        self, client, session, engine_ready, topic, finished
    ):
        body = client.get("/").text
        assert "用时" in body
        assert "2 分" in body

    def test_the_result_menu_routes_correctly(
        self, client, session, engine_ready, topic, finished
    ):
        report_id = finished["report"].id
        body = client.get("/").text

        for label, url in (
            ("查看完整报告", f"/reports/{report_id}?from=simple"),
            ("查看变化", f"/simple/changes/{report_id}"),
            ("来源证据", f"/simple/sources/{report_id}"),
            ("历史记录", f"/simple/history?topic_id={topic.id}"),
        ):
            assert label in body
            assert url in body, url

        # ...and every one of them resolves.
        for url in (
            f"/reports/{report_id}?from=simple",
            f"/simple/changes/{report_id}",
            f"/simple/sources/{report_id}",
            f"/simple/history?topic_id={topic.id}",
        ):
            assert client.get(url).status_code == 200, url

    def test_the_home_page_does_not_dump_the_whole_report(
        self, client, session, engine_ready, topic, finished
    ):
        """12: headline result and navigation, not every paragraph."""
        body = client.get("/").text
        assert "动态 0" not in body
        assert "趋势研判" not in body

    def test_the_run_can_be_repeated_from_the_result_state(
        self, client, session, engine_ready, topic, finished
    ):
        assert "再研究一次" in client.get("/").text


class TestContextRestoration:
    def test_returning_from_a_report_restores_the_simple_context(
        self, client, session, engine_ready, topic
    ):
        """40: 返回主页 must land on the user's own topic, not an empty page."""
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        before = client.get("/").text
        assert topic.name in before

        from aios.models import Report

        report = Report(
            report_date=dt.date(2026, 9, 20),
            title="T",
            model="m",
            research_topic_id=topic.id,
            engine=RUN_ENGINE_AGENT,
        )
        session.add(report)
        session.commit()

        detail = client.get(f"/reports/{report.id}?from=simple")
        assert detail.status_code == 200
        assert "返回主页" in detail.text

        after = client.get("/").text
        assert topic.name in after
        assert "开始研究" in after

    def test_the_active_topic_survives_a_restart(self, client, session, topic, db):
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)

        from fastapi.testclient import TestClient

        from aios.app import create_app

        with TestClient(create_app()) as fresh:
            assert topic.name in fresh.get("/").text

    def test_new_topic_clears_the_remembered_context(
        self, client, session, engine_ready, topic
    ):
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/?new=1").text
        assert "你想研究什么？" in body
        assert "AI 完善主题" in body

    def test_a_deleted_active_topic_does_not_break_the_home_page(
        self, client, session, topic
    ):
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        topics_repo.delete_topic(session, topics_repo.get_topic(session, topic.id))
        session.commit()

        response = client.get("/")
        assert response.status_code == 200
        assert "你想研究什么？" in response.text


class TestComplexityIsHidden:
    """4: Simple mode must not expose the machinery by default."""

    HIDDEN = (
        "GDELT", "Google News", "RSS", "Preferred Source", "Collector",
        "Circuit Breaker", "circuit_breaker", "Proxy", "RawArticle",
        "ObservationSource", "LLMUsage", "prompt_tokens", "total_tokens",
        "network_mode", "collector_gdelt_enabled", "检索式", "SQLite",
        "event_key", "observation_id", "module_key",
    )

    @pytest.mark.parametrize("url", ["/", "/simple", "/simple/history"])
    def test_technical_vocabulary_is_absent(self, client, session, engine_ready, url):
        body = client.get(url).text
        leaked = [word for word in self.HIDDEN if word in body]
        assert not leaked, f"{url} leaks: {leaked}"

    def test_no_internal_json_is_rendered(self, client, session, engine_ready):
        body = client.get("/").text
        for fragment in ('{"events"', '{"coverage"', '"source_ids"', '"event_type"'):
            assert fragment not in body

    def test_the_simple_shell_has_a_minimal_navigation(
        self, client, session, engine_ready
    ):
        """32: 首页 and 历史, not the professional sidebar."""
        body = client.get("/").text
        assert "首页" in body
        assert "历史" in body
        for label in ("监测配置", "情报事件", "对比", "运行记录"):
            assert label not in body

    def test_token_usage_is_not_shown_in_simple_mode(
        self, client, session, engine_ready, topic
    ):
        from aios.models import LLMUsage

        session.add(
            LLMUsage(
                purpose="research", provider_id="deepseek", model="deepseek-chat",
                total_tokens=12345, latency_ms=900,
            )
        )
        session.commit()
        body = client.get("/").text
        assert "12345" not in body
        assert "token" not in body.lower()


class TestSimpleDetailPages:
    def test_unknown_ids_return_404(self, client):
        assert client.get("/simple/changes/99999").status_code == 404
        assert client.get("/simple/sources/99999").status_code == 404

    def test_history_is_reachable_with_no_data(self, client):
        response = client.get("/simple/history")
        assert response.status_code == 200
        assert "还没有历史报告" in response.text

    def test_every_simple_detail_page_offers_a_way_back(self, client, session):
        from aios.models import Report

        report = Report(report_date=dt.date(2026, 9, 20), title="T", model="m")
        session.add(report)
        session.commit()

        for url in (
            f"/simple/changes/{report.id}",
            f"/simple/sources/{report.id}",
            "/simple/history",
        ):
            body = client.get(url).text
            assert "返回" in body, url

    def test_a_first_report_says_there_is_nothing_to_compare(self, client, session):
        from aios.models import Report

        report = Report(report_date=dt.date(2026, 9, 20), title="T", model="m")
        session.add(report)
        session.commit()

        body = client.get(f"/simple/changes/{report.id}").text
        assert "第一期" in body
