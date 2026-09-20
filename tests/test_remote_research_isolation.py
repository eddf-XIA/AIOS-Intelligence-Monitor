"""A remote Simple-mode run does its own reading. AIOS only remembers.

These are the regression tests for the v2.2 architectural correction. Simple
mode used to fall back to the local collection agent whenever the configured
provider had no web search, which meant a DeepSeek user pressed 开始研究 and
got a report assembled from this machine's Google News / GDELT / RSS
collectors - inheriting every anti-scraping, rate-limit, VPN and routing
failure that Simple mode exists to remove, while the UI implied their model had
done the research.

The rule these tests hold in place:

    When the selected Research Agent is remote, **nothing local collects.**

So the whole classic collection surface is replaced with tripwires that fail
the test if they are touched, and a real remote agent - built by the real
registry, not a stub - is driven end to end over a fake HTTP transport. What
lands in the database must be the agent's own citations, and it must still
become ordinary AIOS intelligence: IntelligenceEvent, EventObservation,
ObservationSource, and NEW/UPDATED across two runs.
"""

from __future__ import annotations

import json
import json as _json

import pytest

from aios.models import EventObservation, IntelligenceEvent, RawArticle, RunStatus
from aios.repositories import providers as providers_repo
from aios.repositories import reports as reports_repo
from aios.repositories import research_topics as topics_repo
from aios.services.research import (
    AGENT_LOCAL,
    BACKEND_LOCAL,
    BACKEND_REMOTE,
    build_agent,
    engine_status,
    set_selection,
)
from aios.services.research.catalog import (
    AGENT_OPENAI_WEB,
    CHAT_ONLY_PROVIDERS,
    agents_for_provider,
    all_agents,
    default_agent_for_provider,
    get_agent,
    local_research_agents,
    remote_research_agents,
)
from aios.services.research.local_agent import LocalCollectionAgent
from aios.services.research.web_agents import WebResearchAgent
from aios.services.research_pipeline import ResearchPipeline, create_research_run

REAL_KEY = "sk-remote-isolation-000000000000000000"


#: A provider whose only integration really is chat. Deliberately *not*
#: DeepSeek: DeepSeek's Anthropic-compatible endpoint was verified live to run
#: server-side web search, so using it as the chat-only example would encode a
#: falsehood into the test suite.
CHAT_ONLY_PROVIDER = "ollama"


def _configure_provider(session, provider_id, model):
    """Make one provider usable.

    A fresh install already carries some provider rows with no credential, so
    this updates rather than inserts - the same thing the setup form does.
    """
    row = providers_repo.get_by_provider_id(session, provider_id)
    if row is None:
        row = providers_repo.create_provider(
            session,
            provider_id=provider_id,
            display_name=provider_id,
            enabled=True,
        )
    row.display_name = row.display_name or provider_id
    row.default_model = model
    row.enabled = True
    session.flush()
    return row


# --- the tripwires ----------------------------------------------------------

class LocalCollectionWasUsed(AssertionError):
    """Raised the moment a remote run touches anything that collects locally."""


@pytest.fixture
def no_local_collection(monkeypatch):
    """Make every local collection path explode.

    Patched at the definition site rather than at the import site, so a future
    refactor that reaches the collectors by a new route still trips this. The
    list is deliberately exhaustive rather than minimal: the claim under test
    is "none of this runs", and a tripwire that only covers the paths we
    happen to know about today would not support that claim tomorrow.
    """
    from aios.services import collection_planner, collector
    from aios.services import article_extractor

    tripped: list[str] = []

    def forbid(name):
        def _boom(*args, **kwargs):
            tripped.append(name)
            raise LocalCollectionWasUsed(
                f"a remote research run called {name}; Simple mode must not "
                f"collect locally"
            )
        return _boom

    # The collectors themselves, at every entry point they expose.
    for cls, label in (
        (collector.GDELTCollector, "GDELT"),
        (collector.GoogleNewsCollector, "Google News"),
        (collector.RSSCollector, "RSS"),
        (collector.SourceCollector, "SourceCollector"),
    ):
        for method in ("fetch", "search", "_fetch"):
            if hasattr(cls, method):
                monkeypatch.setattr(cls, method, forbid(f"{label}.{method}"))
    # The registry that fans out to them.
    monkeypatch.setattr(
        collector.CollectorRegistry, "collect", forbid("CollectorRegistry.collect")
    )
    monkeypatch.setattr(
        collector.CollectorRegistry,
        "collect_detailed",
        forbid("CollectorRegistry.collect_detailed"),
    )
    # The planner that drives the registry.
    monkeypatch.setattr(
        collection_planner.CollectionPlanner,
        "__init__",
        forbid("CollectionPlanner()"),
    )
    monkeypatch.setattr(
        collection_planner.CollectionPlanner,
        "collect_topic",
        forbid("CollectionPlanner.collect_topic"),
    )
    monkeypatch.setattr(
        collection_planner.CollectionPlanner,
        "collect_query",
        forbid("CollectionPlanner.collect_query"),
    )
    # Fetching article bodies is local retrieval too.
    monkeypatch.setattr(
        article_extractor.ArticleExtractor, "fetch", forbid("ArticleExtractor.fetch")
    )
    # And the local agent as a whole, in case selection itself regresses.
    monkeypatch.setattr(LocalCollectionAgent, "run", forbid("LocalCollectionAgent"))

    return tripped


