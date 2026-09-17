"""Ground truth for "no dead buttons".

Every URL the templates reference is requested for real. A 404 or 405 means the
template points at a route that does not exist; 422 is acceptable because it
proves the route exists and merely rejected empty form data.
"""

from __future__ import annotations

import datetime as dt

import pytest

DEAD = {404, 405}


@pytest.fixture
def seeded(client, session, make_event):
    """One of everything, so every id-bearing URL is reachable."""
    from aios.models import Report, ReportSection
    from aios.repositories import modules as modules_repo
    from aios.repositories import runs as runs_repo
    from aios.repositories import topics as topics_repo

    module = modules_repo.get_module_by_key(session, "mobile")
    topic = module.topics[0]
    query = topic.queries[0]
    source = module.preferred_sources[0]
    keyword = topics_repo.add_excluded_keyword(session, "测试排除词", topic_id=topic.id)

    event, observation = make_event(session, title="路由测试事件")

    report = Report(report_date=dt.date(2026, 9, 15), title="T", model="m")
    session.add(report)
    session.flush()
    session.add(
        ReportSection(report_id=report.id, module_name="移动智能终端侧",
                      module_key="mobile", status="watch")
    )
    run = runs_repo.create_run(session, dt.date(2026, 9, 15), "manual")
    session.commit()

    from aios.repositories import providers as providers_repo
    from aios.services import network_service

    provider = providers_repo.get_by_provider_id(session, "deepseek")
    feed = network_service.add_feed(session, "https://routes.example.com/feed.xml", "路由测试")
    session.commit()

    return {
        "feed_id": feed.id,
        "module_id": module.id,
        "topic_id": topic.id,
        "query_id": query.id,
        "source_id": source.id,
        "keyword_id": keyword.id,
        "event_id": event.id,
        "report_id": report.id,
        "run_id": run.id,
        "provider_id": provider.id,
    }


def test_every_get_route_exists(client, seeded):
    ids = seeded
    urls = [
        "/",
        "/dashboard/status",
        "/monitoring",
        f"/monitoring/modules/{ids['module_id']}",
        f"/monitoring/topics/{ids['topic_id']}",
        "/reports",
        f"/reports/{ids['report_id']}",
        f"/reports/{ids['report_id']}?view=report",
        f"/reports/{ids['report_id']}?view=evidence",
        f"/reports/{ids['report_id']}?view=events",
        f"/reports/{ids['report_id']}/html",
        f"/reports/{ids['report_id']}/json",
        f"/reports/{ids['report_id']}/audit",
        "/compare",
        "/compare?date_a=2026-09-14&date_b=2026-09-15",
        "/events",
        "/events?q=x&status=active&module_id=0",
        f"/events/{ids['event_id']}",
        "/runs",
        f"/runs/{ids['run_id']}",
        f"/runs/{ids['run_id']}/status",
        f"/runs/{ids['run_id']}/live",
        f"/runs/{ids['run_id']}/logs?after=0",
        "/monitoring/ai/new",
        f"/monitoring/ai/modules/{ids['module_id']}/topic",
        "/settings",
        "/settings/ai",
        "/healthz",
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/js/htmx.min.js",
    ]
    dead = [u for u in urls if client.get(u).status_code in DEAD]
    assert not dead, f"dead GET routes: {dead}"


