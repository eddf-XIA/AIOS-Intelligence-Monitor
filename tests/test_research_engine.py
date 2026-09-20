"""研究引擎: inline setup, credential handling and capability honesty.

The most important thing tested here is the last one. Every LLM will cheerfully
answer "what happened in humanoid robotics this week?" from training data, with
invented dates and no sources. Presenting a chat-only endpoint as "Deep
Research" would make AIOS produce confident fiction, so the capability gate has
tests of its own.
"""

from __future__ import annotations

import pytest

from aios.repositories import providers as providers_repo
from aios.services.research import (
    AGENT_LOCAL,
    agents_for_provider,
    all_agents,
    default_agent_for_provider,
    engine_status,
    get_agent,
    native_agent_for_provider,
    research_capable_agents,
    set_selection,
)
from aios.services.research.catalog import (
    AGENT_ANTHROPIC_WEB,
    AGENT_GEMINI_GROUNDING,
    AGENT_OPENAI_WEB,
    CHAT_ONLY_PROVIDERS,
)

REAL_KEY = "sk-" + "a" * 32


def _configure(session, provider_id="deepseek", model="deepseek-chat", key=REAL_KEY):
    """Make one provider genuinely usable, the way the UI would."""
    from aios.services import keyring_service
    from aios.services.provider_migration import sync_key_state

    row = providers_repo.get_by_provider_id(session, provider_id)
    if row is None:
        row = providers_repo.create_provider(
            session, provider_id=provider_id, display_name=provider_id, enabled=True
        )
    row.default_model = model
    row.enabled = True
    session.flush()
    if key:
        keyring_service.set_provider_key(provider_id, key)
        sync_key_state(session, provider_id)
    providers_repo.set_default(session, row)
    session.commit()
    return row


class TestFirstTimeSetup:
    def test_no_usable_provider_shows_the_inline_setup_state(self, client, session):
        """A fresh install has a provider *row* but no key - that is not configured."""
        status = engine_status(session)
        assert status.configured is False
        assert status.ready is False

        body = client.get("/").text
        assert "尚未配置研究引擎" in body
        assert "添加 AI 服务" in body

    def test_setup_is_inline_not_a_separate_settings_area(self, client):
        body = client.get("/simple/engine").text
        assert "返回主页" in body
        # The professional settings tree is not part of this page.
        assert "监测配置" not in body

    def test_configured_provider_shows_the_connected_state(self, client, session):
        _configure(session)
        set_selection(session, "deepseek", AGENT_LOCAL)
        session.commit()

        status = engine_status(session)
        assert status.configured is True
        assert status.ready is True
        assert status.status_label == "可用"

        body = client.get("/").text
        assert "尚未配置研究引擎" not in body
        assert "可用" in body

    def test_missing_model_is_reported_specifically(self, client, session):
        _configure(session, model="")
        status = engine_status(session)
        assert status.configured is False
        assert "模型名称" in status.error

    def test_saving_through_the_inline_form_returns_to_home(self, client, session):
        response = client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": REAL_KEY,
                "agent_id": AGENT_LOCAL,
                "action": "save",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/?")

        session.expire_all()
        assert engine_status(session).ready is True


class TestCredentialHandling:
    def test_api_key_is_stored_via_keyring_only(self, client, session, isolated_keyring):
        client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": REAL_KEY,
                "agent_id": AGENT_LOCAL,
                "action": "save",
            },
            follow_redirects=False,
        )

        # In the vault...
        assert REAL_KEY in isolated_keyring.store.values()

        # ...and nowhere in the database.
        from sqlalchemy import text

        from aios.database import get_engine

        with get_engine().connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ]
            for table in tables:
                columns = [
                    row[1]
                    for row in connection.execute(text(f"PRAGMA table_info({table})"))
                ]
                for column in columns:
                    values = connection.execute(
                        text(f'SELECT "{column}" FROM "{table}"')  # noqa: S608
                    ).fetchall()
                    for (value,) in values:
                        assert REAL_KEY != value, f"{table}.{column} holds the raw key"
                        if isinstance(value, str):
                            assert REAL_KEY not in value, f"{table}.{column} contains the key"

    def test_key_never_appears_in_html(self, client, session):
        client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": REAL_KEY,
                "agent_id": AGENT_LOCAL,
                "action": "save",
            },
            follow_redirects=False,
        )
        for url in ("/", "/simple/engine", "/dashboard", "/settings/ai"):
            assert REAL_KEY not in client.get(url).text, url

    def test_only_the_masked_tail_reaches_the_page(self, client, session):
        _configure(session)
        body = client.get("/simple/engine").text
        assert REAL_KEY not in body
        assert REAL_KEY[-4:] in body

    def test_a_masked_placeholder_is_not_saved_as_a_key(self, client, session, isolated_keyring):
        """Re-submitting the form without retyping the key must not clobber it."""
        _configure(session)
        stored = dict(isolated_keyring.store)

        client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": "••••••••••••" + REAL_KEY[-4:],
                "agent_id": AGENT_LOCAL,
                "action": "save",
            },
            follow_redirects=False,
        )
        assert isolated_keyring.store == stored


