"""Research agents that drive a vendor's own web-search facility.

Each adapter differs only in how it asks the vendor to search; everything after
that - JSON extraction, repair, validation, evidence pruning, usage accounting -
is shared in :class:`WebResearchAgent`.

The credential never appears in a log line or an error message: every string on
its way out goes through :func:`aios.services.keyring_service.redact`, the same
rule the LLM provider layer follows.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

import requests

from ...schemas.research import (
    ResearchResult,
    ResearchResultError,
    validate_research_payload,
)
from ..keyring_service import redact
from ..llm.base import (
    LLMResponse,
    ProviderRuntimeConfig,
    extract_json,
    InvalidJSONResponse,
)
from .base import (
    ProgressCallback,
    ResearchAgentSpec,
    ResearchError,
    ResearchRequest,
    ResearchService,
    STAGE_ORGANIZING,
    STAGE_READING,
    STAGE_REPORTING,
    STAGE_SEARCHING,
    STAGE_UNDERSTANDING,
)
from .prompts import REPAIR_INSTRUCTION, SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

#: Research is slow by nature - a multi-step web search legitimately takes
#: minutes. The provider's configured timeout is a chat timeout, so it is
#: scaled up here rather than having a real research call killed at 180s.
TIMEOUT_MULTIPLIER = 3
MIN_TIMEOUT = 180
MAX_TIMEOUT = 1800

#: A research answer carries a whole report; the chat default is far too small.
DEFAULT_MAX_TOKENS = 16000


class WebResearchAgent(ResearchService):
    """Shared behaviour for every vendor-native research backend."""

    def __init__(
        self,
        spec: ResearchAgentSpec,
        config: ProviderRuntimeConfig,
        progress: Optional[ProgressCallback] = None,
        usage_sink: Optional[Callable[[dict], None]] = None,
        session: Optional[requests.Session] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        super().__init__(spec, progress=progress)
        self.agent_id = spec.agent_id
        self.config = config
        self._usage_sink = usage_sink
        self._http = session or requests.Session()
        self._max_tokens = max_tokens

    # -- helpers ---------------------------------------------------------

    @property
    def timeout(self) -> int:
        base = (self.config.timeout or MIN_TIMEOUT) * TIMEOUT_MULTIPLIER
        return max(MIN_TIMEOUT, min(int(base), MAX_TIMEOUT))

    def _safe(self, text: str) -> str:
        return redact(text or "", self.config.api_key)

    def readiness_error(self) -> str:
        base = super().readiness_error()
        if base:
            return base
        if not self.config.api_key:
            return (
                f"{self.config.display_name or self.config.provider_id} "
                "尚未配置 API Key。"
            )
        if not self.config.model:
            return (
                f"{self.config.display_name or self.config.provider_id} "
                "尚未设置模型名称。"
            )
        return ""

    def _record(self, **fields) -> None:
        if self._usage_sink is None:
            return
        fields.setdefault("provider_id", self.config.provider_id)
        fields.setdefault("purpose", "research")
        fields.setdefault("model", self.config.model)
        try:
            self._usage_sink(fields)
        except Exception:  # pragma: no cover - accounting never breaks a run
            logger.debug("research usage sink failed", exc_info=True)

    def _raise_for_status(self, response: requests.Response) -> None:
        """Translate an HTTP failure into a message a normal user can act on."""
        if response.status_code < 400:
            return
        body = self._safe((response.text or "")[:400])
        status = response.status_code
        if status == 429:
            raise ResearchError("研究服务触发限流（HTTP 429），请稍后重试。")
        if status == 402:
            raise ResearchError("研究服务余额不足（HTTP 402），请充值后重试。", retryable=False)
        if status in (401, 403):
            raise ResearchError(
                f"研究服务拒绝了该 API Key（HTTP {status}），请重新配置。", retryable=False
            )
        if status == 404:
            raise ResearchError(
                f"研究服务地址或模型不存在（HTTP 404）。请检查模型名称与接入地址。"
                f" {body}".rstrip(),
                retryable=False,
            )
        raise ResearchError(f"研究服务返回 HTTP {status}。{body}".rstrip())

    # -- the research pass -----------------------------------------------

    def run(self, request: ResearchRequest) -> ResearchResult:
        error = self.readiness_error()
        if error:
            raise ResearchError(error, retryable=False)

        self.report_progress(STAGE_UNDERSTANDING, "整理研究目标与检索方向")
        prompt = build_user_prompt(request)

        self.report_progress(STAGE_SEARCHING, f"{self.spec.display_name} 正在检索公开信息")
        started = time.monotonic()
        content = ""
        meta: dict[str, Any] = {}
        last_error: Optional[Exception] = None

        # Two attempts only, and the second is a *repair* attempt rather than a
        # blind retry: a research call is expensive and slow, so spending three
        # full passes on a provider that is answering badly is the wrong trade.
        for attempt in (1, 2):
            try:
                content, meta = self._research(prompt, repair=attempt > 1)
                self.report_progress(STAGE_ORGANIZING, "整理事件与证据")
                payload = extract_json(content)
                result = validate_research_payload(payload)
                latency_ms = int((time.monotonic() - started) * 1000)
                self._record(
                    latency_ms=latency_ms,
                    attempts=attempt,
                    success=True,
                    error_message="",
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens"),
                    total_tokens=meta.get("total_tokens"),
                    extra_json={
                        "agent_id": self.spec.agent_id,
                        "search_calls": meta.get("search_calls"),
                        # How many URLs the vendor's search actually returned,
                        # against how many the model went on to cite. A large
                        # gap is the signal that citations are drifting away
                        # from retrieved evidence.
                        "searched_urls": meta.get("searched_urls"),
                        "sources": len(result.sources),
                        "events": len(result.events),
                        "coverage": result.coverage.status,
                    },
                )
                self.report_progress(STAGE_REPORTING, "撰写报告")
                return result
            except ResearchError:
                raise
            except (InvalidJSONResponse, ResearchResultError) as exc:
                last_error = exc
                logger.info(
                    "%s returned an unusable research payload (attempt %s): %s",
                    self.spec.agent_id, attempt, exc,
                )
                continue
            except requests.RequestException as exc:
                last_error = exc
                break

        latency_ms = int((time.monotonic() - started) * 1000)
        message = self._safe(str(last_error) if last_error else "unknown error")
        self._record(
            latency_ms=latency_ms,
            attempts=2,
            success=False,
            error_message=message[:500],
            extra_json={"agent_id": self.spec.agent_id},
        )
        if isinstance(last_error, requests.RequestException):
            raise ResearchError(f"无法连接研究服务：{message}") from last_error
        raise ResearchError(
            f"研究服务返回的结果无法解析：{message}"
        ) from last_error

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        """Issue one research request. Returns ``(text, meta)``."""
        raise NotImplementedError

    # -- convenience for subclasses --------------------------------------

    @staticmethod
    def _with_repair(prompt: str, repair: bool) -> str:
        return f"{prompt}\n\n{REPAIR_INSTRUCTION}" if repair else prompt

    @staticmethod
    def _usage_from_openai(payload: dict) -> dict[str, Any]:
        usage = payload.get("usage") or {}
        return {
            "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
            "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }


class OpenAIResponsesAgent(WebResearchAgent):
    """OpenAI Responses API with the hosted ``web_search`` tool."""

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        url = self.config.endpoint("/responses")
        body = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._with_repair(prompt, repair)},
            ],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": self._max_tokens,
        }
        response = self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        payload = response.json()
        return _openai_responses_text(payload), {
            **self._usage_from_openai(payload),
            "search_calls": _openai_search_calls(payload),
        }


class AnthropicWebSearchAgent(WebResearchAgent):
    """Anthropic Messages API with the server-side ``web_search`` tool."""

    #: Anthropic versions its server tools explicitly.
    TOOL_TYPE = "web_search_20250305"

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        url = self.config.endpoint("/messages")
        body = {
            "model": self.config.model,
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": self._with_repair(prompt, repair)}
            ],
            "tools": [
                {"type": self.TOOL_TYPE, "name": "web_search", "max_uses": 12}
            ],
        }
        response = self._http.post(
            url,
            headers={
                "x-api-key": self.config.api_key or "",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        payload = response.json()
        usage = payload.get("usage") or {}
        blocks = payload.get("content") or []
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        searches = sum(
            1
            for block in blocks
            if isinstance(block, dict)
            and block.get("type") in {"server_tool_use", "web_search_tool_result"}
        )
        return text, {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            or None,
            "search_calls": searches,
        }


class DeepSeekWebSearchAgent(WebResearchAgent):
    """DeepSeek's Anthropic-compatible endpoint, with server-side web search.

    A dedicated adapter rather than a reuse of :class:`OpenAIResponsesAgent`,
    because DeepSeek's OpenAI-compatible surface cannot express this request
    at all. Posting a built-in tool to ``/chat/completions`` returns HTTP 422
    - ``unknown variant `web_search`, expected `function``` - for both
    ``web_search`` and ``web_search_20250305`` (verified 2026-09-20). That
    surface only accepts client-side ``function`` tools, so server-side search
    is reachable only through ``/anthropic``. The two paths are kept
    physically separate so no configuration change can route a research run
    onto an endpoint that cannot perform one.

    Verified live against ``/anthropic/v1/messages`` on 2026-09-20:

    * without tools, the model refuses and says it has no search facility -
      the negative control that proves the search below is real;
    * with ``web_search_20250305``, DeepSeek issues several server-side
      searches and returns ``server_tool_use`` / ``web_search_tool_result``
      blocks carrying live URLs;
    * the production research prompt still yields a schema-valid
      ``ResearchResult``, with coverage honestly downgraded to ``partial``
      when the model hit its own search-call ceiling.

    Nothing here touches AIOS's collectors. Every URL originates in DeepSeek's
    own search results.
    """

    #: Anthropic versions its server tools; DeepSeek implements this version.
    TOOL_TYPE = "web_search_20250305"

    #: Thinking blocks, six searches' worth of context and a full report share
    #: one budget. The verified run finished at ~16k output tokens, so the
    #: ceiling is set well above it: a truncated response is not a cheap
    #: failure here, it costs a whole slow research pass.
    MAX_TOKENS = 32000

    #: How many server-side searches the agent may run. The live run used six
    #: and reported hitting the limit as a coverage limitation, which is the
    #: behaviour we want - but a daily brief deserves a little more room.
    MAX_SEARCH_USES = 16

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("max_tokens", self.MAX_TOKENS)
        super().__init__(*args, **kwargs)
        #: URLs the search engine actually returned this run, for grounding
        #: checks. Populated by :meth:`_research`.
        self.searched_urls: list[str] = []

    # -- endpoint --------------------------------------------------------

    @property
    def messages_url(self) -> str:
        """The Anthropic-compatible messages endpoint for this provider row.

        The stored ``base_url`` is the *chat* base (``https://api.deepseek.com``
        by default, sometimes with ``/v1`` appended by hand). The research
        endpoint lives under ``/anthropic``, so it is derived here rather than
        asking the user to configure a second URL they should never have to
        know about.
        """
        base = (self.config.base_url or "https://api.deepseek.com").rstrip("/")
        # A hand-written chat base may already carry /v1; that is the OpenAI
        # surface and must not end up inside the Anthropic path.
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        if not base.endswith("/anthropic"):
            base = f"{base}/anthropic"
        return f"{base}/v1/messages"

    # -- the request -----------------------------------------------------

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        body = {
            "model": self.config.model or "deepseek-chat",
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": self._with_repair(prompt, repair)}
            ],
            "tools": [
                {
                    "type": self.TOOL_TYPE,
                    "name": "web_search",
                    "max_uses": self.MAX_SEARCH_USES,
                }
            ],
        }
        # The provider's ``extra_body`` is deliberately not forwarded: it holds
        # OpenAI-shaped options (the DeepSeek preset ships
        # ``{"thinking": {"type": "disabled"}}``) which this endpoint does not
        # share, and a rejected request here costs a full research pass.
        response = self._http.post(
            self.messages_url,
            headers={
                "x-api-key": self.config.api_key or "",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        payload = response.json()
        blocks = [b for b in (payload.get("content") or []) if isinstance(b, dict)]

        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if block.get("type") == "text"
        )

        self.searched_urls = _deepseek_result_urls(blocks)
        search_calls = sum(1 for b in blocks if b.get("type") == "server_tool_use")

        if not search_calls and not self.searched_urls:
            # The endpoint answered, but nothing was searched - the model
            # declined to use the tool, or a future endpoint change stopped
            # honouring it. Either way the answer came from model memory, so
            # it must fail loudly rather than be dressed up as research.
            raise ResearchError(
                "DeepSeek 未执行联网检索（响应中没有 web_search 结果），"
                "本次研究未完成。请确认所用接入地址支持 /anthropic 联网检索。",
                retryable=False,
            )

        usage = payload.get("usage") or {}
        tool_usage = usage.get("server_tool_use") or {}
        return text, {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": (usage.get("input_tokens") or 0)
            + (usage.get("output_tokens") or 0)
            or None,
            "search_calls": tool_usage.get("web_search_requests") or search_calls,
            "searched_urls": len(self.searched_urls),
        }


def _deepseek_result_urls(blocks: list[dict]) -> list[str]:
    """Every URL the server-side search actually returned.

    Citations do **not** arrive on the text blocks the way Anthropic's own API
    delivers them - in the verified run, text-block ``citations`` was empty and
    all 50 URLs lived inside ``web_search_tool_result`` content. Harvesting
    from the wrong place would silently yield zero sources, so this reads the
    place DeepSeek actually uses.
    """
    urls: list[str] = []
    for block in blocks:
        if block.get("type") != "web_search_tool_result":
            continue
        content = block.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("url"):
                    urls.append(str(item["url"]))
    return urls


class GeminiGroundingAgent(WebResearchAgent):
    """Gemini ``generateContent`` grounded with Google Search."""

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        url = self.config.endpoint(f"/models/{self.config.model}:generateContent")
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": self._with_repair(prompt, repair)}],
                }
            ],
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "maxOutputTokens": self._max_tokens,
                "temperature": self.config.temperature,
            },
        }
        response = self._http.post(
            url,
            headers={
                "x-goog-api-key": self.config.api_key or "",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        payload = response.json()
        candidates = payload.get("candidates") or []
        text = ""
        searches = 0
        if candidates:
            first = candidates[0] or {}
            parts = (first.get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
            grounding = first.get("groundingMetadata") or {}
            searches = len(grounding.get("groundingChunks") or [])
        usage = payload.get("usageMetadata") or {}
        return text, {
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
            "search_calls": searches,
        }


class OpenAICompatibleSearchAgent(WebResearchAgent):
    """An OpenAI-compatible ``/chat/completions`` with a vendor search switch.

    Chinese vendors expose live search on their otherwise standard chat
    endpoint, but each in its own way. Rather than pretending one flag works
    everywhere, the vendor-specific shaping is explicit here - and a provider
    with no known switch never reaches this class, because
    :mod:`aios.services.research.catalog` does not give it a research agent.
    """

    #: provider_id -> how to turn search on.
    SEARCH_STYLE = {
        # Alibaba Bailian: a top-level request flag.
        "qwen": "enable_search",
        # Zhipu: a declared tool the platform executes itself.
        "zhipu": "web_search_tool",
        # Moonshot: a builtin function the platform fulfils.
        "moonshot": "builtin_function",
    }

    def _search_body(self) -> dict[str, Any]:
        style = self.SEARCH_STYLE.get(self.config.provider_id, "")
        if style == "enable_search":
            return {"enable_search": True}
        if style == "web_search_tool":
            return {
                "tools": [
                    {"type": "web_search", "web_search": {"enable": True, "search_result": True}}
                ]
            }
        if style == "builtin_function":
            return {
                "tools": [
                    {"type": "builtin_function", "function": {"name": "$web_search"}}
                ]
            }
        return {}

    def _research(self, prompt: str, repair: bool) -> tuple[str, dict[str, Any]]:
        url = self.config.endpoint("/chat/completions")
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._with_repair(prompt, repair)},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self._max_tokens,
        }
        body.update(self._search_body())
        # Non-secret vendor extras from the provider config (never credentials).
        for key, value in (self.config.extra_body or {}).items():
            body.setdefault(key, value)

        response = self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        payload = response.json()
        choices = payload.get("choices") or []
        text = ""
        if choices:
            message = (choices[0] or {}).get("message") or {}
            text = str(message.get("content") or "")
        return text, self._usage_from_openai(payload)


# --- response readers -------------------------------------------------------

def _openai_responses_text(payload: dict) -> str:
    """Pull the assistant text out of a Responses API reply.

    ``output_text`` is the convenience field; when it is absent (or the SDK
    shape changes) the output blocks are walked instead, so a working research
    call is not lost to a field rename.
    """
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    if isinstance(direct, list):
        joined = "".join(str(part) for part in direct)
        if joined.strip():
            return joined

    chunks: list[str] = []
    for block in payload.get("output") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "message":
            continue
        for part in block.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                chunks.append(str(part.get("text", "")))
    return "".join(chunks)


def _openai_search_calls(payload: dict) -> int:
    return sum(
        1
        for block in payload.get("output") or []
        if isinstance(block, dict) and block.get("type") == "web_search_call"
    )


#: Recorded only so the shape of an :class:`LLMResponse` stays importable here
#: for type-checkers; research results are not LLMResponses.
__all__ = [
    "WebResearchAgent",
    "OpenAIResponsesAgent",
    "AnthropicWebSearchAgent",
    "GeminiGroundingAgent",
    "OpenAICompatibleSearchAgent",
    "LLMResponse",
]