# --- a real remote agent over a fake transport ------------------------------

def research_payload(
    title="OpenAI 发布新的研究型 Agent",
    summary="OpenAI 推出可自主检索与交叉验证的研究型 Agent。",
    event_date="2026-09-19",
    url="https://openai.com/index/research-agent",
    publisher="OpenAI Newsroom",
    extra_sources=(),
    extra_events=(),
):
    """A ResearchResult exactly as a remote agent would return it."""
    sources = [
        {
            "source_id": 0,
            "title": title,
            "publisher": publisher,
            "url": url,
            "published_at": event_date,
        },
        {
            "source_id": 1,
            "title": "分析：研究型 Agent 的竞争格局",
            "publisher": "Reuters",
            "url": "https://www.reuters.com/technology/research-agents-2026",
            "published_at": event_date,
        },
    ]
    sources.extend(extra_sources)
    events = [
        {
            "title": title,
            "summary": summary,
            "entities": ["OpenAI"],
            "organization": "OpenAI",
            "product_or_project": "Research Agent",
            "event_type": "product_launch",
            "event_date": event_date,
            "significance": "判断：研究型 Agent 正在成为独立产品品类。",
            "importance": 1,
            "section": "AI 基础设施",
            "source_ids": [0, 1],
        }
    ]
    events.extend(extra_events)
    return {
        "coverage": {"status": "complete", "sources_examined": len(sources)},
        "report": {
            "title": "AI 研究型 Agent 监测日报",
            "focus_title": title,
            "focus_summary": summary,
            "sections": [
                {
                    "name": "AI 基础设施",
                    "summary": "本期以研究型 Agent 的产品化为主。",
                    "summary_items": [
                        {
                            "headline": title,
                            "body": summary,
                            "significance": "判断：竞争从模型转向研究流程。",
                            "evidence_ids": [0, 1],
                        }
                    ],
                }
            ],
            "market_snapshot": [],
            "trend_analysis": [
                {
                    "title": "趋势：研究流程产品化",
                    "analysis": "判断：检索与验证正在从用户侧转移到服务侧。",
                    "confidence": "medium",
                    "evidence_ids": [0],
                }
            ],
        },
        "events": events,
        "sources": sources,
        "watch_next": ["第三方研究型 Agent 的引用质量"],
    }


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeOpenAISession:
    """Stands in for ``requests.Session`` against the OpenAI Responses API.

    Records every URL so a test can assert that the *only* network the run
    performed was to the research agent's own endpoint.
    """

    def __init__(self, result_payload: dict):
        self.result_payload = result_payload
        self.urls: list[str] = []
        self.bodies: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        # ``json`` is requests' keyword for the request body and shadows the
        # module here, so encoding goes through the aliased import.
        self.urls.append(url)
        self.bodies.append(json)
        return FakeResponse(
            {
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _json.dumps(self.result_payload, ensure_ascii=False),
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 900, "output_tokens": 2400, "total_tokens": 3300},
            }
        )

    def get(self, url, **kwargs):  # pragma: no cover - nothing should call this
        raise LocalCollectionWasUsed(f"unexpected outbound GET to {url}")


@pytest.fixture
def remote_engine(session):
    """A configured, ready, genuinely remote research engine."""
    from aios.services import keyring_service
    from aios.services.provider_migration import sync_key_state

    row = providers_repo.create_provider(
        session,
        provider_id="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4.1",
        enabled=True,
    )
    keyring_service.set_provider_key("openai", REAL_KEY)
    sync_key_state(session, "openai")
    providers_repo.set_default(session, row)
    set_selection(session, "openai", AGENT_OPENAI_WEB)
    session.commit()
    return row


