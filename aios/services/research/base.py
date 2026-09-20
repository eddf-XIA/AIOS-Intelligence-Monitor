"""The provider-agnostic Research Agent contract.

The rest of AIOS must not care whether research was performed by OpenAI's
web-search tool, Anthropic's, Gemini's grounding, AIOS's own collectors, or
something that does not exist yet. It calls :meth:`ResearchService.run` and
receives a :class:`~aios.schemas.research.ResearchResult`.

The single rule this module exists to enforce:

    **A chat-completion endpoint is not a research agent.**

Every LLM will happily answer "what happened this week in humanoid robotics?"
from training data, with fabricated dates and no sources. Presenting that as
research would be the most damaging thing this product could do, so capability
is declared per agent in :class:`ResearchCapabilities` and an agent that cannot
actually read the open web is never offered as one that can.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from ...schemas.research import ResearchResult

#: Progress stage keys reported back to the caller. Deliberately human terms -
#: 简易版 shows these directly, so "collector #3 / GDELT retry" has no place
#: here. Professional-mode diagnostics live in the run log as before.
STAGE_UNDERSTANDING = "understanding"
STAGE_SEARCHING = "searching"
STAGE_READING = "reading"
STAGE_ORGANIZING = "organizing"
STAGE_REPORTING = "reporting"

#: Ordered, with the Chinese label each one shows.
RESEARCH_STAGES: tuple[tuple[str, str], ...] = (
    (STAGE_UNDERSTANDING, "理解研究目标"),
    (STAGE_SEARCHING, "搜索公开信息"),
    (STAGE_READING, "阅读与交叉验证"),
    (STAGE_ORGANIZING, "整理关键事件"),
    (STAGE_REPORTING, "生成报告"),
)

STAGE_LABELS = dict(RESEARCH_STAGES)
STAGE_ORDER = [key for key, _ in RESEARCH_STAGES]

#: Research depth, in user terms rather than token budgets.
DEPTH_STANDARD = "standard"
DEPTH_DEEP = "deep"
DEPTH_LABELS = {DEPTH_STANDARD: "标准", DEPTH_DEEP: "深入"}

#: Where the reading actually happens. This is an architectural fact about a
#: backend, not a quality rating, and it is the distinction 简易版 is built on.
#:
#: ``BACKEND_REMOTE``
#:     The agent searches, browses, reads and cites entirely on the vendor's
#:     side. AIOS sends a brief and receives a finished ResearchResult. The
#:     user's own network reach, collection mode and proxy settings are
#:     irrelevant - only the agent's API endpoint has to be reachable.
#: ``BACKEND_LOCAL``
#:     AIOS's own collectors (RSS / GDELT / news search) fetch the documents on
#:     this machine and the configured model analyses them. Powerful, but it
#:     inherits every anti-scraping, rate-limit and network-routing problem
#:     that 简易版 exists to remove, so it is never the default.
BACKEND_REMOTE = "remote"
BACKEND_LOCAL = "local"

BACKEND_LABELS = {
    BACKEND_REMOTE: "远程研究 Agent",
    BACKEND_LOCAL: "本地检索研究（高级）",
}


class ResearchError(RuntimeError):
    """Research could not be completed. Carries a user-readable reason."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ResearchAgentUnavailable(ResearchError):
    """The selected agent cannot run: not configured, or not research-capable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True)
class ResearchCapabilities:
    """What one agent can actually do.

    ``supports_web_research`` is the gate. Everything else refines *how well*.
    An agent with ``supports_web_research=False`` is never listed as a research
    agent, no matter how capable its underlying model is at conversation.
    """

    #: Can hold a conversation. True of essentially everything; not sufficient.
    supports_chat: bool = True
    #: Can be relied on to return a parseable structured object.
    supports_json: bool = True
    #: **Actually reads the live open web.** The one capability that matters.
    supports_web_research: bool = False
    #: Returns resolvable URLs for its claims.
    supports_citations: bool = False
    #: Answers are anchored to retrieved documents, not to model memory.
    supports_grounding: bool = False
    #: Can iterate: search, read, search again.
    supports_multi_step: bool = False
    #: Can be constrained to an output schema.
    supports_structured_output: bool = True

    @property
    def is_research_capable(self) -> bool:
        """Whether this agent may be offered as a Research Agent at all."""
        return self.supports_web_research and self.supports_citations


@dataclass(frozen=True)
class ResearchAgentSpec:
    """A selectable Research Agent, as shown in the 研究引擎 block.

    ``provider_id`` is empty for an agent that is not tied to one vendor (the
    local-collection agent works with whichever provider the user configured).
    """

    agent_id: str
    display_name: str
    #: Empty means "works with any configured provider".
    provider_id: str = ""
    description: str = ""
    capabilities: ResearchCapabilities = field(default_factory=ResearchCapabilities)
    #: ``BACKEND_REMOTE`` or ``BACKEND_LOCAL``. Remote is the default because
    #: it is the only kind 简易版 offers without the user asking for it.
    backend_kind: str = BACKEND_REMOTE
    #: Models known to expose this agent's capability, for the model hint only.
    example_models: tuple[str, ...] = ()
    #: Why this agent is unavailable, when it is. Shown verbatim.
    unavailable_reason: str = ""
    sort_order: int = 100

    @property
    def is_research_capable(self) -> bool:
        return self.capabilities.is_research_capable

    @property
    def is_remote(self) -> bool:
        """True when the vendor does the reading, not this machine."""
        return self.backend_kind == BACKEND_REMOTE

    @property
    def is_local(self) -> bool:
        """True when AIOS's own collectors fetch the documents."""
        return self.backend_kind == BACKEND_LOCAL

    @property
    def backend_label(self) -> str:
        return BACKEND_LABELS.get(self.backend_kind, self.backend_kind)

    @property
    def requires_provider(self) -> bool:
        return bool(self.provider_id)

    @property
    def requires_local_network(self) -> bool:
        """Whether the user's collection/proxy settings affect this backend.

        Remote research must answer False: telling a user to fix their Google
        News reachability when the report is written on OpenAI's servers is
        exactly the confusion 简易版 was created to end.
        """
        return self.is_local

    def capability_labels(self) -> list[str]:
        """Chinese capability chips for 高级信息. Never shown on the calm path."""
        caps = self.capabilities
        labels: list[str] = []
        if caps.supports_web_research:
            labels.append("联网检索")
        if caps.supports_citations:
            labels.append("来源引用")
        if caps.supports_grounding:
            labels.append("证据锚定")
        if caps.supports_multi_step:
            labels.append("多轮研究")
        if caps.supports_structured_output:
            labels.append("结构化输出")
        return labels


