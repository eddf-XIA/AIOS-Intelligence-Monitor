"""The ``/chat/completions`` adapter.

Most providers AIOS targets - DeepSeek, 通义千问, 智谱 GLM, Kimi, 豆包/火山方舟,
MiniMax, 腾讯混元, 百度千帆, 硅基流动, OpenAI, OpenRouter, Ollama, LM Studio -
speak this dialect. They get one well-tested HTTP path instead of ten
near-identical clients; per-vendor differences live in the presets
(``base_url``, ``extra_body``, capability flags), not in code.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import LLMProvider, ProviderCapabilities

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Chat completions over the OpenAI wire format."""

    provider_id = "openai_compatible"
    capabilities = ProviderCapabilities()

    #: Providers whose JSON mode is unreliable set this via the preset.
    json_mode_enabled = True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _build_payload(
        self, messages: list[dict], max_tokens: Optional[int], json_mode: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._prepare_messages(messages),
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        # Only send parameters the backend actually accepts.
        if self.capabilities.supports_temperature:
            payload["temperature"] = self.config.temperature
        if json_mode and self.capabilities.supports_json_mode and self.json_mode_enabled:
            payload["response_format"] = {"type": "json_object"}

        # Non-secret vendor extras from the preset / provider config, e.g.
        # DeepSeek's {"thinking": {"type": "disabled"}}.
        for key, value in (self.config.extra_body or {}).items():
            payload.setdefault(key, value)
        return payload

    def _prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Fold the system prompt into the first user turn when unsupported."""
        if self.capabilities.supports_system_prompt:
            return messages

        folded: list[dict] = []
        carried: list[str] = []
        for message in messages:
            if message.get("role") == "system":
                carried.append(str(message.get("content", "")))
                continue
            if carried and message.get("role") == "user":
                content = "\n\n".join(carried + [str(message.get("content", ""))])
                folded.append({"role": "user", "content": content})
                carried = []
            else:
                folded.append(message)
        if carried:
            folded.insert(0, {"role": "user", "content": "\n\n".join(carried)})
        return folded

    def _chat(
        self, messages: list[dict], max_tokens: Optional[int], json_mode: bool
    ) -> tuple[str, dict[str, Any]]:
        response = self._http.post(
            self.config.endpoint("/chat/completions"),
            headers=self._headers(),
            json=self._build_payload(messages, max_tokens, json_mode),
            timeout=self.config.timeout,
        )
        self._raise_for_status(response.status_code)
        response.raise_for_status()

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise ValueError(f"响应中没有 choices：{str(body)[:200]}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            # Some gateways stream-shape the reply even for non-stream requests.
            content = (choices[0].get("delta") or {}).get("content", "")
        if isinstance(content, list):
            # Multi-part content blocks (a few OpenAI-compatible gateways).
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        usage = body.get("usage") or {}
        return str(content or ""), {
            "model": body.get("model"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": choices[0].get("finish_reason") or "",
            "request_id": body.get("id") or "",
            "raw_usage": usage or None,
        }


def make_openai_compatible(preset, capabilities: ProviderCapabilities):
    """Build a provider class bound to one preset's id and capabilities.

    Keeps ``provider_id`` accurate for usage accounting and log lines without
    writing a near-empty subclass by hand for every vendor.
    """

    return type(
        f"{preset.provider_id.title().replace('_', '')}Provider",
        (OpenAICompatibleProvider,),
        {
            "provider_id": preset.provider_id,
            "capabilities": capabilities,
            "json_mode_enabled": capabilities.supports_json_mode,
            "__doc__": f"{preset.display_name} via the OpenAI-compatible API.",
        },
    )