@pytest.fixture
def topic(session):
    topic = topics_repo.create_topic(
        session,
        name="AI 研究型 Agent",
        brief="跟踪具备自主检索能力的研究型 Agent 的产品与能力进展。",
        scope="OpenAI / Anthropic / Google",
        focus_areas=["产品发布"],
        exclusions=["招聘"],
        keywords=["research agent"],
        regions="全球",
        window_hours=72,
    )
    session.commit()
    return topic


@pytest.fixture
def stub_llm(monkeypatch):
    """Event matching falls to the deterministic rules path, as elsewhere."""
    from aios.services import research_pipeline
    from aios.services.llm import LLMError

    class StubLLM:
        def readiness_error(self):
            return ""

        def complete_json(self, *args, **kwargs):
            raise LLMError("no provider in tests")

        def describe_routing(self):
            return {}

    monkeypatch.setattr(research_pipeline, "build_llm_service", lambda **kw: StubLLM())


def run_remote(session, monkeypatch, topic_id, payload):
    """One full pipeline run driven by the real remote agent. Returns ids."""
    http = FakeOpenAISession(payload)

    run = create_research_run(session, topic_id)
    run_id = run.id
    session.commit()

    pipeline = ResearchPipeline(run_id)
    pipeline._http = http
    status = pipeline.execute()
    session.expire_all()
    return run_id, status, http


# --- the architecture ------------------------------------------------------

class TestBackendsAreDistinctTypes:
    """Remote and local are two backend kinds, not two settings of one."""

    def test_every_remote_agent_declares_itself_remote(self):
        for spec in remote_research_agents():
            assert spec.backend_kind == BACKEND_REMOTE
            assert spec.is_remote is True
            assert spec.is_local is False

    def test_the_local_backend_declares_itself_local(self):
        locals_ = local_research_agents()
        assert [spec.agent_id for spec in locals_] == [AGENT_LOCAL]
        assert locals_[0].backend_kind == BACKEND_LOCAL
        assert locals_[0].is_remote is False

    def test_the_two_lists_never_overlap(self):
        remote = {spec.agent_id for spec in remote_research_agents()}
        local = {spec.agent_id for spec in local_research_agents()}
        assert remote and local
        assert remote.isdisjoint(local)

    def test_only_the_local_backend_depends_on_the_users_network(self):
        """网络模式 is irrelevant to a remote run, and must say so.

        A user whose report is written on OpenAI's servers should never be
        told to check their Google News reachability.
        """
        for spec in remote_research_agents():
            assert spec.requires_local_network is False
        for spec in local_research_agents():
            assert spec.requires_local_network is True

    def test_the_local_backend_is_labelled_honestly(self):
        spec = get_agent(AGENT_LOCAL)
        assert "高级" in spec.display_name
        assert "本地" in spec.display_name
        # The description says which machine does the fetching.
        assert "本机" in spec.description

    def test_the_local_backend_is_not_offered_by_default_to_anyone(self):
        for provider_id in ("openai", "anthropic", "gemini", *CHAT_ONLY_PROVIDERS):
            offered = {spec.agent_id for spec in agents_for_provider(provider_id)}
            assert AGENT_LOCAL not in offered


