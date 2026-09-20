"""Resolving the configured Research Agent into something that can run.

One place answers "what will 开始研究 actually use?", so no router, template or
pipeline ever names a backend. Adding a new research provider means adding a
spec to :mod:`aios.services.research.catalog` and a branch here - nothing in
the UI changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from sqlalchemy.orm import Session

from ...repositories import providers as providers_repo
from .. import settings_service
from ..keyring_service import get_provider_key
from ..llm.registry import runtime_config
from ..llm.service import build_llm_service
from .base import (
    BACKEND_LOCAL,
    BACKEND_REMOTE,
    ProgressCallback,
    ResearchAgentSpec,
    ResearchAgentUnavailable,
    ResearchService,
)
from .catalog import (
    AGENT_ANTHROPIC_WEB,
    AGENT_DEEPSEEK_WEB,
    AGENT_GEMINI_GROUNDING,
    AGENT_LOCAL,
    AGENT_OPENAI_WEB,
    LOCAL_AGENT,
    NO_REMOTE_AGENT_REASON,
    agents_for_provider,
    default_agent_for_provider,
    get_agent,
    local_research_agents,
    remote_agents_for_provider,
    remote_research_agents,
    research_capable_agents,
)
from .local_agent import LocalCollectionAgent
from .web_agents import (
    AnthropicWebSearchAgent,
    DeepSeekWebSearchAgent,
    GeminiGroundingAgent,
    OpenAICompatibleSearchAgent,
    OpenAIResponsesAgent,
)

logger = logging.getLogger(__name__)

#: Settings keys holding the user's 研究引擎 choice.
SETTING_PROVIDER = "research_provider_id"
SETTING_AGENT = "research_agent_id"

NO_ENGINE_MESSAGE = "尚未配置研究引擎"
NO_PROVIDER_MESSAGE = "尚未配置 AI 服务，请先添加一个。"


@dataclass(frozen=True)
class EngineStatus:
    """What the 研究引擎 block renders. Never carries a credential.

    ``configured`` means *usable*, not merely "a row exists". A fresh install
    always has a DeepSeek provider row (carried over from the legacy
    single-provider settings by :mod:`aios.services.provider_migration`) with
    no credential in it - and a first-time user must see 尚未配置研究引擎, not
    a connected-looking engine with a warning bolted on.
    """

    configured: bool
    #: The chosen provider row id, when one is chosen and still exists.
    provider_config_id: Optional[int] = None
    provider_id: str = ""
    provider_name: str = ""
    model: str = ""
    agent_id: str = ""
    agent_name: str = ""
    #: ``BACKEND_REMOTE`` or ``BACKEND_LOCAL``. Drives whether the UI mentions
    #: local collection settings at all.
    backend_kind: str = BACKEND_REMOTE
    #: '' when the engine is ready, otherwise the reason it is not.
    error: str = ""
    #: Masked tail only. The full key never leaves the OS keyring.
    masked_key: str = ""

    @property
    def ready(self) -> bool:
        return self.configured and not self.error

    @property
    def is_remote(self) -> bool:
        return self.backend_kind == BACKEND_REMOTE

    @property
    def is_local(self) -> bool:
        return self.backend_kind == BACKEND_LOCAL

    @property
    def status_label(self) -> str:
        if self.ready:
            return "可用"
        if self.configured:
            return "不可用"
        return "未配置"


# --- reading and writing the choice -----------------------------------------

def selected_provider_id(session: Session) -> str:
    return settings_service.get_str(session, SETTING_PROVIDER, "").strip().lower()


def selected_agent_id(session: Session) -> str:
    return settings_service.get_str(session, SETTING_AGENT, "").strip()


def set_selection(session: Session, provider_id: str, agent_id: str) -> None:
    """Persist the 研究引擎 choice. Stores ids only - never a credential."""
    settings_service.set_many(
        session,
        {
            SETTING_PROVIDER: (provider_id or "").strip().lower(),
            SETTING_AGENT: (agent_id or "").strip(),
        },
    )


def _resolve_provider_row(session: Session):
    """The provider row backing the research engine.

    Falls back to the user's default provider so a研究引擎 stays usable after
    they reconfigure their models on the Professional settings page.
    """
    chosen = selected_provider_id(session)
    row = providers_repo.get_by_provider_id(session, chosen) if chosen else None
    if row is not None and row.enabled:
        return row
    return providers_repo.default_provider(session)


def _resolve_spec(session: Session, provider_id: str) -> Optional[ResearchAgentSpec]:
    """The agent spec to use, honouring the stored choice where it is valid.

    The local collection backend is returned only when the user stored it
    explicitly. There is no path from "no valid choice" to local collection:
    that is the silent-routing bug, and it is closed by
    :func:`default_agent_for_provider` returning None instead.
    """
    stored = get_agent(selected_agent_id(session))
    if stored is not None and stored.is_research_capable:
        # A vendor agent only makes sense with its own vendor. The local
        # backend and any provider-independent agent carry no provider_id.
        if not stored.provider_id or stored.provider_id == provider_id:
            return stored
    return default_agent_for_provider(provider_id)


def engine_status(session: Session) -> EngineStatus:
    """Everything the Simple home needs to render 研究引擎, and nothing more."""
    row = _resolve_provider_row(session)
    if row is None:
        return EngineStatus(configured=False, error=NO_PROVIDER_MESSAGE)

    name = row.display_name or row.provider_id

    spec = _resolve_spec(session, row.provider_id)

    # Provider problems are reported before agent problems: "set a model name"
    # is something the user can act on immediately, whereas "this service has
    # no remote research agent" is a decision about which service to use.
    if not row.is_configured:
        # Not yet usable: render the inline setup prompt, and say which of the
        # two things is missing so the user knows what to do next.
        from ..llm.presets import preset_or_custom

        preset = preset_or_custom(row.provider_id)
        if not (row.default_model or "").strip():
            reason = f"{name} 尚未设置模型名称。"
        elif preset.requires_api_key and not row.has_api_key:
            reason = f"{name} 尚未配置 API Key。"
        else:  # pragma: no cover - is_configured covers the remaining cases
            reason = f"{name} 尚未配置完成。"
        return EngineStatus(
            configured=False,
            provider_config_id=row.id,
            provider_id=row.provider_id,
            provider_name=name,
            model=row.default_model,
            agent_id=spec.agent_id if spec else "",
            agent_name=spec.display_name if spec else "",
            backend_kind=spec.backend_kind if spec else BACKEND_REMOTE,
            error=reason,
        )

    if spec is None:
        # A usable provider with no remote research agent and no explicit
        # local choice. Say so, rather than silently running local collection
        # and attributing the result to this provider.
        return EngineStatus(
            configured=False,
            provider_config_id=row.id,
            provider_id=row.provider_id,
            provider_name=name,
            model=row.default_model,
            error=f"{name}：{NO_REMOTE_AGENT_REASON}",
            masked_key=row.masked_key,
        )

    # Usable provider. The only remaining objection is capability: a chat
    # endpoint must never be presented as a research agent.
    error = (
        ""
        if spec.is_research_capable
        else (spec.unavailable_reason or "所选研究 Agent 不具备联网研究能力。")
    )

    return EngineStatus(
        configured=True,
        provider_config_id=row.id,
        provider_id=row.provider_id,
        provider_name=name,
        model=row.default_model,
        agent_id=spec.agent_id,
        agent_name=spec.display_name,
        backend_kind=spec.backend_kind,
        error=error,
        masked_key=row.masked_key,
    )


def available_agents(session: Session) -> list[ResearchAgentSpec]:
    """Remote research agents the user could pick right now.

    May be empty - that is the honest answer for a chat-only provider, and the
    UI renders it as "no remote research engine" rather than substituting the
    local backend.
    """
    row = _resolve_provider_row(session)
    if row is None:
        return []
    return remote_agents_for_provider(row.provider_id)


def agents_for_provider_choice(provider_id: str) -> list[ResearchAgentSpec]:
    """Remote agents offered while the user is choosing a provider in the modal."""
    return remote_agents_for_provider(provider_id)


def advanced_agents_for_provider_choice(provider_id: str) -> list[ResearchAgentSpec]:
    """Local/advanced backends, shown only behind the 高级 disclosure."""
    return local_research_agents()


# --- building the live agent -------------------------------------------------

#: agent_id -> the class that implements it. A vendor-specific chat endpoint
#: with a search switch shares one adapter, because the difference between
#: Qwen, GLM and Kimi is a request field, not an architecture.
_WEB_AGENT_CLASSES = {
    AGENT_OPENAI_WEB: OpenAIResponsesAgent,
    AGENT_ANTHROPIC_WEB: AnthropicWebSearchAgent,
    AGENT_GEMINI_GROUNDING: GeminiGroundingAgent,
    # Explicitly mapped, never left to the OpenAI-compatible default: that
    # adapter posts to DeepSeek's chat surface, which rejects built-in tools
    # outright (HTTP 422, "expected `function`") and cannot search at all.
    AGENT_DEEPSEEK_WEB: DeepSeekWebSearchAgent,
}


def build_agent(
    session: Session,
    progress: Optional[ProgressCallback] = None,
    usage_sink: Optional[Callable[[dict], None]] = None,
    http_session: Optional[requests.Session] = None,
    log: Optional[Callable[[str, str], None]] = None,
) -> ResearchService:
    """Instantiate the configured Research Agent.

    Raises :class:`ResearchAgentUnavailable` rather than returning something
    that would produce a confident, sourceless report.
    """
    status = engine_status(session)
    if not status.configured:
        raise ResearchAgentUnavailable(status.error or NO_ENGINE_MESSAGE)
    if status.error:
        raise ResearchAgentUnavailable(status.error)

    spec = get_agent(status.agent_id)
    if spec is None or not spec.is_research_capable:  # pragma: no cover - guarded above
        raise ResearchAgentUnavailable("所选研究 Agent 不可用。")

    # Dispatch on the backend kind, not on an id. The local collection engine
    # is reachable from exactly one branch, and only for a spec that declares
    # itself local - so no remote selection can ever reach a collector.
    if spec.is_local:
        return LocalCollectionAgent(
            spec,
            progress=progress,
            client=build_llm_service(usage_sink=usage_sink),
            usage_sink=usage_sink,
            http_session=http_session,
            log=log,
        )

    row = providers_repo.get_provider(session, status.provider_config_id or 0)
    if row is None:  # pragma: no cover - resolved moments ago
        raise ResearchAgentUnavailable(NO_PROVIDER_MESSAGE)

    config = runtime_config(
        provider_id=row.provider_id,
        base_url=row.base_url or "",
        model=row.default_model or "",
        api_key=get_provider_key(row.provider_id),
        temperature=row.temperature if row.temperature is not None else 0.2,
        max_tokens=row.max_tokens or 4000,
        timeout=row.timeout_seconds or 180,
        retries=row.retries or 3,
        extra_body=row.extra_config_json or {},
        display_name=row.display_name or "",
    )

    agent_cls = _WEB_AGENT_CLASSES.get(spec.agent_id, OpenAICompatibleSearchAgent)
    if agent_cls is LocalCollectionAgent:  # pragma: no cover - defensive
        raise ResearchAgentUnavailable(
            "远程研究 Agent 不能使用本地采集实现。"
        )
    return agent_cls(
        spec,
        config=config,
        progress=progress,
        usage_sink=usage_sink,
        session=http_session,
    )


def readiness_error(session: Session) -> str:
    """'' when 开始研究 can run, otherwise why it cannot."""
    status = engine_status(session)
    if not status.configured:
        return status.error or NO_ENGINE_MESSAGE
    return status.error


__all__ = [
    "EngineStatus",
    "NO_ENGINE_MESSAGE",
    "NO_PROVIDER_MESSAGE",
    "NO_REMOTE_AGENT_REASON",
    "SETTING_AGENT",
    "SETTING_PROVIDER",
    "advanced_agents_for_provider_choice",
    "agents_for_provider_choice",
    "available_agents",
    "build_agent",
    "engine_status",
    "readiness_error",
    "research_capable_agents",
    "selected_agent_id",
    "selected_provider_id",
    "set_selection",
]
