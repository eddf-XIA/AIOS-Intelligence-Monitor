"""The multi-provider LLM layer: presets, adapters, routing and credentials."""

from __future__ import annotations

import json

import pytest

from aios.services import keyring_service
from aios.services.llm import (
    LLMError,
    LLMResponse,
    MissingAPIKey,
    ProviderNotConfigured,
    build_provider,
)
from aios.services.llm.anthropic import AnthropicProvider
from aios.services.llm.gemini import GeminiProvider
from aios.services.llm.openai_compatible import OpenAICompatibleProvider
from aios.services.llm.presets import (
    PROVIDER_PRESETS,
    chinese_presets,
    get_preset,
    international_presets,
    ordered_presets,
    preset_or_custom,
)
from aios.services.llm.registry import provider_class_for, runtime_config
from aios.services.llm.service import LLMService, TASKS
from conftest import FakeResponse, chat_payload

SECRET = "sk-provider-test-key-9911"


class StubHTTP:
    """Scripted requests.Session replacement recording every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, params=None):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "params": params}
        )
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def build(provider_id, api_key=SECRET, session=None, model="m", base_url="", **kw):
    return build_provider(
        runtime_config(
            provider_id=provider_id, base_url=base_url, model=model,
            api_key=api_key, retries=kw.pop("retries", 2), **kw,
        ),
        session=session,
    )


# --- presets ----------------------------------------------------------------

class TestPresets:
    def test_all_required_chinese_providers_are_present(self):
        """The P0 list from the spec must be first-class."""
        required = {
            "deepseek", "qwen", "zhipu", "moonshot", "doubao",
            "minimax", "hunyuan", "qianfan", "siliconflow",
        }
        assert required <= set(PROVIDER_PRESETS)

    def test_international_and_local_providers_are_present(self):
        required = {"openai", "anthropic", "gemini", "openrouter", "ollama", "local_openai"}
        assert required <= set(PROVIDER_PRESETS)

    def test_chinese_providers_sort_first(self):
        ordered = [p.provider_id for p in ordered_presets()]
        assert ordered[0] == "deepseek"
        first_international = ordered.index("openai")
        for chinese in ("qwen", "zhipu", "moonshot", "siliconflow"):
            assert ordered.index(chinese) < first_international

    def test_chinese_and_international_split(self):
        chinese = {p.provider_id for p in chinese_presets()}
        international = {p.provider_id for p in international_presets()}
        assert "qwen" in chinese and "openai" in international
        assert not (chinese & international)

    def test_every_preset_has_a_display_name(self):
        for preset in PROVIDER_PRESETS.values():
            assert preset.display_name.strip()

    def test_chinese_presets_have_friendly_names(self):
        assert "通义千问" in get_preset("qwen").display_name
        assert "智谱" in get_preset("zhipu").display_name
        assert "月之暗面" in get_preset("moonshot").display_name
        assert "火山方舟" in get_preset("doubao").display_name
        assert "硅基流动" in get_preset("siliconflow").display_name

    def test_local_providers_need_no_key(self):
        assert get_preset("ollama").requires_api_key is False
        assert get_preset("local_openai").requires_api_key is False

    def test_unknown_provider_falls_back_to_custom_openai(self):
        preset = preset_or_custom("some-new-vendor")
        assert preset.api_style == "openai_compatible"
        assert preset.provider_id == "some-new-vendor"

    def test_deepseek_keeps_its_thinking_default(self):
        """Carried over from the original script's payload."""
        assert get_preset("deepseek").extra_body == {"thinking": {"type": "disabled"}}

    def test_model_lists_are_examples_not_constraints(self):
        """Models are pre-fill hints; anything the user types must work."""
        config = runtime_config(provider_id="qwen", model="qwen9-does-not-exist-yet")
        assert config.model == "qwen9-does-not-exist-yet"