@dataclass
class ResearchRequest:
    """Everything an agent needs to perform one research pass.

    A *brief*, not a query. The agent is responsible for deciding how to search;
    AIOS is responsible for remembering what came back.
    """

    #: The research goal in the user's own words.
    topic_name: str
    brief: str
    scope: str = ""
    focus_areas: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    regions: str = ""
    #: Inclusive window the research should cover.
    window_from: Optional[dt.datetime] = None
    window_to: Optional[dt.datetime] = None
    window_hours: int = 72
    depth: str = DEPTH_STANDARD
    #: Local calendar date the resulting report is filed under.
    report_date: Optional[dt.date] = None
    #: Section names the report should try to use, when the topic has a shape
    #: worth preserving across days. Advisory, never enforced.
    preferred_sections: list[str] = field(default_factory=list)

    @property
    def window_text(self) -> str:
        hours = max(1, self.window_hours or 72)
        # Hours up to a week, days beyond it. 72 reads as 最近72小时, which is
        # how a daily-brief user thinks about it; 最近3天 is the same duration
        # and the wrong unit. A week or more is genuinely easier to read in
        # days, so 168 becomes 最近7天.
        if hours >= 168 and hours % 24 == 0:
            return f"最近{hours // 24}天"
        return f"最近{hours}小时"


#: Called by an agent to report progress. ``(stage_key, note)``.
ProgressCallback = Callable[[str, str], None]


class ResearchService(ABC):
    """One way of performing research.

    Implementations are constructed by :mod:`aios.services.research.registry`
    and are never referenced by name from a router or a template - adding a new
    backend must not require touching the UI.
    """

    #: Matches the :class:`ResearchAgentSpec` this implementation serves.
    agent_id: str = "generic"

    def __init__(
        self,
        spec: ResearchAgentSpec,
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.spec = spec
        self._progress = progress

    # -- helpers ---------------------------------------------------------

    @property
    def capabilities(self) -> ResearchCapabilities:
        return self.spec.capabilities

    def report_progress(self, stage: str, note: str = "") -> None:
        """Tell the caller which human-readable stage is running."""
        if self._progress is None:
            return
        try:
            self._progress(stage, note)
        except Exception:  # pragma: no cover - progress must never break a run
            pass

    def readiness_error(self) -> str:
        """'' when this agent can run right now, else the reason it cannot."""
        if not self.capabilities.is_research_capable:
            return (
                f"{self.spec.display_name} 不具备联网研究能力，"
                "无法作为研究 Agent 使用。"
            )
        return ""

    # -- the single operation business code uses --------------------------

    @abstractmethod
    def run(self, request: ResearchRequest) -> ResearchResult:
        """Perform one research pass.

        Must return a validated :class:`ResearchResult`. A failure that leaves
        the caller unable to distinguish "nothing happened" from "we could not
        look" is a bug: raise :class:`ResearchError` or return a result whose
        coverage is ``failed``.
        """