class TestARemoteRunNeverCollectsLocally:
    """The central regression. None of the classic collection surface runs."""

    def test_the_registry_builds_a_remote_agent_not_a_local_one(
        self, session, remote_engine
    ):
        agent = build_agent(session)
        assert isinstance(agent, WebResearchAgent)
        assert not isinstance(agent, LocalCollectionAgent)
        assert agent.spec.backend_kind == BACKEND_REMOTE

    def test_a_full_remote_run_touches_no_collector(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        """Google News, GDELT, RSS, CollectionPlanner: all tripwired, none hit."""
        run_id, status, http = run_remote(
            session, monkeypatch, topic.id, research_payload()
        )

        assert status == RunStatus.COMPLETED
        assert no_local_collection == [], "a local collection path was called"

    def test_the_only_network_call_is_to_the_research_agent(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        _, status, http = run_remote(session, monkeypatch, topic.id, research_payload())

        assert status == RunStatus.COMPLETED
        assert http.urls == ["https://api.openai.com/v1/responses"]

    def test_the_users_collection_mode_is_irrelevant_to_a_remote_run(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        """China mode, international mode - a remote run behaves identically.

        This is the promise that Simple mode removes network configuration
        from the user's problem: only the agent's endpoint has to be
        reachable.
        """
        from aios.services import settings_service

        outcomes = {}
        for mode in ("china", "international"):
            settings_service.set_many(session, {"collection_mode": mode})
            session.commit()
            run_id, status, http = run_remote(
                session, monkeypatch, topic.id, research_payload()
            )
            outcomes[mode] = (status, tuple(http.urls))

        assert outcomes["china"] == outcomes["international"]
        assert all(status == RunStatus.COMPLETED for status, _ in outcomes.values())
        assert no_local_collection == []


class TestEvidenceComesFromTheAgent:
    """Sources are the agent's citations - AIOS does not rediscover them."""

    def test_every_stored_source_is_one_the_agent_cited(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        payload = research_payload()
        cited = {source["url"] for source in payload["sources"]}

        run_id, status, _ = run_remote(session, monkeypatch, topic.id, payload)
        assert status == RunStatus.COMPLETED

        stored = {article.url for article in session.query(RawArticle).all()}
        assert stored == cited

    def test_stored_evidence_is_attributed_to_the_agent_not_a_collector(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        """The audit trail must distinguish a citation from a fetched document."""
        run_remote(session, monkeypatch, topic.id, research_payload())

        articles = session.query(RawArticle).all()
        assert articles
        for article in articles:
            assert article.collector == "agent"

    def test_a_source_the_agent_did_not_cite_never_appears(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        payload = research_payload()
        run_remote(session, monkeypatch, topic.id, payload)

        stored = {article.url for article in session.query(RawArticle).all()}
        # Nothing a collector would have found on its own.
        assert not any("news.google.com" in url for url in stored)
        assert not any("gdeltproject.org" in url for url in stored)
        assert len(stored) == len(payload["sources"])


class TestHistoryStillWorks:
    """Simple for the user, without giving up AIOS's intelligence memory."""

    def test_a_remote_run_creates_event_observation_and_evidence(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        run_id, status, _ = run_remote(
            session, monkeypatch, topic.id, research_payload()
        )
        assert status == RunStatus.COMPLETED

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1
        assert events[0].organization == "OpenAI"
        assert events[0].research_topic_id == topic.id

        observations = session.query(EventObservation).all()
        assert len(observations) == 1
        assert observations[0].run_id == run_id

        # The evidence chain is intact, and every link is an agent citation.
        from aios.repositories import events as events_repo

        observation = events_repo.get_observation(session, observations[0].id)
        assert len(observation.sources) == 2
        for link in observation.sources:
            assert link.article is not None
            assert link.article.collector == "agent"

    def test_the_first_sighting_is_new(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        run_id, _, _ = run_remote(session, monkeypatch, topic.id, research_payload())

        report = reports_repo.get_by_run(session, run_id)
        assert report.sections[0].items[0].event_state == "new"

    def test_a_second_run_on_the_same_story_is_updated_not_new(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        """The same story, reworded by the agent, must join one timeline.

        This is the whole reason the remote agent returns machine-facing event
        records alongside the human report: without them a reworded headline
        on day two would open a second event and the history would fork.
        """
        first_run, _, _ = run_remote(session, monkeypatch, topic.id, research_payload())

        second_run, _, _ = run_remote(
            session,
            monkeypatch,
            topic.id,
            research_payload(
                title="OpenAI 研究型 Agent 开放更多地区",
                summary="OpenAI 的研究型 Agent 扩大可用范围，并公布引用质量数据。",
                event_date="2026-09-20",
            ),
        )

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1, "one real-world story must be one event"

        # One event, two observations: a timeline, not two unrelated items.
        observations = session.query(EventObservation).all()
        assert len(observations) == 2

        assert reports_repo.get_by_run(session, first_run).sections[0].items[
            0
        ].event_state == "new"
        assert reports_repo.get_by_run(session, second_run).sections[0].items[
            0
        ].event_state == "updated"

    def test_history_is_built_without_a_single_local_fetch(
        self, session, monkeypatch, remote_engine, topic, stub_llm, no_local_collection
    ):
        """Two runs, full NEW/UPDATED history, zero local collection."""
        run_remote(session, monkeypatch, topic.id, research_payload())
        run_remote(
            session,
            monkeypatch,
            topic.id,
            research_payload(event_date="2026-09-20"),
        )
        assert no_local_collection == []


class TestAChatOnlyProviderCannotBeARemoteAgent:
    """A chat-completion endpoint is not a Research Agent, and cannot be made one."""

    @pytest.mark.parametrize("provider_id", CHAT_ONLY_PROVIDERS)
    def test_no_chat_only_provider_offers_a_remote_agent(self, provider_id):
        assert agents_for_provider(provider_id) == []
        assert default_agent_for_provider(provider_id) is None

    def test_a_chat_only_agent_spec_is_never_research_capable(self):
        spec = get_agent(f"{CHAT_ONLY_PROVIDER}_chat")
        assert spec is not None
        assert spec.is_research_capable is False
        assert spec.unavailable_reason

    def test_the_reason_blames_the_integration_not_the_vendor(self):
        """Wording must not make a claim about what a vendor can ever do.

        DeepSeek is the proof: the same credential is chat-only through one
        endpoint and research-capable through another. A blanket
        "该服务不支持联网研究" was simply false, and it hid a working engine
        from the users most likely to have it.
        """
        for spec in all_agents():
            if spec.is_research_capable:
                continue
            assert "尚未启用" in spec.unavailable_reason
            assert "不支持" not in spec.unavailable_reason

    def test_selecting_a_chat_only_agent_is_rejected_by_the_form(
        self, client, session
    ):
        """Posting a chat-only agent id is refused with the reason, not stored."""
        response = client.post(
            "/simple/engine",
            data={
                "provider_id": CHAT_ONLY_PROVIDER,
                "default_model": "qwen2.5",
                "api_key": REAL_KEY,
                "agent_id": f"{CHAT_ONLY_PROVIDER}_chat",
                "action": "save",
            },
            follow_redirects=False,
        )
        # Re-rendered with an explanation rather than redirected as a success.
        assert response.status_code == 200
        assert "尚未启用远程联网研究" in response.text

        session.expire_all()
        from aios.services.research.registry import selected_agent_id

        assert selected_agent_id(session) != f"{CHAT_ONLY_PROVIDER}_chat"

    def test_a_chat_only_provider_leaves_the_engine_unready(self, client, session):
        """Saving it configures the provider but selects no research agent."""
        client.post(
            "/simple/engine",
            data={
                "provider_id": CHAT_ONLY_PROVIDER,
                "default_model": "qwen2.5",
                "api_key": REAL_KEY,
                "action": "save",
            },
            follow_redirects=False,
        )
        session.expire_all()

        status = engine_status(session)
        assert status.ready is False
        assert status.agent_id == ""
        assert "尚未启用远程联网研究" in status.error

    def test_building_an_agent_for_a_chat_only_provider_raises(self, session):
        """No silent substitution at the last moment either."""
        from aios.services import keyring_service
        from aios.services.provider_migration import sync_key_state
        from aios.services.research import ResearchAgentUnavailable

        row = _configure_provider(session, CHAT_ONLY_PROVIDER, "qwen2.5")
        keyring_service.set_provider_key(CHAT_ONLY_PROVIDER, REAL_KEY)
        sync_key_state(session, CHAT_ONLY_PROVIDER)
        providers_repo.set_default(session, row)
        set_selection(session, CHAT_ONLY_PROVIDER, "")
        session.commit()

        with pytest.raises(ResearchAgentUnavailable):
            build_agent(session)


class TestTheLocalBackendIsPreservedNotDeleted:
    """Reclassified, not removed. It is still fully supported when chosen."""

    def test_it_is_still_in_the_catalog_and_research_capable(self):
        spec = get_agent(AGENT_LOCAL)
        assert spec is not None
        assert spec.is_research_capable is True

    def test_it_is_reachable_by_asking_for_it_explicitly(self):
        offered = {
            spec.agent_id
            for spec in agents_for_provider("deepseek", include_local=True)
        }
        assert AGENT_LOCAL in offered

    def test_choosing_it_explicitly_builds_the_local_agent(self, session):
        from aios.services import keyring_service
        from aios.services.provider_migration import sync_key_state

        row = _configure_provider(session, CHAT_ONLY_PROVIDER, "qwen2.5")
        keyring_service.set_provider_key(CHAT_ONLY_PROVIDER, REAL_KEY)
        sync_key_state(session, CHAT_ONLY_PROVIDER)
        providers_repo.set_default(session, row)
        set_selection(session, CHAT_ONLY_PROVIDER, AGENT_LOCAL)
        session.commit()

        status = engine_status(session)
        assert status.ready is True
        assert status.backend_kind == BACKEND_LOCAL

        agent = build_agent(session)
        assert isinstance(agent, LocalCollectionAgent)
