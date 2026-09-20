"""The global 简易版 / 本地专业版 switch.

The contract being protected here is narrow and important: switching modes
changes *workflow and presentation only*. It must never touch monitoring
configuration, events, reports or credentials - both modes are views onto one
database, not two products.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.services import mode_service


def _mode(session) -> str:
    session.expire_all()
    return mode_service.get_mode(session)


class TestModePreference:
    def test_simple_is_the_default_for_a_fresh_install(self, session):
        assert mode_service.get_mode(session) == mode_service.MODE_SIMPLE

    def test_preference_persists(self, client, session):
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        assert _mode(session) == mode_service.MODE_PROFESSIONAL

        client.post("/mode", data={"mode": "simple", "next": "/"}, follow_redirects=False)
        assert _mode(session) == mode_service.MODE_SIMPLE

    def test_preference_survives_a_new_client(self, client, session, db):
        """A restart must not lose the mode - it is stored, not in-memory."""
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )

        from fastapi.testclient import TestClient

        from aios.app import create_app

        with TestClient(create_app()) as fresh:
            body = fresh.get("/").text
        assert "app-shell" in body
        assert "simple-shell" not in body

    def test_unknown_mode_falls_back_to_the_default(self, client, session):
        client.post("/mode", data={"mode": "nonsense", "next": "/"}, follow_redirects=False)
        assert _mode(session) == mode_service.DEFAULT_MODE

    @pytest.mark.parametrize(
        "target",
        ["https://evil.example.com/", "//evil.example.com/", "/x\\y", "javascript:alert(1)"],
    )
    def test_next_cannot_become_an_open_redirect(self, client, target):
        response = client.post(
            "/mode", data={"mode": "simple", "next": target}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/?")


class TestShells:
    def test_simple_mode_renders_the_minimal_shell(self, client):
        body = client.get("/").text
        assert "simple-shell" in body
        assert "智能情报研究台" in body
        # None of the professional navigation is present.
        for label in ("监测配置", "情报事件", "对比", "运行记录"):
            assert label not in body

    def test_professional_mode_renders_the_existing_navigation(self, client):
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        body = client.get("/").text
        assert "app-shell" in body
        for label in ("概览", "监测配置", "情报事件", "报告", "对比", "运行记录", "设置"):
            assert label in body

    def test_the_switch_is_present_in_both_shells(self, client):
        assert "mode-switch" in client.get("/").text
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        assert "mode-switch" in client.get("/").text

    def test_the_switch_is_available_on_a_report_page(self, client, session):
        """Available there, but not visually dominant - see the CSS notes."""
        from aios.models import Report

        report = Report(report_date=dt.date(2026, 9, 20), title="T", model="m")
        session.add(report)
        session.commit()

        body = client.get(f"/reports/{report.id}").text
        assert "mode-switch" in body

    def test_dashboard_remains_directly_addressable(self, client):
        assert client.get("/dashboard").status_code == 200

    def test_simple_home_is_reachable_from_professional_mode(self, client):
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        assert client.get("/simple").status_code == 200


class TestSwitchingIsNonDestructive:
    def test_switching_does_not_modify_monitoring_data(self, client, session):
        """Modules, topics and queries are untouched by a mode change."""
        from aios.repositories import modules as modules_repo

        def snapshot():
            session.expire_all()
            return [
                (
                    m.key,
                    m.name,
                    m.enabled,
                    tuple(
                        (t.name, tuple(q.query for q in t.queries))
                        for t in m.topics
                    ),
                )
                for m in modules_repo.list_modules(session, include_archived=True)
            ]

        before = snapshot()
        assert before, "the seeded configuration should not be empty"

        for mode in ("professional", "simple", "professional", "simple"):
            client.post("/mode", data={"mode": mode, "next": "/"}, follow_redirects=False)

        assert snapshot() == before

    def test_switching_does_not_touch_events_or_reports(self, client, session, make_event):
        from aios.models import Report
        from aios.repositories import events as events_repo
        from aios.repositories import reports as reports_repo

        make_event(session, title="模式切换测试事件")
        session.add(Report(report_date=dt.date(2026, 9, 20), title="T", model="m"))
        session.commit()

        events_before = events_repo.count_events(session)
        reports_before = reports_repo.count_reports(session)

        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        client.post("/mode", data={"mode": "simple", "next": "/"}, follow_redirects=False)

        session.expire_all()
        assert events_repo.count_events(session) == events_before
        assert reports_repo.count_reports(session) == reports_before

    def test_switching_does_not_touch_provider_configuration(self, client, session):
        from aios.repositories import providers as providers_repo

        before = [
            (p.provider_id, p.default_model, p.enabled, p.is_default, p.has_api_key)
            for p in providers_repo.list_providers(session)
        ]

        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        client.post("/mode", data={"mode": "simple", "next": "/"}, follow_redirects=False)

        session.expire_all()
        after = [
            (p.provider_id, p.default_model, p.enabled, p.is_default, p.has_api_key)
            for p in providers_repo.list_providers(session)
        ]
        assert after == before

    def test_switching_writes_only_the_mode_setting(self, client, session):
        """Belt and braces: compare the whole settings table either side."""
        from aios.services import settings_service

        before = settings_service.all_settings(session)
        client.post(
            "/mode", data={"mode": "professional", "next": "/"}, follow_redirects=False
        )
        session.expire_all()
        after = settings_service.all_settings(session)

        changed = {
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        }
        assert changed == {mode_service.SETTING_KEY}


class TestModeAwareLanding:
    def test_switching_to_simple_from_a_professional_page_lands_home(self, client):
        response = client.post(
            "/mode", data={"mode": "simple", "next": "/monitoring"}, follow_redirects=False
        )
        assert response.headers["location"].startswith("/?")

    def test_switching_to_professional_from_a_simple_page_lands_home(self, client):
        response = client.post(
            "/mode",
            data={"mode": "professional", "next": "/simple/history"},
            follow_redirects=False,
        )
        assert response.headers["location"].startswith("/?")

    def test_a_shared_page_keeps_the_user_where_they_were(self, client, session):
        """A report exists in both modes, so the switch should not navigate."""
        from aios.models import Report

        report = Report(report_date=dt.date(2026, 9, 20), title="T", model="m")
        session.add(report)
        session.commit()

        response = client.post(
            "/mode",
            data={"mode": "professional", "next": f"/reports/{report.id}"},
            follow_redirects=False,
        )
        assert response.headers["location"].startswith(f"/reports/{report.id}?")


class TestReportNavigation:
    @pytest.fixture
    def report_id(self, session):
        from aios.models import Report, ReportSection

        report = Report(report_date=dt.date(2026, 9, 20), title="研究报告", model="m")
        session.add(report)
        session.flush()
        session.add(
            ReportSection(
                report_id=report.id, module_name="移动智能终端侧", module_key="mobile",
                status="watch",
            )
        )
        session.commit()
        return report.id

    def test_report_links_work_from_simple_mode(self, client, report_id):
        assert client.get(f"/reports/{report_id}?from=simple").status_code == 200

    def test_report_page_offers_return_to_home(self, client, report_id):
        body = client.get(f"/reports/{report_id}?from=simple").text
        assert "返回主页" in body
        assert 'href="/"' in body

    def test_report_opened_from_simple_uses_the_simple_shell(self, client, report_id):
        body = client.get(f"/reports/{report_id}?from=simple").text
        assert "simple-shell" in body
        assert "监测配置" not in body

    def test_report_opened_normally_keeps_the_professional_breadcrumb(
        self, client, report_id
    ):
        body = client.get(f"/reports/{report_id}").text
        assert "breadcrumb" in body
        assert "返回主页" not in body

    def test_view_tabs_preserve_the_simple_origin(self, client, report_id):
        """Switching to 证据 must not lose the way back to the Simple home.

        The ``&`` is HTML-escaped in the rendered attribute, which is correct -
        the assertion matches what the browser actually receives.
        """
        body = client.get(f"/reports/{report_id}?from=simple").text
        assert f"/reports/{report_id}?view=evidence&amp;from=simple" in body
