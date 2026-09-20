"""HTTP layer: every page renders, every button does something real."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aios.repositories import modules as modules_repo
from aios.repositories import topics as topics_repo


def flash_of(response) -> str:
    """The redirect target of a POST, used to assert on outcome."""
    return response.headers.get("location", "")


class TestPagesRender:
    @pytest.mark.parametrize(
        "path",
        ["/", "/dashboard", "/monitoring", "/reports", "/compare", "/events", "/runs",
         "/settings", "/healthz"],
    )
    def test_page_returns_200(self, client, path):
        assert client.get(path).status_code == 200

    def test_dashboard_lists_module_count(self, client):
        # "/" is mode-aware as of v2.2 and serves 简易版 by default, so the
        # Overview is asserted against the route that always renders it.
        assert "8" in client.get("/dashboard").text

    def test_monitoring_lists_all_seeded_modules(self, client):
        body = client.get("/monitoring").text
        for name in ["移动智能终端侧", "PC 侧", "服务器侧", "智算超节点侧",
                     "物联网侧", "无人飞行器侧", "具身智能侧", "太空智算侧"]:
            assert name in body

    def test_module_page_shows_topics_and_queries(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        body = client.get(f"/monitoring/modules/{module.id}").text
        assert "HarmonyOS" in body
        assert "Android" in body

    def test_topic_page_shows_its_queries(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        topic = module.topics[0]
        body = client.get(f"/monitoring/topics/{topic.id}").text
        assert "HarmonyOS Agent Framework Kit" in body

    def test_unknown_ids_return_404(self, client):
        assert client.get("/monitoring/modules/99999").status_code == 404
        assert client.get("/monitoring/topics/99999").status_code == 404
        assert client.get("/reports/99999").status_code == 404
        assert client.get("/events/99999").status_code == 404
        assert client.get("/runs/99999").status_code == 404

    def test_404_page_is_styled(self, client):
        response = client.get("/no-such-page")
        assert response.status_code == 404
        assert "返回概览" in response.text


class TestMonitoringCRUD:
    def test_add_topic_and_queries_without_touching_python(self, client, session):
        """Acceptance step 5/6: new topic + queries take effect with no code edit."""
        module = modules_repo.get_module_by_key(session, "mobile")
        before = module.query_count

        response = client.post(
            f"/monitoring/modules/{module.id}/topics",
            data={"name": "AI Browser", "description": "Agent 浏览器"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        topic_id = int(flash_of(response).split("?")[0].rsplit("/", 1)[1])

        for query in ["OpenAI browser agent", "Gemini Chrome agent", "Perplexity Comet"]:
            assert client.post(
                f"/monitoring/topics/{topic_id}/queries",
                data={"query": query},
                follow_redirects=False,
            ).status_code == 303

        session.expire_all()
        module = modules_repo.get_module_by_key(session, "mobile")
        assert module.query_count == before + 3

        # And the next run's plan really includes them.
        from aios.services.pipeline import build_plan

        plan = next(p for p in build_plan(session) if p.key == "mobile")
        topic_plan = next(t for t in plan.topics if t.name == "AI Browser")
        assert set(topic_plan.queries) == {
            "OpenAI browser agent", "Gemini Chrome agent", "Perplexity Comet"
        }

    def test_create_and_edit_module(self, client, session):
        response = client.post(
            "/monitoring/modules",
            data={"key": "ai_browser", "name": "AI Browser 侧", "description": "d",
                  "lookback_days": "5", "max_candidates": "20", "max_report_items": "3"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        module_id = int(flash_of(response).split("?")[0].rsplit("/", 1)[1])

        client.post(
            f"/monitoring/modules/{module_id}",
            data={"key": "ai_browser", "name": "改名后的模块", "description": "d2",
                  "enabled": "on", "lookback_days": "4", "max_candidates": "22",
                  "max_report_items": "2", "analysis_prompt": "关注 agent", "sort_order": "90"},
            follow_redirects=False,
        )
        session.expire_all()
        module = modules_repo.get_module(session, module_id)
        assert module.name == "改名后的模块"
        assert module.lookback_days == 4
        assert module.analysis_prompt == "关注 agent"

    def test_duplicate_module_key_is_rejected(self, client):
        data = {"key": "mobile", "name": "冲突", "lookback_days": "3",
                "max_candidates": "18", "max_report_items": "2"}
        assert "lvl=error" in flash_of(
            client.post("/monitoring/modules", data=data, follow_redirects=False)
        )

    def test_invalid_module_key_is_rejected(self, client):
        data = {"key": "Has Spaces!!", "name": "x", "lookback_days": "3",
                "max_candidates": "18", "max_report_items": "2"}
        assert "lvl=error" in flash_of(
            client.post("/monitoring/modules", data=data, follow_redirects=False)
        )

    def test_toggle_module(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        client.post(f"/monitoring/modules/{module.id}/toggle", follow_redirects=False)
        session.expire_all()
        assert modules_repo.get_module(session, module.id).enabled is False

    def test_archive_is_soft_delete(self, client, session):
        module = modules_repo.get_module_by_key(session, "space")
        client.post(f"/monitoring/modules/{module.id}/archive", follow_redirects=False)
        session.expire_all()
        assert modules_repo.get_module(session, module.id).archived is True
        assert modules_repo.count_modules(session) == 7
        assert modules_repo.count_modules(session, include_archived=True) == 8

    def test_move_changes_order(self, client, session):
        before = [m.key for m in modules_repo.list_modules(session)]
        second = modules_repo.get_module_by_key(session, before[1])
        client.post(
            f"/monitoring/modules/{second.id}/move", data={"direction": "up"},
            follow_redirects=False,
        )
        session.expire_all()
        after = [m.key for m in modules_repo.list_modules(session)]
        assert after[0] == before[1]

    def test_query_lifecycle(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        topic = module.topics[0]
        client.post(
            f"/monitoring/topics/{topic.id}/queries",
            data={"query": "一条新的检索式"}, follow_redirects=False,
        )
        session.expire_all()
        query = next(
            q for q in topics_repo.get_topic(session, topic.id).queries
            if q.query == "一条新的检索式"
        )

        client.post(f"/monitoring/queries/{query.id}/toggle", follow_redirects=False)
        session.expire_all()
        assert topics_repo.get_query(session, query.id).enabled is False

        client.post(
            f"/monitoring/queries/{query.id}",
            data={"query": "修改后的检索式", "priority": "7"}, follow_redirects=False,
        )
        session.expire_all()
        assert topics_repo.get_query(session, query.id).query == "修改后的检索式"

        query_id = query.id
        client.post(f"/monitoring/queries/{query_id}/delete", follow_redirects=False)
        session.expunge_all()
        assert topics_repo.get_query(session, query_id) is None

    def test_empty_query_is_rejected(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        topic = module.topics[0]
        response = client.post(
            f"/monitoring/topics/{topic.id}/queries", data={"query": "   "},
            follow_redirects=False,
        )
        assert response.status_code in (303, 422)

    def test_preferred_source_is_normalised(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        topic = module.topics[0]
        client.post(
            f"/monitoring/topics/{topic.id}/sources",
            data={"domain": "https://www.OpenAI.com/blog/"}, follow_redirects=False,
        )
        session.expire_all()
        domains = [p.domain for p in topics_repo.get_topic(session, topic.id).preferred_sources]
        assert "openai.com" in domains

    def test_bad_domain_is_rejected(self, client, session):
        module = modules_repo.get_module_by_key(session, "mobile")
        topic = module.topics[0]
        assert "lvl=error" in flash_of(
            client.post(
                f"/monitoring/topics/{topic.id}/sources",
                data={"domain": "not-a-domain"}, follow_redirects=False,
            )
        )


class TestSettingsPage:
    """AI provider settings moved to /settings/ai; the invariants did not."""

    def test_settings_page_links_to_ai_config(self, client):
        body = client.get("/settings").text
        assert "/settings/ai" in body
        assert "AI 模型" in body

    def test_shows_unconfigured_state(self, client, monkeypatch):
        from aios.services import keyring_service

        monkeypatch.setattr(keyring_service, "get_provider_key", lambda pid: None)
        body = client.get("/settings/ai").text
        assert "未完成配置" in body or "还没有配置" in body

    def test_masked_key_only(self, client, session):
        """The browser must never receive the full key."""
        from aios.repositories import providers as providers_repo

        secret = "sk-abcdefghijklmnop6307"
        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.has_api_key = True
        row.api_key_last_four = "6307"
        session.commit()

        body = client.get("/settings/ai").text
        assert secret not in body
        assert "6307" in body

    def test_save_model_settings(self, client, session):
        from aios.repositories import providers as providers_repo

        client.post(
            "/settings/ai/providers",
            data={"provider_id": "deepseek", "display_name": "DeepSeek",
                  "base_url": "https://api.deepseek.com",
                  "default_model": "deepseek-reasoner",
                  "enabled": "on", "temperature": "0.3", "max_tokens": "5000",
                  "timeout_seconds": "200", "retries": "2"},
            follow_redirects=False,
        )
        session.expire_all()
        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert row.default_model == "deepseek-reasoner"
        assert row.max_tokens == 5000

    def test_arbitrary_future_model_name_accepted(self, client, session):
        """Model names must not be hard-coded to a fixed list."""
        from aios.repositories import providers as providers_repo

        client.post(
            "/settings/ai/providers",
            data={"provider_id": "qwen", "display_name": "通义千问",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "default_model": "qwen4-ultra-2030", "enabled": "on",
                  "temperature": "0.2", "max_tokens": "4000",
                  "timeout_seconds": "180", "retries": "3"},
            follow_redirects=False,
        )
        session.expire_all()
        row = providers_repo.get_by_provider_id(session, "qwen")
        assert row.default_model == "qwen4-ultra-2030"

    def test_bad_base_url_rejected(self, client):
        assert "lvl=error" in flash_of(
            client.post(
                "/settings/ai/providers",
                data={"provider_id": "deepseek", "base_url": "ftp://nope",
                      "default_model": "m", "temperature": "0.2", "max_tokens": "4000",
                      "timeout_seconds": "180", "retries": "3"},
                follow_redirects=False,
            )
        )

    def test_connection_test_reports_failure_without_leaking(
        self, client, session, monkeypatch
    ):
        from aios.repositories import providers as providers_repo
        from aios.routers import providers as providers_router

        secret = "sk-secret1234567890"
        monkeypatch.setattr(
            providers_router,
            "test_provider_row",
            lambda row, **kw: {"ok": False, "error": "HTTP 401", "latency_ms": 5},
        )
        row = providers_repo.get_by_provider_id(session, "deepseek")
        location = flash_of(
            client.post(f"/settings/ai/providers/{row.id}/test", follow_redirects=False)
        )
        assert "lvl=error" in location
        assert secret not in location

    def test_connection_test_success(self, client, session, monkeypatch):
        from aios.repositories import providers as providers_repo
        from aios.routers import providers as providers_router

        monkeypatch.setattr(
            providers_router,
            "test_provider_row",
            lambda row, **kw: {"ok": True, "model": "deepseek-chat", "latency_ms": 321},
        )
        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert "lvl=ok" in flash_of(
            client.post(f"/settings/ai/providers/{row.id}/test", follow_redirects=False)
        )

    def test_masked_placeholder_is_rejected(self, client, session):
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert "lvl=error" in flash_of(
            client.post(
                f"/settings/ai/providers/{row.id}/key",
                data={"api_key": "\u2022" * 12 + "6307"},
                follow_redirects=False,
            )
        )

    def test_scheduler_settings_persist(self, client, session, monkeypatch):
        from aios.services import scheduler as scheduler_module
        from aios.services.settings_service import get_bool, get_str

        monkeypatch.setattr(scheduler_module.scheduler, "reload", lambda: "2026-09-16 06:30")
        client.post(
            "/settings/scheduler", data={"enabled": "on", "time": "6:30"},
            follow_redirects=False,
        )
        session.expire_all()
        assert get_bool(session, "scheduler_enabled") is True
        assert get_str(session, "scheduler_time") == "06:30"

    def test_bad_scheduler_time_rejected(self, client):
        assert "lvl=error" in flash_of(
            client.post(
                "/settings/scheduler", data={"enabled": "on", "time": "99:99"},
                follow_redirects=False,
            )
        )

    def test_backup_creates_archive(self, client, db):
        assert "lvl=ok" in flash_of(client.post("/settings/backup", follow_redirects=False))
        archives = list((db.data_dir / "backups").glob("aios-backup-*.zip"))
        assert len(archives) == 1

        import zipfile

        with zipfile.ZipFile(archives[0]) as bundle:
            assert "aios.db" in bundle.namelist()

    def test_backup_download_rejects_traversal(self, client):
        response = client.get("/settings/backup/..%2F..%2Faios.db", follow_redirects=False)
        assert response.status_code in (303, 404)


class TestRunControls:
    def test_run_without_key_redirects_to_settings(self, client):
        """A fresh install has a provider row but no credential yet."""
        location = flash_of(client.post("/run", follow_redirects=False))
        assert "/settings/ai" in location

    def test_run_starts_when_key_present(self, client, session, monkeypatch):
        from aios.repositories import providers as providers_repo
        from aios.services import run_manager

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.has_api_key = True
        row.api_key_last_four = "6307"
        row.default_model = "deepseek-chat"
        session.commit()

        started = {}

        def fake_start(self, trigger_type="manual", report_date=None, **kwargs):
            started["trigger"] = trigger_type
            return 42

        # Patched on the class rather than the ``manager`` instance: monkeypatch
        # restores an instance attribute by assigning the bound method it
        # captured, which permanently shadows the class attribute on this
        # process-wide singleton and defeats later tests that patch it.
        monkeypatch.setattr(run_manager.RunManager, "start_run", fake_start)
        location = flash_of(client.post("/run", follow_redirects=False))
        assert "/runs/42" in location
        assert started["trigger"] == "manual"

    def test_status_fragment_is_pollable(self, client):
        response = client.get("/dashboard/status")
        assert response.status_code == 200
        assert "run-status" in response.text


class TestReportsAndCompare:
    def _make_report(self, session, date, title="日报"):
        from aios.models import Report, ReportSection

        report = Report(report_date=date, title=title, model="test",
                        headline_json={"title": "H", "body": "B"}, trends_json=[], metrics_json=[])
        session.add(report)
        session.flush()
        session.add(ReportSection(report_id=report.id, module_name="移动智能终端侧",
                                  module_key="mobile", status="watch"))
        session.flush()
        return report

    def test_report_list_and_detail(self, client, session):
        report = self._make_report(session, dt.date(2026, 9, 15))
        session.commit()

        assert "2026-09-15" in client.get("/reports").text
        for view in ("report", "evidence", "events"):
            assert client.get(f"/reports/{report.id}?view={view}").status_code == 200

    def test_report_list_shows_real_counts(self, client, session):
        """Guards a Jinja trap: a context key named `items` resolves to
        dict.items and renders as a bound-method repr instead of the number."""
        from aios.models import ReportItem

        report = self._make_report(session, dt.date(2026, 9, 15))
        section = report.sections[0]
        for n in range(3):
            session.add(ReportItem(
                report_section_id=section.id, title=f"条目 {n}",
                fact_summary="事实。", confidence="high", sort_order=n))
        session.commit()

        body = client.get("/reports").text
        assert "built-in method" not in body
        assert "<built-in" not in body
        compact = "".join(body.split())
        assert ">3<" in compact

    def test_report_html_and_json_endpoints(self, client, session):
        report = self._make_report(session, dt.date(2026, 9, 15))
        session.commit()

        html = client.get(f"/reports/{report.id}/html")
        assert html.status_code == 200
        assert "全球智能终端操作系统监测日报" in html.text

        audit = client.get(f"/reports/{report.id}/json")
        assert audit.status_code == 200
        payload = audit.json()
        assert payload["report_date"] == "2026-09-15"
        assert payload["schema_version"] == 2

    def test_export_writes_files(self, client, session, db):
        report = self._make_report(session, dt.date(2026, 9, 15))
        session.commit()
        client.post(f"/reports/{report.id}/export", follow_redirects=False)
        assert (db.reports_dir / "2026-09-15.html").exists()
        assert (db.reports_dir / "2026-09-15.json").exists()

    def test_compare_with_no_reports_explains_itself(self, client):
        body = client.get("/compare").text
        assert "还没有任何报告" in body

    def test_compare_defaults_to_two_most_recent(self, client, session):
        self._make_report(session, dt.date(2026, 9, 14))
        self._make_report(session, dt.date(2026, 9, 15))
        session.commit()
        body = client.get("/compare").text
        assert "2026-09-14" in body and "2026-09-15" in body

    def test_compare_rejects_bad_dates(self, client, session):
        self._make_report(session, dt.date(2026, 9, 15))
        session.commit()
        body = client.get("/compare?date_a=nonsense&date_b=2026-09-15").text
        assert "YYYY-MM-DD" in body

    def test_compare_reports_missing_date(self, client, session):
        self._make_report(session, dt.date(2026, 9, 15))
        session.commit()
        body = client.get("/compare?date_a=2020-01-01&date_b=2026-09-15").text
        assert "2020-01-01" in body

    def test_report_delete_keeps_events(self, client, session, make_event):
        report = self._make_report(session, dt.date(2026, 9, 15))
        make_event(session, title="保留的事件")
        session.commit()

        from aios.repositories import events as events_repo

        client.post(f"/reports/{report.id}/delete", follow_redirects=False)
        session.expire_all()
        assert events_repo.count_events(session) == 1


class TestEventViews:
    def test_event_list_and_detail(self, client, session, make_event):
        event, _ = make_event(session, title="HarmonyOS 生态")
        session.commit()

        assert "HarmonyOS 生态" in client.get("/events").text
        detail = client.get(f"/events/{event.id}")
        assert detail.status_code == 200
        assert "时间线" in detail.text

    def test_event_search(self, client, session, make_event):
        make_event(session, title="鸿蒙设备数量", event_key="k1")
        make_event(session, title="卫星算力测试", event_key="k2")
        session.commit()

        body = client.get("/events?q=鸿蒙").text
        assert "鸿蒙设备数量" in body
        assert "卫星算力测试" not in body

    def test_event_status_update(self, client, session, make_event):
        event, _ = make_event(session, title="可结束的事件")
        session.commit()
        client.post(f"/events/{event.id}/status", data={"status": "resolved"},
                    follow_redirects=False)
        session.expire_all()

        from aios.repositories import events as events_repo

        assert events_repo.get_event(session, event.id).status == "resolved"

    def test_invalid_status_rejected(self, client, session, make_event):
        event, _ = make_event(session, title="事件")
        session.commit()
        assert "lvl=error" in flash_of(
            client.post(f"/events/{event.id}/status", data={"status": "bogus"},
                        follow_redirects=False)
        )


class TestEscaping:
    def test_user_input_is_escaped(self, client, session):
        """Configuration text is user input and must never render as markup."""
        payload = '<script>alert("xss")</script>'
        client.post(
            "/monitoring/modules",
            data={"key": "xss_test", "name": payload, "description": payload,
                  "lookback_days": "3", "max_candidates": "18", "max_report_items": "2"},
            follow_redirects=False,
        )
        body = client.get("/monitoring").text
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body

    def test_report_html_escapes_content(self, session, db):
        from aios.models import Report, ReportItem, ReportSection
        from aios.services.report_generator import render_html
        from aios.repositories import reports as reports_repo

        report = Report(report_date=dt.date(2026, 9, 15), title="T", model="m")
        session.add(report)
        session.flush()
        section = ReportSection(report_id=report.id, module_name="<b>模块</b>",
                                module_key="m", status="new")
        session.add(section)
        session.flush()
        session.add(ReportItem(report_section_id=section.id, title='<img src=x onerror=1>',
                               fact_summary="<script>bad()</script>", confidence="high"))
        session.commit()

        html = render_html(reports_repo.get_report(session, report.id))
        assert "<script>bad()" not in html
        assert "&lt;script&gt;" in html
        assert "onerror=1>" not in html