def test_every_post_route_exists(client, seeded, monkeypatch):
    """POST targets must resolve. Empty bodies may 422; they must not 404."""
    from aios.routers import providers as providers_router
    from aios.services import keyring_service, run_manager

    monkeypatch.setattr(keyring_service, "get_api_key", lambda **k: "sk-" + "x" * 20)
    monkeypatch.setattr(run_manager.manager, "start_run", lambda **k: seeded["run_id"])
    # Never reach the network from a route-existence check.
    monkeypatch.setattr(
        providers_router, "test_provider_row",
        lambda row, **kw: {"ok": True, "model": "m", "latency_ms": 1},
    )

    from aios.routers import config_ai
    from aios.services import network_service
    from aios.services.config_generator import ConfigGenerationError

    class OfflineGenerator:
        """Proves the route exists without letting it call a provider."""

        def __init__(self, *args, **kwargs):
            pass

        def readiness_error(self) -> str:
            return ""

        def generate_module(self, *args, **kwargs):
            raise ConfigGenerationError("route check: generation not attempted")

        generate_topic = generate_module

    monkeypatch.setattr(config_ai.config_generator, "ConfigGenerator", OfflineGenerator)
    monkeypatch.setattr(network_service, "run_diagnostics", lambda *a, **k: [])

    ids = seeded
    posts = [
        ("/run", {}),
        (f"/runs/{ids['run_id']}/cancel", {}),
        ("/monitoring/modules", {"key": "route_test", "name": "路由测试",
                                 "lookback_days": "3", "max_candidates": "18",
                                 "max_report_items": "2"}),
        (f"/monitoring/modules/{ids['module_id']}",
         {"key": "mobile", "name": "移动智能终端侧", "description": "",
          "enabled": "on", "lookback_days": "3", "max_candidates": "18",
          "max_report_items": "2", "analysis_prompt": "", "sort_order": "10"}),
        (f"/monitoring/modules/{ids['module_id']}/toggle", {}),
        (f"/monitoring/modules/{ids['module_id']}/move", {"direction": "up"}),
        (f"/monitoring/modules/{ids['module_id']}/sources", {"domain": "example.com"}),
        (f"/monitoring/modules/{ids['module_id']}/topics", {"name": "路由测试主题"}),
        (f"/monitoring/topics/{ids['topic_id']}", {"name": "HarmonyOS", "description": "",
                                                   "enabled": "on", "analysis_prompt": ""}),
        (f"/monitoring/topics/{ids['topic_id']}/toggle", {}),
        (f"/monitoring/topics/{ids['topic_id']}/queries", {"query": "路由测试检索式"}),
        (f"/monitoring/topics/{ids['topic_id']}/sources", {"domain": "example.org"}),
        (f"/monitoring/topics/{ids['topic_id']}/exclusions", {"keyword": "测试"}),
        (f"/monitoring/queries/{ids['query_id']}", {"query": "更新", "priority": "0"}),
        (f"/monitoring/queries/{ids['query_id']}/toggle", {}),
        (f"/monitoring/sources/{ids['source_id']}/delete", {"back": "/monitoring"}),
        (f"/monitoring/exclusions/{ids['keyword_id']}/delete", {"back": "/monitoring"}),
        (f"/events/{ids['event_id']}/status", {"status": "watching"}),
        (f"/reports/{ids['report_id']}/export", {}),
        (f"/settings/ai/providers/{ids['provider_id']}/test", {}),
        ("/settings/ai/providers", {"provider_id": "deepseek", "display_name": "DeepSeek",
                                     "base_url": "https://api.deepseek.com",
                                     "default_model": "deepseek-chat", "enabled": "on",
                                     "temperature": "0.2", "max_tokens": "4000",
                                     "timeout_seconds": "180", "retries": "3"}),
        (f"/settings/ai/providers/{ids['provider_id']}/key",
         {"api_key": "sk-" + "z" * 20}),
        (f"/settings/ai/providers/{ids['provider_id']}/toggle", {}),
        (f"/settings/ai/providers/{ids['provider_id']}/default", {}),
        ("/settings/ai/routes", {"route_topic_analysis": "", "model_topic_analysis": "",
                                 "route_event_matching": "", "model_event_matching": "",
                                 "route_synthesis": "", "model_synthesis": ""}),
        ("/settings/collection", {"default_lookback_days": "3", "default_max_candidates": "18",
                                  "article_max_chars": "7000", "http_timeout": "20",
                                  "collect_workers": "6", "fetch_body_top_n": "8",
                                  "event_match_lookback_days": "45", "report_output_dir": ""}),
        ("/settings/scheduler", {"time": "06:00"}),
        ("/settings/backup", {}),
        ("/settings/network/mode", {"mode": "auto"}),
        ("/settings/network/collectors",
         {"collector_gdelt_enabled": "on", "collector_rss_enabled": "on",
          "gdelt_min_interval_seconds": "2.0",
          "collection_cache_ttl_minutes": "30"}),
        ("/settings/network/transport", {"connection_mode": "system"}),
        ("/settings/network/feeds", {"url": "https://example.com/feed.xml",
                                     "title": "路由测试订阅源"}),
        ("/settings/network/diagnose", {}),
        ("/monitoring/ai/generate", {"description": "监测某个领域的动态。"}),
        (f"/monitoring/ai/modules/{ids['module_id']}/topic/generate",
         {"description": "新增一个主题。"}),
        ("/monitoring/ai/apply", {"topic_count": "0"}),
        (f"/monitoring/ai/modules/{ids['module_id']}/topic/apply", {"topic_count": "0"}),
    ]

    dead = []
    for url, data in posts:
        status = client.post(url, data=data, follow_redirects=False).status_code
        if status in DEAD:
            dead.append((url, status))
    assert not dead, f"dead POST routes: {dead}"


def test_destructive_routes_exist(client, seeded):
    """Delete/archive endpoints are checked last: they remove their target."""
    ids = seeded
    for url in [
        f"/settings/ai/providers/{ids['provider_id']}/key/delete",
        f"/settings/ai/providers/{ids['provider_id']}/delete",
        f"/monitoring/queries/{ids['query_id']}/delete",
        f"/monitoring/topics/{ids['topic_id']}/archive",
        f"/monitoring/modules/{ids['module_id']}/archive",
        f"/monitoring/modules/{ids['module_id']}/restore",
        f"/settings/network/feeds/{ids['feed_id']}/toggle",
        f"/settings/network/feeds/{ids['feed_id']}/delete",
        f"/reports/{ids['report_id']}/delete",
    ]:
        assert client.post(url, follow_redirects=False).status_code not in DEAD, url


def test_import_endpoint_accepts_a_file(client):
    import io
    import json

    payload = {
        "date": "2026-09-01",
        "sections": [{"section": "移动智能终端侧", "status": "watch",
                      "items": [], "metrics": [], "_evidence": []}],
        "overview": {"headline": {}, "trends": [], "metrics": []},
    }
    files = {"file": ("legacy.json", io.BytesIO(json.dumps(payload).encode()), "application/json")}
    response = client.post("/settings/import", files=files, follow_redirects=False)
    assert response.status_code not in DEAD
    assert response.status_code == 303


def test_backup_download_route_exists(client, db):
    client.post("/settings/backup", follow_redirects=False)
    archives = list((db.data_dir / "backups").glob("aios-backup-*.zip"))
    assert archives
    response = client.get(f"/settings/backup/{archives[0].name}", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_windows_startup_routes_exist(client, monkeypatch):
    """Enable/disable must resolve on every platform; the service reports support."""
    from aios.services import startup_service

    monkeypatch.setattr(
        startup_service, "enable",
        lambda: startup_service.StartupStatus(supported=True, enabled=True, command="x"),
    )
    monkeypatch.setattr(
        startup_service, "disable",
        lambda: startup_service.StartupStatus(supported=True, enabled=False),
    )
    for url in ("/settings/startup/enable", "/settings/startup/disable"):
        assert client.post(url, follow_redirects=False).status_code not in DEAD, url