class TestAdapterSelection:
    @pytest.mark.parametrize(
        "provider_id",
        ["deepseek", "qwen", "zhipu", "moonshot", "doubao", "minimax",
         "hunyuan", "qianfan", "siliconflow", "openai", "openrouter",
         "ollama", "local_openai", "custom"],
    )
    def test_openai_dialect_vendors_share_one_adapter(self, provider_id):
        cls = provider_class_for(get_preset(provider_id))
        assert issubclass(cls, OpenAICompatibleProvider)
        assert cls.provider_id == provider_id

    def test_anthropic_and_gemini_have_their_own_adapters(self):
        assert provider_class_for(get_preset("anthropic")) is AnthropicProvider
        assert provider_class_for(get_preset("gemini")) is GeminiProvider

    def test_preset_defaults_fill_in_base_url(self):
        config = runtime_config(provider_id="moonshot", model="m")
        assert config.base_url == "https://api.moonshot.cn/v1"

    def test_user_base_url_overrides_the_preset(self):
        """Proxy gateways and enterprise endpoints must win."""
        config = runtime_config(
            provider_id="deepseek", base_url="https://my-gateway.internal/v1", model="m"
        )
        assert config.base_url == "https://my-gateway.internal/v1"


# --- OpenAI-compatible path -------------------------------------------------

class TestOpenAICompatible:
    def test_request_shape(self):
        http = StubHTTP([FakeResponse(200, chat_payload('{"a":1}'))])
        provider = build("qwen", session=http, model="qwen-plus")
        provider.generate_json([{"role": "user", "content": "hi"}])

        call = http.calls[0]
        assert call["url"].endswith("/chat/completions")
        assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert call["json"]["model"] == "qwen-plus"
        assert call["json"]["response_format"] == {"type": "json_object"}

    def test_vendor_extras_are_scoped_to_that_vendor(self):
        """DeepSeek's `thinking` must not be sent to OpenAI."""
        deepseek_http = StubHTTP([FakeResponse(200, chat_payload('{"a":1}'))])
        build("deepseek", session=deepseek_http).generate_json([{"role": "user", "content": "x"}])
        assert "thinking" in deepseek_http.calls[0]["json"]

        openai_http = StubHTTP([FakeResponse(200, chat_payload('{"a":1}'))])
        build("openai", session=openai_http).generate_json([{"role": "user", "content": "x"}])
        assert "thinking" not in openai_http.calls[0]["json"]

    def test_usage_is_normalized(self):
        http = StubHTTP([
            FakeResponse(200, chat_payload(
                '{"a":1}', model="qwen-plus",
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            ))
        ])
        response = build("qwen", session=http).generate_json([{"role": "user", "content": "x"}])
        assert isinstance(response, LLMResponse)
        assert response.provider_id == "qwen"
        assert (response.prompt_tokens, response.completion_tokens) == (7, 3)
        assert response.total_tokens == 10
        assert response.parsed_json == {"a": 1}

    def test_missing_usage_stays_none_rather_than_estimated(self):
        http = StubHTTP([FakeResponse(200, chat_payload('{"a":1}'))])
        response = build("ollama", api_key=None, session=http).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert response.total_tokens is None
        assert response.prompt_tokens is None

    def test_local_provider_needs_no_key(self):
        http = StubHTTP([FakeResponse(200, chat_payload('{"status":"ok"}'))])
        provider = build("ollama", api_key=None, session=http, model="qwen3:32b")
        assert provider.has_key is True
        assert provider.generate_json([{"role": "user", "content": "x"}]).data["status"] == "ok"
        assert "Authorization" not in http.calls[0]["headers"]

    def test_multi_part_content_blocks_are_joined(self):
        body = {
            "choices": [{"message": {"content": [{"text": '{"a":'}, {"text": "1}"}]}}],
            "model": "m",
        }
        http = StubHTTP([FakeResponse(200, body)])
        assert build("custom", session=http, base_url="https://x/v1").generate_json(
            [{"role": "user", "content": "x"}]
        ).data == {"a": 1}

    def test_empty_choices_is_an_error(self):
        http = StubHTTP([FakeResponse(200, {"choices": []}), FakeResponse(200, {"choices": []})])
        with pytest.raises(LLMError):
            build("qwen", session=http, retries=2).generate_json(
                [{"role": "user", "content": "x"}]
            )

    def test_generate_text_returns_raw_content(self):
        http = StubHTTP([FakeResponse(200, chat_payload("plain words"))])
        response = build("qwen", session=http).generate_text([{"role": "user", "content": "x"}])
        assert response.content == "plain words"
        assert response.data == {}


