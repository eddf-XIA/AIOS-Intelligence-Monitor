"""End-to-end pipeline runs with every external call mocked.

Covers the acceptance path: run -> articles -> events -> observations ->
report -> HTML/JSON export, then a second day proving event continuity rather
than duplicate events.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import stub_collectors

from aios.models import ModuleRunStatus, RunStatus
from aios.services.collector import Candidate
from aios.services.llm import LLMError, LLMResponse
from aios.services.llm.service import Resolution


# --- doubles ----------------------------------------------------------------

class ScriptedClient:
    """LLMService stand-in that answers by ``purpose`` (the routing key)."""

    def __init__(self, *, topic_items=None, match_decision=None, fail_purposes=(), usage_sink=None):
        self.topic_items = topic_items if topic_items is not None else [self._default_item()]
        self.match_decision = match_decision or {
            "decision": "NEW_EVENT", "event_id": None, "confidence": 0.9, "reason": "new"
        }
        self.fail_purposes = set(fail_purposes)
        self.calls = []
        self.has_key = True
        self.usage_sink = usage_sink
        self.ready = True

    @staticmethod
    def _default_item():
        return {
            "tag": "操作系统AI化",
            "title": "HarmonyOS 7 生态发展",
            "fact_summary": "官方公布设备数量达到 8500 万台。",
            "assessment": "判断：生态扩张仍在继续，值得关注后续增速。",
            "importance": 1,
            "confidence": "high",
            "source_ids": [0],
            "structured_data": {
                "device_count": {"value": 85000000, "display": "8500万", "unit": "devices", "source_id": 0}
            },
        }

    # -- LLMService surface the pipeline depends on --------------------------

    def readiness_error(self) -> str:
        return "" if self.ready else "测试用：尚未配置 API Key，请在「设置 → AI 模型」中填写。"

    def describe_routing(self):
        return {
            task: Resolution(
                provider_id="test", display_name="Test Provider",
                model="test-model", config_id=1, from_route=False,
            )
            for task in ("topic_analysis", "event_matching", "synthesis")
        }

    def _emit_usage(self, purpose, success=True, error=""):
        if self.usage_sink:
            self.usage_sink({
                "purpose": purpose, "module_id": None, "topic_id": None,
                "provider_id": "test",
                "model": "test-model", "prompt_tokens": 100, "completion_tokens": 50,
                "total_tokens": 150, "latency_ms": 12, "attempts": 1,
                "success": success, "error_message": error,
            })

    def complete_json(self, messages, purpose="generic", **kwargs):
        self.calls.append(purpose)
        if purpose in self.fail_purposes:
            self._emit_usage(purpose, success=False, error="simulated")
            raise LLMError(f"simulated failure for {purpose}")

        if purpose == "topic_analysis":
            data = {"status": "new", "items": self.topic_items, "metrics": []}
        elif purpose == "module_analysis":
            data = {"status": "new", "summary": "本领域本期有新增动态。", "metrics": []}
        elif purpose == "event_matching":
            data = self.match_decision
        elif purpose == "synthesis":
            data = {
                "headline": {"title": "鸿蒙生态持续扩张", "body": "摘要文字。", "section": "移动智能终端侧"},
                "trends": [{"title": "趋势一：系统级 AI", "body": "研判文字。", "confidence": "high"}],
                "metrics": [{"value": "8500万", "label": "设备数量", "section": "移动智能终端侧"}],
            }
        else:
            data = {"status": "ok"}
        self._emit_usage(purpose)
        return LLMResponse(
            data=data, content="", model="test-model", provider_id="test",
            latency_ms=12, attempts=1,
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
        )


#: Deliberately unrelated headlines - near-identical titles would be collapsed
#: by the deduplicator, which is correct behaviour but unhelpful here.
CANDIDATE_TITLES = [
    "华为公布鸿蒙生态设备数量最新数据",
    "开发者大会发布 Agent 框架接口更新说明",
    "第三方机构统计移动操作系统市场份额",
    "供应链厂商披露终端出货计划安排",
]


def make_candidates(n=2, prefix="https://huawei.com/news"):
    return [
        Candidate(
            title=CANDIDATE_TITLES[i % len(CANDIDATE_TITLES)],
            url=f"{prefix}/{i}",
            domain="huawei.com",
            source="Huawei",
            snippet="摘要内容",
            published_at=dt.datetime(2026, 9, 15, 8, 0),
            published_raw="2026-09-15",
            collector="test",
        )
        for i in range(n)
    ]


@pytest.fixture
def piped(db, monkeypatch):
    """Patch collection, extraction and the LLM; return the scripted client."""
    from aios.services import pipeline as pipeline_module

    client = ScriptedClient()

    def factory(usage_sink=None):
        client.usage_sink = usage_sink
        return client

    monkeypatch.setattr(pipeline_module, "build_llm_service", factory)
    stub_collectors(monkeypatch, lambda query: make_candidates(2))
    monkeypatch.setattr(
        pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文内容。" * 30
    )
    return client


def only_mobile(session):
    """Shrink the plan to one module so tests stay fast and deterministic."""
    from aios.repositories import modules as modules_repo

    for module in modules_repo.list_modules(session):
        module.enabled = module.key == "mobile"
    mobile = modules_repo.get_module_by_key(session, "mobile")
    for index, topic in enumerate(mobile.topics):
        topic.enabled = index == 0
    session.commit()
    return mobile


# --- tests ------------------------------------------------------------------

class TestFullRun:
    def test_run_produces_report_events_and_files(self, db, piped, session):
        from aios.database import session_scope
        from aios.repositories import articles as articles_repo
        from aios.repositories import events as events_repo
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        with session_scope() as s:
            run = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual")
            run_id = run.id

        status = MonitoringPipeline(run_id).execute()
        assert status == RunStatus.COMPLETED, status

        with session_scope() as s:
            run = runs_repo.get_run(s, run_id)
            assert run.finished_at is not None
            assert run.total_articles > 0
            assert run.total_report_items > 0
            assert run.total_new_events > 0
            assert all(
                m.status == ModuleRunStatus.COMPLETED for m in run.module_runs
            ), [(m.module_name, m.status, m.error_message) for m in run.module_runs]

            # Articles were persisted with traceable identity.
            articles = articles_repo.list_for_run(s, run_id)
            assert articles
            assert all(a.url_hash for a in articles)
            assert all(a.body_text for a in articles)

            # An event with one observation, linked to its sources.
            events = events_repo.list_events(s)
            assert len(events) == 1
            event = events_repo.get_event(s, events[0].id)
            assert event.observation_count == 1
            observation = event.observations[0]
            assert observation.fact_summary
            assert observation.assessment.startswith("判断")
            assert observation.metrics["device_count"]["value"] == 85000000
            assert observation.sources, "observation must cite evidence"
            assert observation.sources[0].article.url.startswith("https://huawei.com")

            # Report persisted with headline, trends, metrics.
            report = reports_repo.get_by_run(s, run_id)
            assert report is not None
            assert report.headline_json["title"] == "鸿蒙生态持续扩张"
            assert len(report.trends_json) == 1
            assert report.item_count == 1
            assert report.sections[0].items[0].event_state == "new"

            # Export artifacts exist on disk.
            html_path = db.reports_dir / "2026-09-15.html"
            json_path = db.reports_dir / "2026-09-15.json"
            assert html_path.exists() and json_path.exists()

            html = html_path.read_text(encoding="utf-8")
            assert "全球智能终端操作系统监测日报" in html
            assert "情报判断" in html
            assert "huawei.com" in html
            assert "NEW" in html

            audit = json.loads(json_path.read_text(encoding="utf-8"))
            assert audit["schema_version"] == 2
            assert audit["report_date"] == "2026-09-15"
            assert audit["events"] and audit["observations"] and audit["sources"]
            assert audit["report_items"][0]["source_ids"]

    def test_run_logs_and_usage_are_recorded(self, db, piped, session):
        from aios.database import session_scope
        from aios.repositories import runs as runs_repo
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        MonitoringPipeline(run_id).execute()

        with session_scope() as s:
            logs = runs_repo.logs_for(s, run_id)
            messages = [entry.message for entry in logs]
            assert any("Started" in m for m in messages)
            assert any("Collecting" in m for m in messages)
            assert any("Finished" in m for m in messages)

            usage = runs_repo.usage_summary(s, run_id)
            assert usage["calls"] > 0
            assert usage["total_tokens"] > 0


class TestEventContinuity:
    def test_second_day_creates_observation_not_new_event(self, db, piped, session):
        """The core promise: a continuing story is one event, two observations."""
        from aios.database import session_scope
        from aios.repositories import events as events_repo
        from aios.repositories import runs as runs_repo
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)

        with session_scope() as s:
            run1 = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        MonitoringPipeline(run1).execute()

        with session_scope() as s:
            events = events_repo.list_events(s)
            assert len(events) == 1
            first_event_id = events[0].id

        # Day two: the model reports growth and matches the existing event.
        piped.topic_items = [
            {
                "tag": "操作系统AI化",
                "title": "HarmonyOS 7 生态发展",
                "fact_summary": "官方公布设备数量增至 8720 万台。",
                "assessment": "判断：增速较上期放缓。",
                "importance": 1,
                "confidence": "high",
                "source_ids": [0],
                "structured_data": {
                    "device_count": {
                        "value": 87200000, "display": "8720万", "unit": "devices", "source_id": 0
                    }
                },
            }
        ]
        piped.match_decision = {
            "decision": "MATCH", "event_id": first_event_id,
            "confidence": 0.93, "reason": "同一事件的后续数据",
        }

        with session_scope() as s:
            run2 = runs_repo.create_run(s, dt.date(2026, 9, 16), "manual").id
        assert MonitoringPipeline(run2).execute() == RunStatus.COMPLETED

        with session_scope() as s:
            events = events_repo.list_events(s)
            assert len(events) == 1, "a continuing story must not spawn a second event"

            event = events_repo.get_event(s, first_event_id)
            assert event.observation_count == 2
            assert len(event.observations) == 2
            assert event.first_seen_date == dt.date(2026, 9, 15)
            assert event.last_seen_date == dt.date(2026, 9, 16)

            # The time series is preserved, not overwritten.
            values = [o.metrics["device_count"]["value"] for o in event.observations]
            assert values == [85000000, 87200000]

            run = runs_repo.get_run(s, run2)
            assert run.total_updated_events == 1
            assert run.total_new_events == 0

    def test_day_two_diff_shows_the_numeric_change(self, db, piped, session):
        """The acceptance example: 8500万 -> 8720万 = +220万 / +2.59%."""
        from aios.database import session_scope
        from aios.repositories import events as events_repo
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services.diff_engine import DATA_CHANGE, compare_dates
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        with session_scope() as s:
            run1 = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        MonitoringPipeline(run1).execute()

        with session_scope() as s:
            event_id = events_repo.list_events(s)[0].id

        piped.topic_items[0]["fact_summary"] = "官方公布设备数量增至 8720 万台。"
        piped.topic_items[0]["structured_data"]["device_count"] = {
            "value": 87200000, "display": "8720万", "unit": "devices", "source_id": 0
        }
        piped.match_decision = {
            "decision": "MATCH", "event_id": event_id, "confidence": 0.95, "reason": "后续"
        }

        with session_scope() as s:
            run2 = runs_repo.create_run(s, dt.date(2026, 9, 16), "manual").id
        MonitoringPipeline(run2).execute()

        with session_scope() as s:
            result = compare_dates(s, dt.date(2026, 9, 15), dt.date(2026, 9, 16))
            assert not result.error
            diff = next(d for d in result.diffs if d.event_id == event_id)
            assert diff.change_type == DATA_CHANGE
            change = diff.metric_changes[0]
            assert change.absolute_change == 2_200_000
            assert change.absolute_display == "+220万"
            assert change.percentage_display == "+2.59%"
            assert change.direction == "up"


class TestFailureIsolation:
    def test_a_failing_module_does_not_lose_the_others(self, db, monkeypatch, session):
        """Spec: one broken module must not cost the whole run."""
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services import pipeline as pipeline_module
        from aios.services.pipeline import MonitoringPipeline

        # Enable exactly two modules, one topic each.
        for module in modules_repo.list_modules(session):
            module.enabled = module.key in {"mobile", "pc"}
            for index, topic in enumerate(module.topics):
                topic.enabled = index == 0
        session.commit()

        class HalfBrokenClient(ScriptedClient):
            def complete_json(self, messages, purpose="generic", **kwargs):
                blob = json.dumps(messages, ensure_ascii=False)
                if purpose == "topic_analysis" and "PC 侧" in blob:
                    raise LLMError("simulated PC failure")
                return super().complete_json(messages, purpose=purpose, **kwargs)

        client = HalfBrokenClient()

        def factory(usage_sink=None):
            client.usage_sink = usage_sink
            return client

        monkeypatch.setattr(pipeline_module, "build_llm_service", factory)
        stub_collectors(monkeypatch, lambda query: make_candidates(2))
        monkeypatch.setattr(pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文。" * 30)

        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id

        status = MonitoringPipeline(run_id).execute()
        assert status == RunStatus.COMPLETED_WITH_ERRORS

        with session_scope() as s:
            run = runs_repo.get_run(s, run_id)
            assert run.error_message
            report = reports_repo.get_by_run(s, run_id)
            assert report is not None
            # The healthy module still produced its item.
            mobile = next(x for x in report.sections if x.module_key == "mobile")
            assert mobile.items
            # The broken module is present but empty, not missing.
            pc = next(x for x in report.sections if x.module_key == "pc")
            assert pc.items == []

    def test_missing_api_key_fails_the_run_cleanly(self, db, monkeypatch, session):
        from aios.database import session_scope
        from aios.repositories import runs as runs_repo
        from aios.services import pipeline as pipeline_module
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)

        class KeylessClient(ScriptedClient):
            def __init__(self):
                super().__init__()
                self.has_key = False
                self.ready = False

        monkeypatch.setattr(
            pipeline_module, "build_llm_service", lambda usage_sink=None: KeylessClient()
        )
        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id

        assert MonitoringPipeline(run_id).execute() == RunStatus.FAILED
        with session_scope() as s:
            run = runs_repo.get_run(s, run_id)
            assert "API Key" in run.error_message

    def test_items_without_valid_sources_are_dropped(self, db, piped, session):
        """An item the model cannot ground in evidence is not admissible."""
        from aios.database import session_scope
        from aios.repositories import events as events_repo
        from aios.repositories import runs as runs_repo
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        piped.topic_items = [
            {
                "tag": "x", "title": "无证据的条目", "fact_summary": "凭空生成的内容。",
                "assessment": "判断：...", "importance": 1, "confidence": "high",
                "source_ids": [],
            }
        ]
        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        MonitoringPipeline(run_id).execute()

        with session_scope() as s:
            assert events_repo.list_events(s) == []
            run = runs_repo.get_run(s, run_id)
            assert run.total_report_items == 0

    def test_no_candidates_yields_watch_section(self, db, monkeypatch, session):
        """No sources must produce an honest 'watch', never invented content."""
        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services import pipeline as pipeline_module
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        client = ScriptedClient()
        monkeypatch.setattr(
            pipeline_module, "build_llm_service", lambda usage_sink=None: client
        )
        # A *successful* search that found nothing - not a source failure.
        stub_collectors(monkeypatch, [])

        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        assert MonitoringPipeline(run_id).execute() == RunStatus.COMPLETED

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            assert report.sections[0].status == "watch"
            assert report.sections[0].items == []
            assert "topic_analysis" not in client.calls


class TestArticleDeduplication:
    def test_same_url_across_runs_is_not_duplicated(self, db, piped, session):
        from aios.database import session_scope
        from aios.repositories import articles as articles_repo
        from aios.repositories import runs as runs_repo
        from aios.services.pipeline import MonitoringPipeline

        only_mobile(session)
        for day in (15, 16):
            with session_scope() as s:
                run_id = runs_repo.create_run(s, dt.date(2026, 9, day), "manual").id
            MonitoringPipeline(run_id).execute()

        with session_scope() as s:
            # Both runs saw the same two URLs; storage must hold two rows, not four.
            assert articles_repo.total_count(s) == 2


class TestRealRouterIntegration:
    """Drives the genuine LLMService -> provider -> HTTP chain.

    Everything above this point stubs the router. Here only the network is
    faked, so provider resolution, task routing, credential lookup, payload
    construction and usage normalisation are all exercised for real.
    """

    def _install_providers(self, session, with_glm=False):
        from aios.repositories import providers as providers_repo

        deepseek = providers_repo.get_by_provider_id(session, "deepseek")
        deepseek.default_model = "deepseek-chat"
        deepseek.has_api_key = True
        deepseek.api_key_last_four = "1111"
        providers_repo.set_default(session, deepseek)

        glm = None
        if with_glm:
            glm = providers_repo.create_provider(
                session, provider_id="zhipu", display_name="智谱 GLM",
                base_url="https://open.bigmodel.cn/api/paas/v4",
                default_model="glm-4-plus", has_api_key=True, api_key_last_four="2222",
            )
            providers_repo.set_route(session, "synthesis", glm.id, "glm-4-plus")
        session.commit()
        return deepseek, glm

    def _fake_http(self, recorder):
        """A requests.Session stand-in that answers by inspecting the prompt."""
        from conftest import FakeResponse, chat_payload

        def post(url, headers=None, json=None, timeout=None, params=None):
            recorder.append({"url": url, "model": json.get("model"), "json": json})
            blob = str(json.get("messages", ""))

            if "归并判断器" in blob:
                content = '{"decision":"NEW_EVENT","event_id":null,"confidence":0.9,"reason":"x"}'
            elif "二次综合" in blob:
                content = (
                    '{"headline":{"title":"综合头条","body":"摘要。","section":"移动智能终端侧"},'
                    '"trends":[{"title":"趋势一","body":"研判。","confidence":"high"}],'
                    '"metrics":[]}'
                )
            elif "当期小结" in blob:
                content = '{"status":"new","summary":"本领域有新增动态。","metrics":[]}'
            else:
                content = json_dumps_item()
            return FakeResponse(
                200,
                chat_payload(
                    content,
                    model=json.get("model"),
                    usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                ),
            )

        class FakeSession:
            pass

        session = FakeSession()
        session.post = post
        return session

    def test_full_run_through_the_real_router(self, db, session, monkeypatch):
        """TEST A: a DeepSeek-only configuration still produces a report."""
        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services import pipeline as pipeline_module
        from aios.services import keyring_service
        from aios.services.llm import service as service_module
        from aios.services.pipeline import MonitoringPipeline

        self._install_providers(session)
        only_mobile(session)

        monkeypatch.setattr(keyring_service, "get_provider_key", lambda pid: "sk-test-1111")
        stub_collectors(monkeypatch, lambda query: make_candidates(2))
        monkeypatch.setattr(
            pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文。" * 30
        )

        calls = []
        fake_http = self._fake_http(calls)
        original_init = service_module.LLMService.__init__

        def patched_init(self, usage_sink=None, http_session=None):
            original_init(self, usage_sink=usage_sink, http_session=fake_http)

        monkeypatch.setattr(service_module.LLMService, "__init__", patched_init)

        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id

        assert MonitoringPipeline(run_id).execute() == RunStatus.COMPLETED, calls

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            assert report is not None
            assert report.item_count == 1
            assert report.headline_json["title"] == "综合头条"
            # Every call went to the configured provider.
            assert {c["model"] for c in calls} == {"deepseek-chat"}
            assert all("api.deepseek.com" in c["url"] for c in calls)

            usage = runs_repo.usage_summary(s, run_id)
            assert usage["calls"] == len(calls)
            assert usage["total_tokens"] == 30 * len(calls)

    def test_task_routing_sends_synthesis_to_a_second_vendor(
        self, db, session, monkeypatch
    ):
        """TEST C: DeepSeek by default, GLM for final synthesis - one run, two vendors."""
        from aios.database import session_scope
        from aios.models import LLMUsage
        from aios.repositories import runs as runs_repo
        from aios.services import keyring_service
        from aios.services import pipeline as pipeline_module
        from aios.services.llm import service as service_module
        from aios.services.pipeline import MonitoringPipeline

        self._install_providers(session, with_glm=True)
        only_mobile(session)

        monkeypatch.setattr(keyring_service, "get_provider_key", lambda pid: f"sk-{pid}-key")
        stub_collectors(monkeypatch, lambda query: make_candidates(2))
        monkeypatch.setattr(
            pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文。" * 30
        )

        calls = []
        fake_http = self._fake_http(calls)
        original_init = service_module.LLMService.__init__

        def patched_init(self, usage_sink=None, http_session=None):
            original_init(self, usage_sink=usage_sink, http_session=fake_http)

        monkeypatch.setattr(service_module.LLMService, "__init__", patched_init)

        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        assert MonitoringPipeline(run_id).execute() == RunStatus.COMPLETED

        synthesis_calls = [c for c in calls if "二次综合" in str(c["json"]["messages"])]
        other_calls = [c for c in calls if "二次综合" not in str(c["json"]["messages"])]

        assert synthesis_calls, "synthesis must have run"
        assert all(c["model"] == "glm-4-plus" for c in synthesis_calls)
        assert all("bigmodel.cn" in c["url"] for c in synthesis_calls)
        assert all(c["model"] == "deepseek-chat" for c in other_calls)

        with session_scope() as s:
            providers_used = {
                row.provider_id for row in s.query(LLMUsage).filter_by(run_id=run_id)
            }
            assert providers_used == {"deepseek", "zhipu"}

    def test_report_is_stamped_with_the_engines_used(self, db, session, monkeypatch):
        from aios.database import session_scope
        from aios.repositories import reports as reports_repo
        from aios.repositories import runs as runs_repo
        from aios.services import keyring_service
        from aios.services import pipeline as pipeline_module
        from aios.services.llm import service as service_module
        from aios.services.pipeline import MonitoringPipeline

        self._install_providers(session, with_glm=True)
        only_mobile(session)

        monkeypatch.setattr(keyring_service, "get_provider_key", lambda pid: "sk-x-1234")
        stub_collectors(monkeypatch, lambda query: make_candidates(2))
        monkeypatch.setattr(
            pipeline_module.ArticleExtractor, "fetch", lambda self, url: "正文。" * 30
        )

        fake_http = self._fake_http([])
        original_init = service_module.LLMService.__init__

        def patched_init(self, usage_sink=None, http_session=None):
            original_init(self, usage_sink=usage_sink, http_session=fake_http)

        monkeypatch.setattr(service_module.LLMService, "__init__", patched_init)

        with session_scope() as s:
            run_id = runs_repo.create_run(s, dt.date(2026, 9, 15), "manual").id
        MonitoringPipeline(run_id).execute()

        with session_scope() as s:
            report = reports_repo.get_by_run(s, run_id)
            # Synthesis engine named, plus a note that other models took part.
            assert "智谱 GLM" in report.model
            assert "glm-4-plus" in report.model
            assert "more" in report.model


def json_dumps_item() -> str:
    """The topic-analysis reply used by the integration tests."""
    return json.dumps({
        "status": "new",
        "items": [{
            "tag": "操作系统AI化",
            "title": "HarmonyOS 生态数据更新",
            "fact_summary": "官方公布设备数量达到 8500 万台。",
            "assessment": "判断：生态扩张仍在继续。",
            "importance": 1,
            "confidence": "high",
            "source_ids": [0],
            "structured_data": {
                "device_count": {"value": 85000000, "display": "8500万", "unit": "devices"}
            },
        }],
        "metrics": [],
    }, ensure_ascii=False)
