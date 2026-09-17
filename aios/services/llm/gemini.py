"""Google Gemini ``generateContent`` adapter.

Differences kept inside this file: the model name is part of the URL path, the
credential is a query parameter, turns are ``contents`` with ``parts``, the
assistant role is called ``model``, and generation options live under
``generationConfig``. Gemini does have a native JSON mode
(``responseMimeType: application/json``), which is used when asked for JSON.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import LLMProvider, ProviderCapabilities

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Gemini via ``/models/{model}:generateContent``."""

    provider_id = "gemini"
    capabilities = ProviderCapabilities(
        supports_json_mode=True,
        supports_streaming=True,
        supports_system_prompt=True,
        supports_usage=True,
        supports_temperature=True,
    )

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        contents: list[dict] = []
        for message in messages:
            role = message.get("role")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
                continue
            # Gemini calls the assistant role "model".
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]
        return "\n\n".join(system_parts), contents

    def _chat(
        self, messages: list[dict], max_tokens: Optional[int], json_mode: bool
    ) -> tuple[str, dict[str, Any]]:
        system_prompt, contents = self._split_system(messages)

        generation_config: dict[str, Any] = {
            "temperature": self.config.temperature,
            "maxOutputTokens": max_tokens or self.config.max_tokens,
        }
        if json_mode:
            generation_config["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        for key, value in (self.config.extra_body or {}).items():
            payload.setdefault(key, value)

        url = self.config.endpoint(f"/models/{self.config.model}:generateContent")
        response = self._http.post(
            url,
            headers={"Content-Type": "application/json"},
            # The key is a query parameter for this API, not a header.
            params={"key": self.config.api_key or ""},
            json=payload,
            timeout=self.config.timeout,
        )
        self._raise_for_status(response.status_code)
        response.raise_for_status()

        body = response.json()
        candidates = body.get("candidates") or []
        if not candidates:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            raise ValueError(f"Gemini 未返回候选结果{f'（{blocked}）' if blocked else ''}")

        parts = (candidates[0].get("content") or {}).get("parts") or []
        content = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        )

        usage = body.get("usageMetadata") or {}
        return content, {
            "model": body.get("modelVersion") or self.config.model,
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
            "finish_reason": candidates[0].get("finishReason") or "",
            "request_id": body.get("responseId") or "",
            "raw_usage": usage or None,
        }
