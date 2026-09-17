"""Provider-neutral contract for every LLM backend.

Two rules keep vendor differences contained:

* nothing outside this package sees a vendor response shape - providers return
  :class:`LLMResponse`;
* nothing outside this package raises a vendor error - providers raise the
  :class:`LLMError` hierarchy.

The retry/backoff and JSON-extraction behaviour here is carried over verbatim
from the original ``DeepSeekClient``, which was already proven in production:
three attempts, 2s/4s/8s backoff, tolerant JSON parsing, and immediate failure
on errors that retrying cannot fix.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from ..keyring_service import redact

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

#: Appended to a retry when the model returned something unparseable.
JSON_REPAIR_INSTRUCTION = (
    "你上一次的回复不是合法 JSON。请只输出一个合法的 JSON 对象，"
    "不要输出 Markdown 代码围栏、解释或任何额外文字。"
)


# --- errors -----------------------------------------------------------------

class LLMError(RuntimeError):
    """Base class for every LLM failure surfaced to the pipeline."""


class MissingAPIKey(LLMError):
    """No credential configured for the selected provider."""


class ProviderNotConfigured(LLMError):
    """No usable provider/model could be resolved for the requested task."""


class RateLimited(LLMError):
    """HTTP 429 - tell the user to slow down or check their quota."""


class InsufficientBalance(LLMError):
    """HTTP 402 - the account has no credit left."""


class AuthenticationFailed(LLMError):
    """HTTP 401/403 - the stored key is wrong, expired or revoked."""


class InvalidJSONResponse(LLMError):
    """The model returned something unusable as JSON after repair attempts."""


#: Failures retrying cannot fix. Surfaced immediately so a bad key or an empty
#: balance does not burn three timeouts per topic.
NON_RETRYABLE = (MissingAPIKey, RateLimited, InsufficientBalance, AuthenticationFailed)


# --- normalized response ----------------------------------------------------

@dataclass
class LLMResponse:
    """One successful call, normalised across every provider.

    ``data`` is the parsed JSON object for :meth:`LLMProvider.generate_json`.
    The name is kept from the original ``LLMResult`` so the intelligence
    services did not need rewriting when the provider layer was introduced.
    """

    data: dict[str, Any]
    content: str
    model: str
    provider_id: str = ""
    latency_ms: int = 0
    attempts: int = 1
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: str = ""
    request_id: str = ""
    raw_usage: Optional[dict[str, Any]] = field(default=None, repr=False)

    @property
    def parsed_json(self) -> dict[str, Any]:
        """Spec-facing alias for :attr:`data`."""
        return self.data

    @property
    def raw_content(self) -> str:
        """Backwards-compatible alias used by older call sites."""
        return self.content


# --- capabilities -----------------------------------------------------------

@dataclass(frozen=True)
class ProviderCapabilities:
    """What a backend can actually do.

    Callers must consult this instead of assuming every model behaves alike;
    unsupported options are dropped rather than sent as invalid parameters.
    """

    supports_json_mode: bool = True
    supports_streaming: bool = True
    supports_system_prompt: bool = True
    supports_usage: bool = True
    supports_temperature: bool = True
    supports_custom_base_url: bool = True
    supports_reasoning: bool = False
    requires_api_key: bool = True


@dataclass
class ProviderRuntimeConfig:
    """Everything needed for a call except the credential itself."""

    provider_id: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout: int = 180
    retries: int = 3
    #: Non-secret vendor extras merged into the request body (e.g. DeepSeek's
    #: ``thinking`` switch). Never put credentials here.
    extra_body: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""

    def endpoint(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


# --- JSON handling ----------------------------------------------------------

def extract_json(content: str) -> dict[str, Any]:
    """Parse model output, tolerating fenced or prose-wrapped JSON.

    Raises :class:`InvalidJSONResponse` when no JSON object can be recovered.
    """
    content = (content or "").strip()
    if not content:
        raise InvalidJSONResponse("empty response body")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None

    if parsed is None and content.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        stripped = re.sub(r"```\s*$", "", stripped).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    if parsed is None:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise InvalidJSONResponse(f"no JSON object found in: {content[:200]}")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise InvalidJSONResponse(f"invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise InvalidJSONResponse("model returned a non-object JSON value")
    return parsed


# --- provider base ----------------------------------------------------------

class LLMProvider(ABC):
    """Synchronous LLM backend.

    Synchronous on purpose: the monitoring pipeline runs in worker threads with
    SQLAlchemy sessions, and threads give the same I/O parallelism without the
    asyncio/session lifecycle hazards. See the architecture notes in the README.
    """

    #: Stable English identifier, e.g. ``"deepseek"``.
    provider_id: str = "generic"
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(
        self,
        config: ProviderRuntimeConfig,
        usage_sink: Optional[Callable[[dict], None]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config
        self._usage_sink = usage_sink
        self._http = session or requests.Session()

    # -- helpers -------------------------------------------------------------

    @property
    def has_key(self) -> bool:
        if not self.capabilities.requires_api_key:
            return True
        return bool(self.config.api_key)

    def _safe(self, text: str) -> str:
        """Scrub credentials before an error string leaves this class."""
        return redact(text or "", self.config.api_key)

    def _require_key(self) -> None:
        if self.capabilities.requires_api_key and not self.config.api_key:
            raise MissingAPIKey(
                f"{self.config.display_name or self.provider_id} 尚未配置 API Key，"
                "请在「设置 → AI 模型」中填写。"
            )

    def _record(self, **fields) -> None:
        if not self._usage_sink:
            return
        fields.setdefault("provider_id", self.provider_id)
        try:
            self._usage_sink(fields)
        except Exception:  # pragma: no cover - accounting never breaks a run
            logger.debug("usage sink failed", exc_info=True)

    @staticmethod
    def _raise_for_status(status_code: int, body_text: str = "") -> None:
        """Translate transport status codes into the shared error hierarchy."""
        if status_code == 429:
            raise RateLimited(
                "触发模型服务限流 (HTTP 429)。请稍后重试，或减少启用的主题数量。"
            )
        if status_code == 402:
            raise InsufficientBalance(
                "模型服务余额不足 (HTTP 402)。请充值后重试。"
            )
        if status_code in (401, 403):
            raise AuthenticationFailed(
                f"模型服务拒绝了该 API Key (HTTP {status_code})。请在设置中重新填写。"
            )

    # -- the two operations business code uses -------------------------------

    def generate_json(
        self,
        messages: list[dict],
        purpose: str = "generic",
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
        **context,
    ) -> LLMResponse:
        """Call the model and return parsed JSON.

        Prefers the provider's native JSON mode; otherwise instructs the model
        in the prompt. A malformed reply is retried once with an explicit repair
        instruction before the attempt budget is spent.
        """
        attempts = max(1, retries if retries is not None else self.config.retries)
        self._require_key()

        working = list(messages)
        last_error: Optional[Exception] = None
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            try:
                content, meta = self._chat(working, max_tokens, json_mode=True)
                data = extract_json(content)
                latency_ms = int((time.monotonic() - started) * 1000)
                response = LLMResponse(
                    data=data,
                    content=content,
                    model=meta.get("model") or self.config.model,
                    provider_id=self.provider_id,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens"),
                    total_tokens=meta.get("total_tokens"),
                    finish_reason=meta.get("finish_reason", ""),
                    request_id=meta.get("request_id", ""),
                    raw_usage=meta.get("raw_usage"),
                )
                self._record(
                    purpose=purpose,
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    total_tokens=response.total_tokens,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    success=True,
                    error_message="",
                    **context,
                )
                return response

            except NON_RETRYABLE as exc:
                last_error = exc
                break
            except InvalidJSONResponse as exc:
                last_error = exc
                if attempt < attempts:
                    logger.warning(
                        "%s returned malformed JSON for %s (attempt %s/%s); asking for a repair",
                        self.provider_id, purpose, attempt, attempts,
                    )
                    working = list(messages) + [
                        {"role": "assistant", "content": (exc.args[0] if exc.args else "")[:400]},
                        {"role": "user", "content": JSON_REPAIR_INSTRUCTION},
                    ]
                    time.sleep(min(2 ** attempt, 8))
            except Exception as exc:  # network / HTTP / decoding problems
                last_error = exc
                if attempt < attempts:
                    delay = 2 ** attempt
                    logger.warning(
                        "%s %s attempt %s/%s failed (%s); retrying in %ss",
                        self.provider_id, purpose, attempt, attempts,
                        self._safe(str(exc)), delay,
                    )
                    time.sleep(delay)

        latency_ms = int((time.monotonic() - started) * 1000)
        message = self._safe(str(last_error) if last_error else "unknown error")
        self._record(
            purpose=purpose,
            model=self.config.model,
            latency_ms=latency_ms,
            attempts=attempts,
            success=False,
            error_message=message[:500],
            **context,
        )
        if isinstance(last_error, LLMError):
            raise last_error
        raise LLMError(f"模型调用在 {attempts} 次尝试后失败：{message}")

    def generate_text(
        self,
        messages: list[dict],
        purpose: str = "generic",
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
        **context,
    ) -> LLMResponse:
        """Call the model and return free-form text."""
        attempts = max(1, retries if retries is not None else self.config.retries)
        self._require_key()
        last_error: Optional[Exception] = None
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            try:
                content, meta = self._chat(messages, max_tokens, json_mode=False)
                latency_ms = int((time.monotonic() - started) * 1000)
                response = LLMResponse(
                    data={},
                    content=content,
                    model=meta.get("model") or self.config.model,
                    provider_id=self.provider_id,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens"),
                    total_tokens=meta.get("total_tokens"),
                    finish_reason=meta.get("finish_reason", ""),
                    request_id=meta.get("request_id", ""),
                    raw_usage=meta.get("raw_usage"),
                )
                self._record(
                    purpose=purpose,
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    total_tokens=response.total_tokens,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    success=True,
                    error_message="",
                    **context,
                )
                return response
            except NON_RETRYABLE as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(2 ** attempt)

        message = self._safe(str(last_error) if last_error else "unknown error")
        self._record(
            purpose=purpose, model=self.config.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts, success=False, error_message=message[:500], **context,
        )
        if isinstance(last_error, LLMError):
            raise last_error
        raise LLMError(f"模型调用在 {attempts} 次尝试后失败：{message}")

    def test_connection(self) -> dict:
        """Cheap round-trip for the Settings page.

        Never raises and never echoes the credential back to the caller.
        """
        messages = [
            {"role": "system", "content": "You reply with JSON only."},
            {"role": "user", "content": 'Return exactly this JSON: {"status":"ok"}'},
        ]
        started = time.monotonic()
        try:
            response = self.generate_json(
                messages, purpose="test_connection", max_tokens=64, retries=1
            )
        except MissingAPIKey as exc:
            return {"ok": False, "error": str(exc), "latency_ms": 0}
        except LLMError as exc:
            return {
                "ok": False,
                "error": self._safe(str(exc)),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": self._safe(str(exc)), "latency_ms": 0}

        return {
            "ok": True,
            "provider_id": self.provider_id,
            "provider_name": self.config.display_name or self.provider_id,
            "model": response.model,
            "latency_ms": response.latency_ms,
            "status": response.data.get("status", "ok"),
            "total_tokens": response.total_tokens,
        }

    # -- the single method each adapter implements ---------------------------

    @abstractmethod
    def _chat(
        self, messages: list[dict], max_tokens: Optional[int], json_mode: bool
    ) -> tuple[str, dict[str, Any]]:
        """Perform one request.

        Returns ``(content, meta)`` where ``meta`` may carry ``model``,
        ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
        ``finish_reason``, ``request_id`` and ``raw_usage``.
        """
