"""Pluggable LLM layer.

AIOS owns the intelligence workflow; the model is a replaceable reasoning
engine. Business services (topic analysis, event matching, report synthesis)
depend only on the names exported here - never on a vendor SDK or on a
vendor-specific response shape.
"""

from .base import (
    AuthenticationFailed,
    InsufficientBalance,
    InvalidJSONResponse,
    LLMError,
    LLMProvider,
    LLMResponse,
    MissingAPIKey,
    ProviderCapabilities,
    ProviderNotConfigured,
    ProviderRuntimeConfig,
    RateLimited,
    extract_json,
)
from .presets import PROVIDER_PRESETS, ProviderPreset, get_preset, preset_choices
from .registry import build_provider
from .service import LLMService, TASKS, build_llm_service

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "ProviderCapabilities",
    "ProviderRuntimeConfig",
    "LLMError",
    "MissingAPIKey",
    "RateLimited",
    "InsufficientBalance",
    "AuthenticationFailed",
    "InvalidJSONResponse",
    "ProviderNotConfigured",
    "extract_json",
    "PROVIDER_PRESETS",
    "ProviderPreset",
    "get_preset",
    "preset_choices",
    "build_provider",
    "LLMService",
    "build_llm_service",
    "TASKS",
]
