"""Which Research Agents exist, and which of them can really read the web.

Two backend kinds, and they are never interchangeable:

``BACKEND_REMOTE``
    The vendor's agent searches, reads, cross-checks and cites on its own
    servers and hands back one finished ``ResearchResult``. This is what
    简易版 means by research, and the only kind offered by default.
``BACKEND_LOCAL``
    AIOS's own collectors fetch documents from this machine. Preserved and
    fully supported, but labelled 本地检索研究（高级） and reachable only by
    explicitly asking for it - because it re-inherits the anti-scraping,
    rate-limit, VPN and routing problems 简易版 exists to eliminate.

There is deliberately **no fallback between them**. A user who selected a
remote agent and got an error is told what failed; they are never quietly
switched to local collection and handed a report that claims the remote agent
produced it.

This file is also the product's honesty layer. It is deliberately conservative:

* An agent appears as research-capable only when the vendor documents a
  **server-side web search / grounding** facility that AIOS actually drives.
* Every other provider gets an explicit ``unavailable_reason`` explaining that
  its chat endpoint cannot browse - shown to the user instead of being quietly
  offered as "Deep Research".

DeepSeek is the instructive case, and the reason capability is tracked per
*integration* rather than per vendor. Its OpenAI-compatible chat API has no
web search at all - a built-in tool posted there is rejected with HTTP 422,
``expected `function```. Its **Anthropic-compatible** endpoint
(``/anthropic/v1/messages``) does have it: it runs
``web_search_20250305`` server-side and returns ``server_tool_use`` /
``web_search_tool_result`` blocks with real URLs.

Verified live on 2026-09-20 against the user's own credential:

* no tools -> the model explicitly refuses, saying it has no search tool and
  its knowledge predates 2026. A clean negative control.
* ``web_search_20250305`` -> 6 server-side searches, 50 URLs, a schema-valid
  ResearchResult, and events dated within the last three days.

So "DeepSeek" is not one capability. ``deepseek_web_search`` is offered as a
remote Research Agent; the plain chat integration still is not, because it
genuinely cannot browse and would answer from training data with invented
dates and no URLs.
"""

from __future__ import annotations

from typing import Optional

from .base import (
    BACKEND_LOCAL,
    BACKEND_REMOTE,
    ResearchAgentSpec,
    ResearchCapabilities,
)

# --- capability presets -----------------------------------------------------

#: A vendor-hosted agent that searches and reads the live web itself.
NATIVE_WEB = ResearchCapabilities(
    supports_chat=True,
    supports_json=True,
    supports_web_research=True,
    supports_citations=True,
    supports_grounding=True,
    supports_multi_step=True,
    supports_structured_output=True,
)

#: A vendor search switch on an otherwise ordinary chat endpoint: it does
#: retrieve live results, but a single pass rather than iterative research.
VENDOR_SEARCH = ResearchCapabilities(
    supports_chat=True,
    supports_json=True,
    supports_web_research=True,
    supports_citations=True,
    supports_grounding=True,
    supports_multi_step=False,
    supports_structured_output=True,
)

#: AIOS's own collectors do the retrieval; the configured model analyses.
#: Genuinely web research - the sources are real and fetched this run - but the
#: reach is whatever RSS/GDELT/Google News can see from *this machine*, not an
#: open-ended crawl, and it depends on the user's own network reach.
LOCAL_COLLECTION = ResearchCapabilities(
    supports_chat=True,
    supports_json=True,
    supports_web_research=True,
    supports_citations=True,
    supports_grounding=True,
    supports_multi_step=False,
    supports_structured_output=True,
)

#: A plain chat endpoint. Cannot research, and must never be offered as if it
#: could: it would answer from training data with invented dates and no URLs.
CHAT_ONLY = ResearchCapabilities(
    supports_chat=True,
    supports_json=True,
    supports_web_research=False,
    supports_citations=False,
    supports_grounding=False,
    supports_multi_step=False,
    supports_structured_output=True,
)

#: Deliberately about the *integration*, not the vendor. DeepSeek proved why:
#: the same API key is chat-only through one endpoint and fully
#: research-capable through another. Saying "该服务不支持联网研究" would have
#: been factually wrong, and would have kept a capable provider hidden.
CHAT_ONLY_REASON = (
    "当前接入方式尚未启用远程联网研究：该接口只提供普通对话，"
    "不能自行检索网页，因此不能作为远程研究 Agent。"
    "它仍可用于主题优化、本地专业版分析与报告撰写。"
)

#: Shown when the user's provider has no remote research agent at all. Says
#: what to do next without claiming anything about the vendor's abilities in
#: general - only about the integration AIOS currently drives.
NO_REMOTE_AGENT_REASON = (
    "当前接入方式尚未启用远程联网研究。请改用已支持联网研究的接入方式"
    "（如 DeepSeek / OpenAI / Claude / Gemini / 智谱 / 通义千问 / Kimi），"
    "或在「高级」中显式选择「本地检索研究（高级）」——"
    "后者由本机采集来源，依赖你的网络环境。"
)