class TestJSONRepair:
    def test_malformed_reply_is_retried_with_a_repair_instruction(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        http = StubHTTP([
            FakeResponse(200, chat_payload("Sorry, here is prose instead.")),
            FakeResponse(200, chat_payload('{"ok": true}')),
        ])
        response = build("zhipu", session=http, retries=2).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert response.data == {"ok": True}
        assert response.attempts == 2

        # The second request carries the repair instruction.
        second = http.calls[1]["json"]["messages"]
        assert any("合法 JSON" in m["content"] for m in second)

    def test_fenced_json_is_accepted_without_a_retry(self):
        http = StubHTTP([FakeResponse(200, chat_payload('```json\n{"a": 1}\n```'))])
        response = build("moonshot", session=http).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert response.data == {"a": 1}
        assert response.attempts == 1

    def test_bounded_retries_then_clean_failure(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        http = StubHTTP([FakeResponse(200, chat_payload("nope"))] * 3)
        with pytest.raises(LLMError):
            build("qwen", session=http, retries=3).generate_json(
                [{"role": "user", "content": "x"}]
            )
        assert len(http.calls) == 3


class TestAnthropicAdapter:
    def _response(self, text='{"a":1}'):
        return FakeResponse(200, {
            "content": [{"type": "text", "text": text}],
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "id": "msg_1",
            "usage": {"input_tokens": 11, "output_tokens": 4},
        })

    def test_system_prompt_is_hoisted_and_key_is_a_header(self):
        http = StubHTTP([self._response()])
        provider = build("anthropic", session=http, model="claude-sonnet-4-5")
        provider.generate_json([
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ])
        call = http.calls[0]
        assert call["url"].endswith("/messages")
        assert call["headers"]["x-api-key"] == SECRET
        assert "anthropic-version" in call["headers"]
        assert "be terse" in call["json"]["system"]
        assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]
        assert "response_format" not in call["json"]

    def test_json_directive_added_because_there_is_no_json_mode(self):
        http = StubHTTP([self._response()])
        build("anthropic", session=http).generate_json([{"role": "user", "content": "x"}])
        assert "JSON" in http.calls[0]["json"]["system"]

    def test_token_usage_is_normalized(self):
        http = StubHTTP([self._response()])
        response = build("anthropic", session=http).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert response.prompt_tokens == 11
        assert response.completion_tokens == 4
        assert response.total_tokens == 15
        assert response.finish_reason == "end_turn"


class TestGeminiAdapter:
    def _response(self, text='{"a":1}'):
        return FakeResponse(200, {
            "candidates": [{
                "content": {"parts": [{"text": text}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 9, "candidatesTokenCount": 2, "totalTokenCount": 11
            },
            "modelVersion": "gemini-2.0-flash",
        })

    def test_model_in_path_and_key_in_query(self):
        http = StubHTTP([self._response()])
        provider = build("gemini", session=http, model="gemini-2.0-flash")
        provider.generate_json([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ])
        call = http.calls[0]
        assert "gemini-2.0-flash:generateContent" in call["url"]
        assert call["params"] == {"key": SECRET}
        assert call["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
        assert call["json"]["generationConfig"]["responseMimeType"] == "application/json"

    def test_assistant_role_is_renamed_to_model(self):
        http = StubHTTP([self._response()])
        build("gemini", session=http).generate_json([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ])
        roles = [c["role"] for c in http.calls[0]["json"]["contents"]]
        assert roles == ["user", "model"]

    def test_usage_is_normalized(self):
        http = StubHTTP([self._response()])
        response = build("gemini", session=http).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert (response.prompt_tokens, response.completion_tokens) == (9, 2)
        assert response.total_tokens == 11


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,exc_name",
        [(429, "RateLimited"), (402, "InsufficientBalance"), (401, "AuthenticationFailed")],
    )
    def test_transport_errors_map_to_shared_types(self, status, exc_name):
        from aios.services.llm import base as base_module

        http = StubHTTP([FakeResponse(status)])
        with pytest.raises(getattr(base_module, exc_name)):
            build("qwen", session=http).generate_json([{"role": "user", "content": "x"}])
        # Non-retryable: exactly one attempt.
        assert len(http.calls) == 1

    def test_missing_key_is_raised_before_any_request(self):
        http = StubHTTP([])
        with pytest.raises(MissingAPIKey):
            build("qwen", api_key="", session=http).generate_json(
                [{"role": "user", "content": "x"}]
            )
        assert http.calls == []

    def test_errors_never_leak_the_credential(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        http = StubHTTP([RuntimeError(f"boom {SECRET}")] * 2)
        with pytest.raises(LLMError) as exc:
            build("qwen", session=http, retries=2).generate_json(
                [{"role": "user", "content": "x"}]
            )
        assert SECRET not in str(exc.value)


class TestTestConnection:
    def test_success_reports_provider_and_latency(self):
        http = StubHTTP([FakeResponse(200, chat_payload('{"status":"ok"}', model="qwen-plus"))])
        result = build("qwen", session=http, model="qwen-plus").test_connection()
        assert result["ok"] is True
        assert result["model"] == "qwen-plus"
        assert result["provider_id"] == "qwen"
        assert result["latency_ms"] >= 0

    def test_failure_is_readable_and_scrubbed(self):
        http = StubHTTP([FakeResponse(401)])
        result = build("qwen", session=http).test_connection()
        assert result["ok"] is False
        assert "401" in result["error"]
        assert SECRET not in json.dumps(result, ensure_ascii=False)

    def test_never_raises(self):
        http = StubHTTP([RuntimeError("network down")] * 3)
        assert build("qwen", session=http, retries=1).test_connection()["ok"] is False


# --- credentials ------------------------------------------------------------

class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, username, value):
        self.store[(service, username)] = value

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring_service, "_backend", lambda: fake)
    return fake


class TestCredentialIsolation:
    def test_each_provider_gets_its_own_slot(self, fake_keyring):
        keyring_service.set_provider_key("deepseek", "sk-deep-1111")
        keyring_service.set_provider_key("qwen", "sk-qwen-2222")

        assert keyring_service.get_provider_key("deepseek") == "sk-deep-1111"
        assert keyring_service.get_provider_key("qwen") == "sk-qwen-2222"
        assert {u for _, u in fake_keyring.store} == {"provider:deepseek", "provider:qwen"}

    def test_deleting_one_leaves_the_others(self, fake_keyring):
        """Configuring 通义千问 must never disturb an existing DeepSeek key."""
        keyring_service.set_provider_key("deepseek", "sk-deep-1111")
        keyring_service.set_provider_key("qwen", "sk-qwen-2222")

        assert keyring_service.delete_provider_key("qwen") is True
        assert keyring_service.get_provider_key("qwen") is None
        assert keyring_service.get_provider_key("deepseek") == "sk-deep-1111"

    def test_provider_id_is_normalised(self, fake_keyring):
        keyring_service.set_provider_key("DeepSeek", "sk-x-1234")
        assert keyring_service.get_provider_key("deepseek") == "sk-x-1234"

    def test_has_provider_key(self, fake_keyring):
        assert keyring_service.has_provider_key("zhipu") is False
        keyring_service.set_provider_key("zhipu", "sk-z-1234")
        assert keyring_service.has_provider_key("zhipu") is True


class TestNoSecretsInDatabase:
    def test_provider_rows_never_hold_a_credential(self, session, fake_keyring):
        """Scan every DB file for the secret after configuring two providers."""
        from pathlib import Path

        from aios.database import get_engine
        from aios.repositories import providers as providers_repo
        from aios.services.provider_migration import sync_key_state

        deepseek_secret = "sk-deepseek-secret-1234"
        qwen_secret = "sk-qwen-secret-5678"

        keyring_service.set_provider_key("deepseek", deepseek_secret)
        sync_key_state(session, "deepseek")

        providers_repo.create_provider(
            session, provider_id="qwen", display_name="通义千问",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            default_model="qwen-plus",
        )
        keyring_service.set_provider_key("qwen", qwen_secret)
        sync_key_state(session, "qwen")
        session.commit()

        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert row.has_api_key is True
        assert row.api_key_last_four == "1234"
        assert row.masked_key.endswith("1234")
        assert deepseek_secret not in row.masked_key

        engine = get_engine()
        db_path = Path(engine.url.database)
        engine.dispose()

        scanned = 0
        for candidate in db_path.parent.glob(db_path.name + "*"):
            blob = candidate.read_bytes()
            scanned += 1
            assert deepseek_secret.encode() not in blob, candidate.name
            assert qwen_secret.encode() not in blob, candidate.name
        assert scanned >= 1

    def test_extra_config_is_not_a_place_for_secrets(self, session):
        """The column exists for vendor options; nothing writes a key into it."""
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert row.extra_config_json in (None, {})


# --- routing ----------------------------------------------------------------

class TestProviderResolution:
    def _add(self, session, provider_id, model, default=False):
        from aios.repositories import providers as providers_repo

        row = providers_repo.create_provider(
            session, provider_id=provider_id,
            display_name=get_preset(provider_id).display_name,
            base_url=get_preset(provider_id).default_base_url,
            default_model=model, has_api_key=True, api_key_last_four="0000",
        )
        if default:
            providers_repo.set_default(session, row)
        return row

    def test_default_provider_serves_every_task(self, db, session):
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.default_model = "deepseek-chat"
        providers_repo.set_default(session, row)
        session.commit()

        service = LLMService()
        service.load()
        for task in TASKS:
            resolution = service.resolve(task)
            assert resolution.provider_id == "deepseek"
            assert resolution.model == "deepseek-chat"
            assert resolution.from_route is False

    def test_task_override_wins(self, db, session):
        """TEST C: default DeepSeek, synthesis on GLM."""
        from aios.repositories import providers as providers_repo

        deepseek = providers_repo.get_by_provider_id(session, "deepseek")
        deepseek.default_model = "deepseek-chat"
        providers_repo.set_default(session, deepseek)
        glm = self._add(session, "zhipu", "glm-4-plus")
        providers_repo.set_route(session, "synthesis", glm.id, "glm-4-plus")
        session.commit()

        service = LLMService()
        service.load()
        assert service.resolve("topic_analysis").provider_id == "deepseek"
        assert service.resolve("event_matching").provider_id == "deepseek"

        synthesis = service.resolve("synthesis")
        assert synthesis.provider_id == "zhipu"
        assert synthesis.model == "glm-4-plus"
        assert synthesis.from_route is True

    def test_route_model_override_without_changing_provider(self, db, session):
        from aios.repositories import providers as providers_repo

        deepseek = providers_repo.get_by_provider_id(session, "deepseek")
        deepseek.default_model = "deepseek-chat"
        providers_repo.set_default(session, deepseek)
        providers_repo.set_route(session, "synthesis", deepseek.id, "deepseek-reasoner")
        session.commit()

        service = LLMService()
        service.load()
        assert service.resolve("synthesis").model == "deepseek-reasoner"
        assert service.resolve("topic_analysis").model == "deepseek-chat"

    def test_switching_default_needs_no_code_change(self, db, session):
        """TEST B/D: swap the engine entirely from configuration."""
        from aios.repositories import providers as providers_repo

        qwen = self._add(session, "qwen", "qwen-plus", default=True)
        session.commit()

        service = LLMService()
        service.load()
        assert service.resolve("topic_analysis").provider_id == "qwen"

        kimi = self._add(session, "moonshot", "moonshot-v1-32k")
        providers_repo.set_default(session, kimi)
        session.commit()

        service = LLMService()
        service.load()
        assert service.resolve("topic_analysis").provider_id == "moonshot"

    def test_disabled_provider_is_not_used(self, db, session):
        from aios.repositories import providers as providers_repo

        deepseek = providers_repo.get_by_provider_id(session, "deepseek")
        deepseek.default_model = "deepseek-chat"
        providers_repo.set_default(session, deepseek)
        qwen = self._add(session, "qwen", "qwen-plus")
        session.commit()

        deepseek.enabled = False
        session.commit()

        service = LLMService()
        service.load()
        assert service.resolve("topic_analysis").provider_id == "qwen"

    def test_no_providers_is_a_clean_error(self, db, session):
        from aios.repositories import providers as providers_repo

        for row in providers_repo.list_providers(session):
            providers_repo.delete_provider(session, row)
        session.commit()

        service = LLMService()
        service.load()
        with pytest.raises(ProviderNotConfigured):
            service.resolve("topic_analysis")
        assert service.has_key is False
        assert "AI" in service.readiness_error()

    def test_readiness_reports_a_missing_credential(self, db, session):
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.default_model = "deepseek-chat"
        row.has_api_key = False
        session.commit()

        service = LLMService()
        service.load()
        assert "API Key" in service.readiness_error()

    def test_provider_instances_are_cached_per_model(self, db, session):
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.default_model = "deepseek-chat"
        providers_repo.set_default(session, row)
        session.commit()

        service = LLMService()
        service.load()
        assert service.provider_for("topic_analysis") is service.provider_for("module_analysis")

    def test_describe_routing_covers_every_task(self, db, session):
        from aios.repositories import providers as providers_repo

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.default_model = "deepseek-chat"
        providers_repo.set_default(session, row)
        session.commit()

        service = LLMService()
        service.load()
        assert set(service.describe_routing()) == set(TASKS)


class TestLegacyMigration:
    def test_existing_deepseek_settings_become_a_provider_row(self, paths, fake_keyring):
        """TEST A: an old install keeps working, with its key intact."""
        from aios.database import configure_engine, init_db, session_scope
        from aios.repositories import providers as providers_repo
        from aios.services import settings_service

        configure_engine(paths.db_path)
        init_db(paths, seed=False)

        # Simulate a pre-multi-provider install.
        with session_scope() as session:
            settings_service.ensure_default_settings(session)
            settings_service.set_many(session, {
                "deepseek_base_url": "https://my-proxy.example/v1",
                "deepseek_model": "deepseek-custom",
                "deepseek_max_tokens": "8000",
                "deepseek_timeout": "240",
            })
        keyring_service.set_api_key("sk-legacy-key-4242")

        from aios.services.provider_migration import migrate_legacy_provider

        with session_scope() as session:
            assert migrate_legacy_provider(session) is True

        with session_scope() as session:
            row = providers_repo.get_by_provider_id(session, "deepseek")
            assert row is not None
            assert row.is_default is True
            assert row.base_url == "https://my-proxy.example/v1"
            assert row.default_model == "deepseek-custom"
            assert row.max_tokens == 8000
            assert row.timeout_seconds == 240
            assert row.has_api_key is True
            assert row.api_key_last_four == "4242"

        # The credential was copied, not re-requested.
        assert keyring_service.get_provider_key("deepseek") == "sk-legacy-key-4242"

    def test_migration_runs_only_once(self, db, session):
        from aios.repositories import providers as providers_repo
        from aios.services.provider_migration import migrate_legacy_provider

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.default_model = "changed-by-user"
        session.commit()

        assert migrate_legacy_provider(session) is False
        session.expire_all()
        assert providers_repo.get_by_provider_id(session, "deepseek").default_model == (
            "changed-by-user"
        )

    def test_fresh_install_defaults_to_deepseek(self, db, session):
        from aios.repositories import providers as providers_repo

        assert providers_repo.default_provider(session).provider_id == "deepseek"


class TestUsageRecording:
    def test_usage_rows_carry_the_provider(self, db, session):
        from aios.repositories import runs as runs_repo
        from aios.timeutil import local_today

        run = runs_repo.create_run(session, local_today(), "manual")
        runs_repo.add_usage(
            session, run_id=run.id, purpose="topic_analysis",
            provider_id="qwen", model="qwen-plus",
            prompt_tokens=10, completion_tokens=5, total_tokens=15, latency_ms=120,
        )
        session.commit()

        summary = runs_repo.usage_summary(session, run.id)
        assert summary["calls"] == 1
        assert summary["total_tokens"] == 15

    def test_unreported_tokens_stay_null(self, db, session):
        from aios.models import LLMUsage
        from aios.repositories import runs as runs_repo
        from aios.timeutil import local_today

        run = runs_repo.create_run(session, local_today(), "manual")
        runs_repo.add_usage(
            session, run_id=run.id, purpose="synthesis",
            provider_id="ollama", model="qwen3:32b", latency_ms=90,
        )
        session.commit()

        row = session.query(LLMUsage).one()
        assert row.total_tokens is None
        assert row.provider_id == "ollama"


class TestKeyMirrorReconciliation:
    """The ``has_api_key`` column mirrors the vault and must not drift.

    A drifted mirror made the app refuse to start a run while the configured
    model was reachable, which is the bug this reconciliation exists to stop.
    """

    def test_a_stored_key_the_database_missed_is_recovered(self, session, db):
        from aios.repositories import providers as providers_repo
        from aios.services import keyring_service
        from aios.services.provider_migration import reconcile_key_state

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.has_api_key = False
        row.api_key_last_four = ""
        session.flush()

        keyring_service.set_provider_key("deepseek", "sk-" + "a" * 16 + "ef21")

        assert reconcile_key_state(session) == 1
        session.refresh(row)
        assert row.has_api_key is True
        assert row.api_key_last_four == "ef21"
        assert row.is_configured

    def test_a_removed_key_clears_the_mirror(self, session, db):
        from aios.repositories import providers as providers_repo
        from aios.services.provider_migration import reconcile_key_state

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.has_api_key = True
        row.api_key_last_four = "9999"
        session.flush()

        assert reconcile_key_state(session) == 1
        session.refresh(row)
        assert row.has_api_key is False
        assert row.api_key_last_four == ""

    def test_an_unavailable_vault_never_declares_keys_gone(self, session, db, monkeypatch):
        """Losing access to the vault must not look like losing the keys."""
        from aios.repositories import providers as providers_repo
        from aios.services import keyring_service, provider_migration

        row = providers_repo.get_by_provider_id(session, "deepseek")
        row.has_api_key = True
        row.api_key_last_four = "ef21"
        session.flush()

        monkeypatch.setattr(keyring_service, "is_available", lambda: False)
        monkeypatch.setattr(keyring_service, "get_provider_key", lambda pid: None)

        assert provider_migration.reconcile_key_state(session) == 0
        session.refresh(row)
        assert row.has_api_key is True

    def test_a_correct_mirror_is_left_alone(self, session, db):
        from aios.repositories import providers as providers_repo
        from aios.services import keyring_service
        from aios.services.provider_migration import reconcile_key_state

        keyring_service.set_provider_key("deepseek", "sk-" + "b" * 16 + "1234")
        reconcile_key_state(session)
        assert reconcile_key_state(session) == 0

        row = providers_repo.get_by_provider_id(session, "deepseek")
        assert row.api_key_last_four == "1234"
