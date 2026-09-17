"""AI-assisted monitoring configuration: schema, preview, apply, accounting.

Nothing here touches a real provider. A fake :class:`LLMService` returns
whatever payload the test wants, which is the point: the guarantees being
tested are about *our* validation, budgeting and transaction handling, not
about any model's behaviour.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aios.schemas.config_generation import (
    MAX_QUERIES_PER_TOPIC,
    MAX_TOPICS_PER_MODULE,
    MAX_TOTAL_QUERIES,
    GeneratedMonitorConfig,
)
from aios.services import config_generator
from aios.services.config_generator import (
    ApplyError,
    ConfigGenerationError,
    ConfigGenerator,
)
from aios.services.llm.base import InvalidJSONResponse, LLMResponse


# --- fakes ------------------------------------------------------------------

class FakeLLM:
    """Stands in for LLMService, recording exactly what it was asked."""

    def __init__(self, payloads, provider_id="deepseek", model="deepseek-chat"):
        self.payloads = list(payloads) if isinstance(payloads, list) else [payloads]
        self.provider_id = provider_id
        self.model = model
        self.calls: list[dict] = []
        self.usage_sink = None

    def readiness_error(self) -> str:
        return ""

    def complete_json(self, messages, purpose="generic", **kwargs):
        self.calls.append({"messages": messages, "purpose": purpose, **kwargs})
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(payload, Exception):
            raise payload
        response = LLMResponse(
            data=payload,
            content=json.dumps(payload, ensure_ascii=False),
            model=self.model,
            provider_id=self.provider_id,
            latency_ms=842,
            prompt_tokens=1200,
            completion_tokens=400,
            total_tokens=1600,
        )
        if self.usage_sink:
            self.usage_sink(
                {
                    "purpose": purpose,
                    "provider_id": self.provider_id,
                    "model": self.model,
                    "prompt_tokens": 1200,
                    "completion_tokens": 400,
                    "total_tokens": 1600,
                    "latency_ms": 842,
                    "attempts": 1,
                    "success": True,
                    "error_message": "",
                }
            )
        return response


def good_payload(topics=2, queries=2, name="人形机器人操作系统"):
    return {
        "name": name,
        "key": "humanoid_os",
        "description": "监测人形机器人操作系统与具身智能软件栈的动态。",
        "topics": [
            {
                "name": f"主题{i}",
                "description": f"主题{i} 的监测范围",
                "queries": [
                    {"query": f"humanoid robot OS topic{i} q{j}", "priority": 5}
                    for j in range(queries)
                ],
                "preferred_sources": [{"domain": "unitree.com", "reason": "厂商官方"}],
                "excluded_keywords": ["招聘"],
                "analysis_instructions": "关注软件平台与工具链。",
            }
            for i in range(topics)
        ],
        "recommended_lookback_days": 3,
        "recommended_report_limit": 2,
        "analysis_instructions": "只写证据支持的内容。",
        "optional_notes": "建议后续补充标准组织来源。",
    }


@pytest.fixture
def generator_factory(monkeypatch):
    """Build a ConfigGenerator wired to a FakeLLM, with usage flowing through."""

    def _make(payloads):
        fake = FakeLLM(payloads)
        usage: list[dict] = []
        fake.usage_sink = usage.append
        generator = ConfigGenerator(client=fake)
        generator._usage = usage
        # ConfigGenerator captured its own list at construction time; rebind so
        # the fake writes into the list the generator reports.
        fake.usage_sink = generator._usage.append
        return generator, fake

    return _make


# --- 1. schema-valid generation --------------------------------------------

class TestGeneration:
    def test_generates_schema_valid_configuration(self, db, generator_factory):
        generator, _ = generator_factory(good_payload())
        result = generator.generate_module("监测人形机器人操作系统与 ROS 2 动态。")

        config = result.config
        assert isinstance(config, GeneratedMonitorConfig)
        assert config.name == "人形机器人操作系统"
        assert config.key == "humanoid_os"
        assert len(config.topics) == 2
        assert all(t.queries for t in config.topics)
        assert result.provider_id == "deepseek"

    def test_routes_through_the_config_generation_purpose(self, db, generator_factory):
        generator, fake = generator_factory(good_payload())
        generator.generate_module("监测国产 AI PC 操作系统动态。")
        assert fake.calls[0]["purpose"] == config_generator.PURPOSE == "config_generation"

    def test_prompt_asks_for_search_queries_not_prose(self, db, generator_factory):
        generator, fake = generator_factory(good_payload())
        generator.generate_module("监测商业航天太空算力动态。")
        system = fake.calls[0]["messages"][0]["content"]
        assert "检索式" in system
        assert str(MAX_QUERIES_PER_TOPIC) in system
        assert str(MAX_TOTAL_QUERIES) in system


# --- 2. malformed JSON is repaired by the existing LLM layer -----------------

class TestMalformedJSON:
    def test_repair_loop_lives_in_the_provider_layer(self, db, monkeypatch):
        """A malformed reply is repaired by generate_json, not re-implemented."""
        from aios.services.llm.base import LLMProvider, ProviderRuntimeConfig

        replies = [
            "这是一段解释\n{invalid json",
            json.dumps(good_payload(), ensure_ascii=False),
        ]
        seen: list[list[dict]] = []

        class Provider(LLMProvider):
            provider_id = "fake"

            def _chat(self, messages, max_tokens, json_mode):
                seen.append(messages)
                return replies.pop(0), {"model": "fake-model"}

        monkeypatch.setattr("time.sleep", lambda *_: None)
        provider = Provider(
            ProviderRuntimeConfig(
                provider_id="fake", base_url="http://x", model="fake-model",
                api_key="sk-test", retries=3,
            )
        )
        response = provider.generate_json([{"role": "user", "content": "生成配置"}],
                                          purpose="config_generation")

        assert response.data["name"] == "人形机器人操作系统"
        assert response.attempts == 2
        # The repair instruction was appended by the shared layer.
        assert any("合法 JSON" in m["content"] for m in seen[1])
        # And the repaired payload still has to pass our schema.
        GeneratedMonitorConfig.model_validate(response.data)

    def test_unrepairable_json_surfaces_as_a_generation_error(self, db, generator_factory):
        generator, _ = generator_factory(InvalidJSONResponse("no JSON object found"))
        with pytest.raises(ConfigGenerationError):
            generator.generate_module("监测卫星边缘 AI 动态。")


# --- 3. invalid generated config is rejected --------------------------------

class TestRejection:
    @pytest.mark.parametrize(
        "mutate,reason",
        [
            (lambda p: p.update({"name": ""}), "空模块名"),
            (lambda p: p.update({"topics": []}), "没有主题"),
            (lambda p: p["topics"][0].update({"queries": []}), "主题没有检索式"),
            (lambda p: p["topics"][0]["queries"].clear(), "检索式被清空"),
        ],
    )
    def test_structurally_invalid_payloads_are_rejected(
        self, db, generator_factory, mutate, reason
    ):
        payload = good_payload()
        mutate(payload)
        generator, _ = generator_factory(payload)
        with pytest.raises(ConfigGenerationError):
            generator.generate_module("监测某个领域。", existing_keys=[])

    def test_empty_query_string_is_rejected(self, db):
        payload = good_payload(topics=1, queries=1)
        payload["topics"][0]["queries"] = [{"query": "   ", "priority": 5}]
        with pytest.raises(ValidationError):
            GeneratedMonitorConfig.model_validate(payload)

    def test_invalid_domain_is_dropped_not_silently_accepted(self, db, generator_factory):
        payload = good_payload(topics=1, queries=2)
        payload["topics"][0]["preferred_sources"] = [
            {"domain": "not a domain at all", "reason": ""},
            {"domain": "https://reuters.com/tech", "reason": "权威媒体"},
        ]
        generator, _ = generator_factory(payload)
        config = generator.generate_module("监测某个领域。").config
        domains = [s.domain for s in config.topics[0].preferred_sources]
        assert domains == ["reuters.com"]

    def test_a_domain_carrying_credentials_is_refused(self, db):
        from aios.schemas.config_generation import GeneratedSource

        for bad in ("user:pw@example.com", "example.com?api_key=abc"):
            with pytest.raises(ValidationError):
                GeneratedSource.model_validate({"domain": bad, "reason": ""})

    def test_extra_fields_are_forbidden(self, db):
        payload = good_payload(topics=1, queries=1)
        payload["surprise"] = {"api_key": "sk-leak"}
        with pytest.raises(ValidationError):
            GeneratedMonitorConfig.model_validate(payload)


# --- query budget -----------------------------------------------------------

class TestQueryBudget:
    def test_topics_are_capped(self, db, generator_factory):
        generator, _ = generator_factory(good_payload(topics=20, queries=1))
        result = generator.generate_module("监测某个很大的领域。")
        assert len(result.config.topics) == MAX_TOPICS_PER_MODULE
        assert any("主题" in w for w in result.warnings)

    def test_queries_per_topic_are_capped(self, db, generator_factory):
        generator, _ = generator_factory(good_payload(topics=1, queries=15))
        result = generator.generate_module("监测某个领域。")
        assert len(result.config.topics[0].queries) == MAX_QUERIES_PER_TOPIC

    def test_total_query_budget_is_enforced(self, db, generator_factory):
        generator, _ = generator_factory(good_payload(topics=8, queries=5))
        result = generator.generate_module("监测某个很大的领域。")
        assert result.config.total_queries <= MAX_TOTAL_QUERIES
        assert any("检索式" in w for w in result.warnings)

    def test_duplicate_queries_inside_a_topic_are_collapsed(self, db):
        payload = good_payload(topics=1, queries=1)
        payload["topics"][0]["queries"] = [
            {"query": "HarmonyOS  NEXT", "priority": 5},
            {"query": "harmonyos next", "priority": 3},
            {"query": "ROS 2 humble", "priority": 5},
        ]
        config = GeneratedMonitorConfig.model_validate(payload)
        assert len(config.topics[0].queries) == 2


# --- 4 & 5. preview does not write; apply is transactional -------------------

class TestPreviewAndApply:
    def test_preview_writes_nothing_to_the_database(self, client, session, monkeypatch):
        from aios.repositories import modules as modules_repo
        from aios.routers import config_ai

        before = len(modules_repo.list_modules(session, include_archived=True))
        monkeypatch.setattr(
            config_ai.config_generator, "ConfigGenerator",
            lambda *a, **k: ConfigGenerator(client=FakeLLM(good_payload())),
        )

        response = client.post(
            "/monitoring/ai/generate", data={"description": "监测人形机器人操作系统动态。"}
        )
        assert response.status_code == 200
        assert "人形机器人操作系统" in response.text
        assert "应用配置" in response.text

        session.expire_all()
        after = modules_repo.list_modules(session, include_archived=True)
        assert len(after) == before
        assert not any(m.key == "humanoid_os" for m in after)

    def test_apply_creates_module_topics_and_queries(self, session, db):
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo

        config = GeneratedMonitorConfig.model_validate(good_payload(topics=3, queries=2))
        with session_scope() as s:
            result = config_generator.apply_module_draft(s, config)
            module_id = result.module_id

        with session_scope() as s:
            module = modules_repo.get_module(s, module_id)
            assert module.key == "humanoid_os"
            assert len(module.topics) == 3
            assert module.query_count == 6
            assert module.lookback_days == 3
            assert module.max_report_items == 2
            topic = module.topics[0]
            assert [s_.domain for s_ in topic.preferred_sources] == ["unitree.com"]
            assert [k.keyword for k in topic.excluded_keywords] == ["招聘"]
            assert topic.analysis_prompt == "关注软件平台与工具链。"

    def test_apply_is_transactional_on_duplicate_key(self, session, db):
        """A rejected draft must leave nothing behind, not a bare module."""
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo

        config = GeneratedMonitorConfig.model_validate(good_payload())
        with session_scope() as s:
            config_generator.apply_module_draft(s, config)

        with session_scope() as s:
            before = len(modules_repo.list_modules(s, include_archived=True))

        from aios.database import new_session

        s = new_session()
        try:
            with pytest.raises(ApplyError):
                config_generator.apply_module_draft(s, config)
            s.rollback()
        finally:
            s.close()

        with session_scope() as s:
            assert len(modules_repo.list_modules(s, include_archived=True)) == before

    def test_apply_rolls_back_when_a_topic_fails_midway(self, session, db):
        """Failure after the module row exists must still leave no module."""
        from aios.database import new_session, session_scope
        from aios.repositories import modules as modules_repo

        payload = good_payload(topics=3, queries=2)
        config = GeneratedMonitorConfig.model_validate(payload)
        # Force a mid-apply failure on the second topic.
        object.__setattr__(config.topics[1], "name", config.topics[0].name)

        with session_scope() as s:
            before = len(modules_repo.list_modules(s, include_archived=True))

        s = new_session()
        try:
            with pytest.raises(ApplyError):
                config_generator.apply_module_draft(s, config)
            s.rollback()
        finally:
            s.close()

        with session_scope() as s:
            modules = modules_repo.list_modules(s, include_archived=True)
            assert len(modules) == before
            assert not any(m.key == "humanoid_os" for m in modules)

    def test_apply_through_the_route_uses_the_edited_values(self, client, session, monkeypatch):
        """The user edits one query in the preview; the edit is what is saved."""
        from aios.repositories import modules as modules_repo

        response = client.post(
            "/monitoring/ai/apply",
            data={
                "topic_count": "1",
                "module_name": "边缘智能",
                "module_key": "edge_intel",
                "module_description": "监测边缘智能。",
                "lookback_days": "5",
                "max_report_items": "3",
                "module_instructions": "只记录官方发布。",
                "topic_enabled_0": "1",
                "topic_name_0": "星载操作系统",
                "topic_description_0": "卫星上的操作系统",
                "topic_queries_0": "satellite operating system\n星载 操作系统 发布",
                "topic_sources_0": "nasa.gov",
                "topic_excluded_0": "招聘",
                "topic_instructions_0": "关注在轨验证。",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        session.expire_all()
        module = modules_repo.get_module_by_key(session, "edge_intel")
        assert module is not None
        assert module.lookback_days == 5
        assert module.max_report_items == 3
        queries = [q.query for q in module.topics[0].queries]
        assert queries == ["satellite operating system", "星载 操作系统 发布"]

    def test_apply_rejects_an_edited_draft_that_became_invalid(self, client, session):
        from aios.repositories import modules as modules_repo

        before = len(modules_repo.list_modules(session, include_archived=True))
        response = client.post(
            "/monitoring/ai/apply",
            data={
                "topic_count": "1",
                "module_name": "空检索式模块",
                "module_key": "empty_q",
                "topic_enabled_0": "1",
                "topic_name_0": "某主题",
                "topic_queries_0": "   \n  ",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        session.expire_all()
        assert len(modules_repo.list_modules(session, include_archived=True)) == before


# --- 6. topic generation never overwrites the rest of the module ------------

class TestTopicGeneration:
    def test_adding_a_topic_leaves_existing_topics_untouched(self, session, db):
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo
        from aios.schemas.config_generation import GeneratedTopic

        with session_scope() as s:
            module = modules_repo.get_module_by_key(s, "mobile")
            module_id = module.id
            before = {
                t.name: sorted(q.query for q in t.queries)
                for t in module.topics if not t.archived
            }
            before_prompt = module.analysis_prompt
            before_lookback = module.lookback_days

        topic = GeneratedTopic.model_validate(
            {
                "name": "商业空间站边缘计算",
                "description": "在轨边缘计算与自主任务规划",
                "queries": [
                    {"query": "commercial space station edge computing", "priority": 5},
                    {"query": "在轨 自主任务规划", "priority": 4},
                ],
                "preferred_sources": [{"domain": "nasa.gov", "reason": "官方"}],
                "excluded_keywords": [],
                "analysis_instructions": "关注在轨验证结果。",
            }
        )

        with session_scope() as s:
            result = config_generator.apply_topic_draft(s, module_id, topic)
            assert result.query_count == 2

        with session_scope() as s:
            module = modules_repo.get_module(s, module_id)
            after = {
                t.name: sorted(q.query for q in t.queries)
                for t in module.topics if not t.archived
            }
            # Every pre-existing topic is byte-identical.
            for name, queries in before.items():
                assert after[name] == queries
            assert "商业空间站边缘计算" in after
            assert len(after) == len(before) + 1
            # Module-level settings are untouched.
            assert module.analysis_prompt == before_prompt
            assert module.lookback_days == before_lookback

    def test_duplicate_topic_name_is_refused(self, session, db):
        from aios.database import session_scope
        from aios.repositories import modules as modules_repo
        from aios.schemas.config_generation import GeneratedTopic

        with session_scope() as s:
            module = modules_repo.get_module_by_key(s, "mobile")
            module_id, existing = module.id, module.topics[0].name

        topic = GeneratedTopic.model_validate(
            {"name": existing, "queries": [{"query": "some query", "priority": 5}]}
        )
        with session_scope() as s:
            with pytest.raises(ApplyError):
                config_generator.apply_topic_draft(s, module_id, topic)

    def test_topic_prompt_forbids_regenerating_the_module(self, db, generator_factory):
        payload = {
            "topic": good_payload(topics=1, queries=2)["topics"][0],
            "optional_notes": "",
        }
        generator, fake = generator_factory(payload)
        generator.generate_topic(
            "再加一个关注商业空间站边缘计算的主题。",
            module_name="太空智算侧",
            existing_topics=["星载算力"],
        )
        user = fake.calls[0]["messages"][1]["content"]
        assert "不要重新生成已有主题" in user
        assert "星载算力" in user


# --- 7 & 8. usage accounting and credential hygiene -------------------------

class TestUsageAndSecrets:
    def test_config_generation_usage_is_recorded(self, session, db, generator_factory):
        from aios.models import LLMUsage

        generator, _ = generator_factory(good_payload())
        result = generator.generate_module("监测人形机器人操作系统动态。")
        assert result.usage, "the fake provider reported usage"

        written = config_generator.record_usage(session, result.usage)
        session.flush()
        assert written == 1

        rows = list(session.query(LLMUsage).filter(LLMUsage.purpose == "config_generation"))
        assert len(rows) == 1
        row = rows[0]
        assert row.provider_id == "deepseek"
        assert row.model == "deepseek-chat"
        assert row.prompt_tokens == 1200
        assert row.completion_tokens == 400
        assert row.latency_ms == 842
        assert row.run_id is None

    def test_no_api_key_reaches_the_prompt_or_the_database(
        self, session, db, generator_factory, isolated_keyring
    ):
        from aios.models import LLMUsage
        from aios.services import keyring_service

        secret = "sk-" + "s3cr3t" * 5
        keyring_service.set_provider_key("deepseek", secret)

        generator, fake = generator_factory(good_payload())
        result = generator.generate_module("监测人形机器人操作系统动态。")

        blob = json.dumps(fake.calls, ensure_ascii=False)
        assert secret not in blob
        assert "api_key" not in blob.lower()

        config_generator.record_usage(session, result.usage)
        session.flush()
        for row in session.query(LLMUsage).all():
            serialised = json.dumps(
                {
                    "model": row.model,
                    "provider": row.provider_id,
                    "error": row.error_message,
                    "extra": row.extra_json,
                },
                ensure_ascii=False,
            )
            assert secret not in serialised

    def test_generation_is_blocked_with_a_link_when_no_provider_exists(
        self, client, session
    ):
        from aios.repositories import providers as providers_repo

        for row in providers_repo.list_providers(session):
            session.delete(row)
        session.commit()

        page = client.get("/monitoring/ai/new")
        assert page.status_code == 200
        assert config_generator.NO_PROVIDER_MESSAGE in page.text
        assert "/settings/ai" in page.text

        response = client.post("/monitoring/ai/generate", data={"description": "监测某领域。"})
        assert config_generator.NO_PROVIDER_MESSAGE in response.text