# --- agent ids --------------------------------------------------------------

AGENT_LOCAL = "local_collection"
AGENT_DEEPSEEK_WEB = "deepseek_web_search"
AGENT_OPENAI_WEB = "openai_web_search"
AGENT_ANTHROPIC_WEB = "anthropic_web_search"
AGENT_GEMINI_GROUNDING = "gemini_grounding"
AGENT_QWEN_SEARCH = "qwen_search"
AGENT_ZHIPU_SEARCH = "zhipu_web_search"
AGENT_MOONSHOT_SEARCH = "moonshot_web_search"


#: The local-collection agent. Provider-independent: it works with whichever
#: model the user configured.
#:
#: Sorted last and named for what it is. It is a genuinely useful backend - for
#: a user who has only a chat-only provider it is the only way to get a sourced
#: report at all - but it is *not* the Simple-mode architecture. It runs the
#: Classic collectors on this machine, so it is subject to the user's网络模式,
#: proxy and rate-limit reality, and that must be visible before they choose it
#: rather than discovered when a run fails.
LOCAL_AGENT = ResearchAgentSpec(
    agent_id=AGENT_LOCAL,
    display_name="本地检索研究（高级）",
    provider_id="",
    description=(
        "不使用远程研究 Agent：由本机的经典采集引擎抓取公开来源"
        "（RSS / GDELT / 新闻检索），再由你配置的 AI 阅读、交叉验证与撰写。"
        "适用于任何模型服务，但检索效果取决于本机网络与采集设置。"
    ),
    capabilities=LOCAL_COLLECTION,
    backend_kind=BACKEND_LOCAL,
    sort_order=800,
)


#: Vendor-native research agents, one per documented capability.
NATIVE_AGENTS: tuple[ResearchAgentSpec, ...] = (
    ResearchAgentSpec(
        agent_id=AGENT_DEEPSEEK_WEB,
        display_name="DeepSeek 联网研究",
        provider_id="deepseek",
        description=(
            "使用 DeepSeek 的 Anthropic 兼容接口（/anthropic）进行服务端联网检索，"
            "由 DeepSeek 自行搜索、阅读并给出来源链接。"
            "与普通 deepseek-chat 对话接口不同，后者不能联网。"
        ),
        capabilities=NATIVE_WEB,
        # Any name works: the Anthropic-compatible endpoint maps unknown names
        # onto its own models, and web search ran on every one tested.
        example_models=("deepseek-chat", "deepseek-flash", "deepseek-v4-pro"),
        # First in the list on purpose. This is the provider most of this
        # product's users already have configured, and it is now genuinely
        # research-capable - so the calm path should land on it.
        sort_order=15,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_ZHIPU_SEARCH,
        display_name="智谱 GLM 联网检索",
        provider_id="zhipu",
        description="使用智谱开放平台的 web_search 工具边检索边作答。",
        capabilities=VENDOR_SEARCH,
        example_models=("glm-4-plus", "glm-4-air"),
        sort_order=20,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_QWEN_SEARCH,
        display_name="通义千问联网检索",
        provider_id="qwen",
        description="使用阿里云百炼的联网搜索（enable_search）。",
        capabilities=VENDOR_SEARCH,
        example_models=("qwen-plus", "qwen-max"),
        sort_order=30,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_MOONSHOT_SEARCH,
        display_name="Kimi 联网检索",
        provider_id="moonshot",
        description="使用月之暗面内置的 $web_search 工具。",
        capabilities=VENDOR_SEARCH,
        example_models=("moonshot-v1-32k",),
        sort_order=40,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_OPENAI_WEB,
        display_name="OpenAI 联网研究",
        provider_id="openai",
        description="使用 OpenAI Responses API 的 web_search 工具做多轮检索。",
        capabilities=NATIVE_WEB,
        example_models=("gpt-4.1", "gpt-4o"),
        sort_order=200,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_ANTHROPIC_WEB,
        display_name="Claude 联网研究",
        provider_id="anthropic",
        description="使用 Anthropic Messages API 的 web_search 工具做多轮检索。",
        capabilities=NATIVE_WEB,
        example_models=("claude-sonnet-4-5",),
        sort_order=210,
    ),
    ResearchAgentSpec(
        agent_id=AGENT_GEMINI_GROUNDING,
        display_name="Gemini 联网研究",
        provider_id="gemini",
        description="使用 Google Search grounding，答案锚定到检索到的网页。",
        capabilities=NATIVE_WEB,
        example_models=("gemini-2.5-pro", "gemini-2.0-flash"),
        sort_order=220,
    ),
)