class TestConnectionTest:
    def test_failure_is_shown_cleanly(self, client, session, monkeypatch):
        from aios.routers import simple as simple_router

        _configure(session)

        def failing(row, **kwargs):
            return {"ok": False, "error": "连接超时", "latency_ms": 0}

        monkeypatch.setattr(
            "aios.services.llm.service.test_provider_row", failing
        )

        body = client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": "",
                "agent_id": AGENT_LOCAL,
                "action": "test",
            },
        ).text
        assert "连接失败" in body
        assert "连接超时" in body
        # A failed test is not a stack trace and not a 500.
        assert "Traceback" not in body

    def test_success_is_shown_cleanly(self, client, session, monkeypatch):
        _configure(session)
        monkeypatch.setattr(
            "aios.services.llm.service.test_provider_row",
            lambda row, **kw: {"ok": True, "model": "deepseek-chat", "latency_ms": 42},
        )
        body = client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": "",
                "agent_id": AGENT_LOCAL,
                "action": "test",
            },
        ).text
        assert "连接成功" in body

    def test_a_failed_test_does_not_lose_the_saved_key(
        self, client, session, monkeypatch, isolated_keyring
    ):
        _configure(session)
        monkeypatch.setattr(
            "aios.services.llm.service.test_provider_row",
            lambda row, **kw: {"ok": False, "error": "x", "latency_ms": 0},
        )
        client.post(
            "/simple/engine",
            data={
                "provider_id": "deepseek",
                "default_model": "deepseek-chat",
                "api_key": "",
                "agent_id": AGENT_LOCAL,
                "action": "test",
            },
        )
        from aios.services import keyring_service

        assert keyring_service.get_provider_key("deepseek") == REAL_KEY


