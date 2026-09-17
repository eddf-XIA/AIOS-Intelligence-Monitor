"""Turning a stored provider configuration into a live provider object."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import requests

from .anthropic import AnthropicProvider
from .base import LLMProvider, ProviderRuntimeConfig
from .gemini import GeminiProvider
from .openai_compatible import make_openai_compatible
from .presets import (
    API_STYLE_ANTHROPIC,
    API_STYLE_GEMINI,
    ProviderPreset,
    preset_or_custom,
)

logger = logging.getLogger(__name__)

#: Built once per preset so ``provider_id`` stays accurate in usage records.
_OPENAI_CLASSES: dict[str, type[LLMProvider]] = {}


def provider_class_for(preset: ProviderPreset) -> type[LLMProvider]:
    """The adapter class implementing this preset's API style."""
    if preset.api_style == API_STYLE_ANTHROPIC:
        return AnthropicProvider
    if preset.api_style == API_STYLE_GEMINI:
        return GeminiProvider

    cached = _OPENAI_CLASSES.get(preset.provider_id)
    if cached is None:
        cached = make_openai_compatible(preset, preset.capabilities)
        _OPENAI_CLASSES[preset.provider_id] = cached
    return cached


def runtime_config(
    provider_id: str,
    base_url: str = "",
    model: str = "",
    api_key: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: int = 180,
    retries: int = 3,
    extra_body: Optional[dict[str, Any]] = None,
    display_name: str = "",
) -> ProviderRuntimeConfig:
    """Merge stored settings over the preset's defaults."""
    preset = preset_or_custom(provider_id)

    merged_extra: dict[str, Any] = dict(preset.extra_body or {})
    merged_extra.update(extra_body or {})

    return ProviderRuntimeConfig(
        provider_id=preset.provider_id,
        base_url=(base_url or preset.default_base_url).strip(),
        model=(model or preset.example_model).strip(),
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
        extra_body=merged_extra,
        display_name=display_name or preset.display_name,
    )


def build_provider(
    config: ProviderRuntimeConfig,
    usage_sink: Optional[Callable[[dict], None]] = None,
    session: Optional[requests.Session] = None,
) -> LLMProvider:
    """Instantiate the right adapter for a runtime configuration."""
    preset = preset_or_custom(config.provider_id)
    provider_cls = provider_class_for(preset)
    return provider_cls(config=config, usage_sink=usage_sink, session=session)


def build_from_row(
    row,
    api_key: Optional[str],
    usage_sink: Optional[Callable[[dict], None]] = None,
    session: Optional[requests.Session] = None,
    model_override: str = "",
) -> LLMProvider:
    """Build a provider from an :class:`~aios.models.LLMProviderConfig` row.

    The credential is passed in separately and never read from the row - it
    lives in the OS keyring.
    """
    config = runtime_config(
        provider_id=row.provider_id,
        base_url=row.base_url or "",
        model=(model_override or row.default_model or ""),
        api_key=api_key,
        temperature=row.temperature if row.temperature is not None else 0.2,
        max_tokens=row.max_tokens or 4000,
        timeout=row.timeout_seconds or 180,
        retries=row.retries or 3,
        extra_body=row.extra_config_json or {},
        display_name=row.display_name or "",
    )
    return build_provider(config, usage_sink=usage_sink, session=session)
