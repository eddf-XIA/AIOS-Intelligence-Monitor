"""The Research Agent layer.

    Research Agent is the eyes. AIOS remains the memory.

An agent discovers information; it does not own it. Whatever a backend returns
is validated into a :class:`~aios.schemas.research.ResearchResult` and then
folded into the same ``IntelligenceEvent`` / ``EventObservation`` /
``ObservationSource`` core the Classic engine writes to, so NEW/UPDATED,
timelines, compare and history behave identically whichever way the information
arrived.

Nothing outside this package names a vendor:
:func:`aios.services.research.registry.build_agent` resolves the configured
choice, and :class:`aios.services.research.base.ResearchService` is the only
interface the pipeline knows.
"""

from __future__ import annotations

from .base import (
    BACKEND_LABELS,
    BACKEND_LOCAL,
    BACKEND_REMOTE,
    DEPTH_DEEP,
    DEPTH_LABELS,
    DEPTH_STANDARD,
    RESEARCH_STAGES,
    STAGE_LABELS,
    STAGE_ORDER,
    STAGE_ORGANIZING,
    STAGE_READING,
    STAGE_REPORTING,
    STAGE_SEARCHING,
    STAGE_UNDERSTANDING,
    ResearchAgentSpec,
    ResearchAgentUnavailable,
    ResearchCapabilities,
    ResearchError,
    ResearchRequest,
    ResearchService,
)
from .catalog import (
    AGENT_DEEPSEEK_WEB,
    AGENT_LOCAL,
    LOCAL_AGENT,
    NO_REMOTE_AGENT_REASON,
    agents_for_provider,
    all_agents,
    default_agent_for_provider,
    get_agent,
    local_research_agents,
    native_agent_for_provider,
    provider_supports_remote_research,
    remote_agents_for_provider,
    remote_research_agents,
    research_capable_agents,
    unavailable_reason_for_provider,
)
from .registry import (
    EngineStatus,
    NO_ENGINE_MESSAGE,
    advanced_agents_for_provider_choice,
    agents_for_provider_choice,
    available_agents,
    build_agent,
    engine_status,
    readiness_error,
    selected_agent_id,
    selected_provider_id,
    set_selection,
)

__all__ = [
    "AGENT_DEEPSEEK_WEB",
    "AGENT_LOCAL",
    "BACKEND_LABELS",
    "BACKEND_LOCAL",
    "BACKEND_REMOTE",
    "DEPTH_DEEP",
    "DEPTH_LABELS",
    "DEPTH_STANDARD",
    "EngineStatus",
    "LOCAL_AGENT",
    "NO_ENGINE_MESSAGE",
    "NO_REMOTE_AGENT_REASON",
    "RESEARCH_STAGES",
    "STAGE_LABELS",
    "STAGE_ORDER",
    "STAGE_ORGANIZING",
    "STAGE_READING",
    "STAGE_REPORTING",
    "STAGE_SEARCHING",
    "STAGE_UNDERSTANDING",
    "ResearchAgentSpec",
    "ResearchAgentUnavailable",
    "ResearchCapabilities",
    "ResearchError",
    "ResearchRequest",
    "ResearchService",
    "advanced_agents_for_provider_choice",
    "agents_for_provider",
    "agents_for_provider_choice",
    "all_agents",
    "available_agents",
    "build_agent",
    "default_agent_for_provider",
    "engine_status",
    "get_agent",
    "local_research_agents",
    "native_agent_for_provider",
    "provider_supports_remote_research",
    "readiness_error",
    "remote_agents_for_provider",
    "remote_research_agents",
    "research_capable_agents",
    "selected_agent_id",
    "selected_provider_id",
    "set_selection",
    "unavailable_reason_for_provider",
]