#: Providers whose chat endpoint genuinely cannot browse. Listed explicitly so
#: the UI can say *why* rather than silently omitting them.
#: DeepSeek is deliberately absent: its Anthropic-compatible endpoint really
#: does search the web, verified live. Its plain chat integration still
#: cannot, but that is a property of the endpoint, not of the provider, and
#: the provider now has a research-capable agent.
CHAT_ONLY_PROVIDERS: tuple[str, ...] = (
    "doubao",
    "minimax",
    "hunyuan",
    "qianfan",
    "siliconflow",
    "openrouter",
    "ollama",
    "local_openai",
    "custom",
)


def _chat_only_spec(provider_id: str, display_name: str) -> ResearchAgentSpec:
    return ResearchAgentSpec(
        agent_id=f"{provider_id}_chat",
        display_name=f"{display_name}（仅对话）",
        provider_id=provider_id,
        description=CHAT_ONLY_REASON,
        capabilities=CHAT_ONLY,
        unavailable_reason=CHAT_ONLY_REASON,
        sort_order=900,
    )


def all_agents() -> list[ResearchAgentSpec]:
    """Every agent AIOS knows about, research-capable or not, in display order."""
    from ..llm.presets import preset_or_custom

    agents: list[ResearchAgentSpec] = [LOCAL_AGENT, *NATIVE_AGENTS]
    for provider_id in CHAT_ONLY_PROVIDERS:
        preset = preset_or_custom(provider_id)
        agents.append(_chat_only_spec(provider_id, preset.display_name))
    return sorted(agents, key=lambda spec: (spec.sort_order, spec.agent_id))


def research_capable_agents() -> list[ResearchAgentSpec]:
    """Every agent that may be *offered* at all, remote or local."""
    return [spec for spec in all_agents() if spec.is_research_capable]


def remote_research_agents() -> list[ResearchAgentSpec]:
    """The agents that perform research on the vendor's side.

    This is what 简易版 means by a Research Agent, and the only list a user
    sees unless they open 高级.
    """
    return [spec for spec in research_capable_agents() if spec.is_remote]


def local_research_agents() -> list[ResearchAgentSpec]:
    """Backends that retrieve using AIOS's own collectors on this machine."""
    return [spec for spec in research_capable_agents() if spec.is_local]


def get_agent(agent_id: str) -> Optional[ResearchAgentSpec]:
    """Look up one agent spec by id, or None."""
    wanted = (agent_id or "").strip()
    if not wanted:
        return None
    for spec in all_agents():
        if spec.agent_id == wanted:
            return spec
    return None


def remote_agents_for_provider(provider_id: str) -> list[ResearchAgentSpec]:
    """Remote research agents usable with one configured provider.

    A vendor agent only works against its own vendor's API, so this is at most
    one entry - and for a chat-only provider it is correctly empty. An empty
    list is a real answer, not a gap to paper over with the local agent.
    """
    wanted = (provider_id or "").strip().lower()
    return [
        spec
        for spec in remote_research_agents()
        if not spec.provider_id or spec.provider_id == wanted
    ]


def agents_for_provider(
    provider_id: str, *, include_local: bool = False
) -> list[ResearchAgentSpec]:
    """Agents offerable for one provider.

    Defaults to remote only. ``include_local=True`` is for the 高级 disclosure,
    where the local collection backend is shown deliberately and described as
    what it is.
    """
    agents = remote_agents_for_provider(provider_id)
    if include_local:
        agents = [*agents, *local_research_agents()]
    return agents


def native_agent_for_provider(provider_id: str) -> Optional[ResearchAgentSpec]:
    """The vendor's own remote research agent, when it has one."""
    wanted = (provider_id or "").strip().lower()
    for spec in NATIVE_AGENTS:
        if spec.provider_id == wanted:
            return spec
    return None


def provider_supports_remote_research(provider_id: str) -> bool:
    """Whether this provider can be selected as a remote Research Agent."""
    native = native_agent_for_provider(provider_id)
    return native is not None and native.is_research_capable


def unavailable_reason_for_provider(provider_id: str) -> str:
    """Why this provider has no remote research agent, or '' when it does."""
    if provider_supports_remote_research(provider_id):
        return ""
    return NO_REMOTE_AGENT_REASON


def default_agent_for_provider(provider_id: str) -> Optional[ResearchAgentSpec]:
    """What to pre-select for a provider the user just configured.

    Returns the vendor's own remote research agent, or **None**.

    Returning None is the point of this function. The previous behaviour fell
    back to the local collection agent, which meant a DeepSeek user who never
    chose anything got local RSS/GDELT collection presented to them as
    "research" - the silent routing this architecture forbids. A user who wants
    local collection must now ask for it by name.
    """
    native = native_agent_for_provider(provider_id)
    if native is not None and native.is_research_capable:
        return native
    return None