class TestCapabilityHonesty:
    """A chat-completion endpoint is not a research agent."""

    def test_every_offered_agent_can_actually_research(self):
        for spec in research_capable_agents():
            assert spec.capabilities.supports_web_research is True
            assert spec.capabilities.supports_citations is True

    @pytest.mark.parametrize("provider_id", CHAT_ONLY_PROVIDERS)
    def test_chat_only_providers_have_no_native_research_agent(self, provider_id):
        assert native_agent_for_provider(provider_id) is None

    def test_a_chat_only_provider_is_not_exposed_as_research_capable(self):
        """A provider whose only integration is chat offers no remote agent."""
        native = native_agent_for_provider("ollama")
        assert native is None

        # Nothing is offered. Not the local collection agent dressed up as
        # "Ollama research" - nothing. A chat-only provider genuinely has no
        # remote research engine, and the UI must be able to say that.
        assert agents_for_provider("ollama") == []

        # It remains reachable, but only by asking for the local backend by
        # name and being told what that means.
        advanced = {
            spec.agent_id
            for spec in agents_for_provider("ollama", include_local=True)
        }
        assert advanced == {AGENT_LOCAL}

    def test_deepseek_is_research_capable_through_its_anthropic_endpoint(self):
        """Capability is per integration, not per vendor.

        DeepSeek's OpenAI-compatible chat surface ignores built-in tools, but
        its Anthropic-compatible endpoint really does run server-side web
        search - verified live against the real API on 2026-09-20 (6 searches,
        50 URLs, a schema-valid ResearchResult). Classifying the vendor as
        "cannot research" was factually wrong and hid a capable provider from
        the audience most likely to have it configured.
        """
        from aios.services.research.catalog import AGENT_DEEPSEEK_WEB

        native = native_agent_for_provider("deepseek")
        assert native is not None
        assert native.agent_id == AGENT_DEEPSEEK_WEB
        assert native.is_research_capable is True
        assert native.is_remote is True

        offered = {spec.agent_id for spec in agents_for_provider("deepseek")}
        assert offered == {AGENT_DEEPSEEK_WEB}

    def test_the_deepseek_chat_integration_is_still_not_research_capable(self):
        """The distinction the whole catalog turns on.

        Adding the web-search agent must not retroactively bless the plain
        chat endpoint: posting a web_search tool there is silently ignored and
        the model answers from training data.
        """
        chat = get_agent("deepseek_chat")
        if chat is not None:
            assert chat.is_research_capable is False

    def test_a_chat_only_agent_states_why_it_cannot_research(self):
        chat_agents = [
            spec for spec in all_agents() if not spec.is_research_capable
        ]
        assert chat_agents
        for spec in chat_agents:
            assert spec.unavailable_reason
            assert "不能联网" in spec.unavailable_reason or "不能" in spec.unavailable_reason

    def test_unsupported_model_is_not_selectable_as_research_capable(self, session):
        """A chat-only agent resolves to no engine, never to a substitute."""
        _configure(session, provider_id="ollama", model="qwen2.5")
        set_selection(session, "ollama", "ollama_chat")
        session.commit()

        status = engine_status(session)
        # The stored choice is rejected and nothing is put in its place.
        assert status.agent_id == ""
        assert status.ready is False
        assert "尚未启用远程联网研究" in status.error

    def test_a_vendor_agent_is_not_offered_for_another_vendor(self):
        offered = {spec.agent_id for spec in agents_for_provider("qwen")}
        assert AGENT_OPENAI_WEB not in offered
        assert AGENT_ANTHROPIC_WEB not in offered
        assert AGENT_GEMINI_GROUNDING not in offered

    @pytest.mark.parametrize(
        "provider_id,agent_id",
        [
            ("openai", AGENT_OPENAI_WEB),
            ("anthropic", AGENT_ANTHROPIC_WEB),
            ("gemini", AGENT_GEMINI_GROUNDING),
        ],
    )
    def test_a_vendor_with_web_search_gets_its_own_agent(self, provider_id, agent_id):
        offered = {spec.agent_id for spec in agents_for_provider(provider_id)}
        # Exactly the vendor's own remote agent. The local backend is not in
        # the default list for any provider.
        assert offered == {agent_id}
        assert AGENT_LOCAL not in offered

    def test_default_agent_prefers_the_vendors_own_research(self):
        assert default_agent_for_provider("openai").agent_id == AGENT_OPENAI_WEB

    def test_default_agent_never_falls_back_to_local_collection(self):
        """The core architectural correction.

        A provider with no remote research agent pre-selects *nothing*. The
        previous fallback to local collection is what made a DeepSeek user
        believe DeepSeek had researched their topic when the reading was
        actually done by this machine's RSS/GDELT collectors.
        """
        for provider_id in CHAT_ONLY_PROVIDERS:
            assert default_agent_for_provider(provider_id) is None

    def test_capability_labels_never_overclaim(self):
        for spec in all_agents():
            labels = spec.capability_labels()
            if not spec.capabilities.supports_web_research:
                assert "联网检索" not in labels
            if not spec.capabilities.supports_multi_step:
                assert "多轮研究" not in labels


class TestEngineReadiness:
    def test_readiness_error_blocks_a_run(self, client, session):
        """开始研究 must not start when the engine cannot run."""
        from aios.services.research import readiness_error

        assert readiness_error(session)

    def test_a_ready_engine_reports_no_error(self, session):
        from aios.services.research import readiness_error

        _configure(session)
        set_selection(session, "deepseek", AGENT_LOCAL)
        session.commit()
        assert readiness_error(session) == ""

    def test_building_an_unavailable_agent_raises_rather_than_guessing(self, session):
        from aios.services.research import ResearchAgentUnavailable, build_agent

        with pytest.raises(ResearchAgentUnavailable):
            build_agent(session)
