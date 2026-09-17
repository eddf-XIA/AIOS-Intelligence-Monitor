"""Anthropic Messages API adapter.

Differences kept inside this file: the system prompt is a top-level field
rather than a message, ``max_tokens`` is required, the credential travels in
``x-api-key``, and the reply is a list of content blocks. There is no
``response_format``, so JSON is requested in the prompt and parsed by the
shared extractor.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import LLMProvider, ProviderCapabilities

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

#: Appended to the system prompt because Anthropic has no JSON mode.
JSON_DIRECTIVE = "You must reply with a single valid JSON object and nothing else."


class AnthropicProvider(LLMProvider):
    """Claude via ``/messages``."""

    provider_id = "anthropic"
    capabilities = ProviderCapabilities(
        supports_json_mode=False,
        supports_streaming=True,
        supports_system_prompt=True,
        supports_usage=True,
        supports_temperature=True,
        supports_reasoning=True,
    )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
        }

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        """Anthropic takes the system prompt out of the message list."""
        system_parts: list[str] = []
        turns: list[dict] = []
        for message in messages:
            role = message.get("role")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                turns.append({"role": role, "content": content})
        if not turns:
            turns = [{"role": "user", "content": ""}]
        return "\n\n".join(system_parts), turns

    def _chat(
        self, messages: list[dict], max_tokens: Optional[int], json_mode: bool
    ) -> tuple[str, dict[str, Any]]:
        system_prompt, turns = self._split_system(messages)
        if json_mode:
            system_prompt = (system_prompt + "\n\n" + JSON_DIRECTIVE).strip()

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": turns,
            # Required by the API, unlike the OpenAI dialect.
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            payload["system"] = system_prompt
        for key, value in (self.config.extra_body or {}).items():
            payload.setdefault(key, value)

        response = self._http.post(
            self.config.endpoint("/messages"),
            headers=self._headers(),
            json=payload,
            timeout=self.config.timeout,
        )
        self._raise_for_status(response.status_code)
        response.raise_for_status()

        body = response.json()
        blocks = body.get("content") or []
        content = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

        usage = body.get("usage") or {}
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        total = (
            (prompt_tokens or 0) + (completion_tokens or 0)
            if prompt_tokens is not None or completion_tokens is not None
            else None
        )
        return content, {
            "model": body.get("model"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
            "finish_reason": body.get("stop_reason") or "",
            "request_id": body.get("id") or "",
            "raw_usage": usage or None,
        }
