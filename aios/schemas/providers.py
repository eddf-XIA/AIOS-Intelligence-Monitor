"""Validation for AI provider configuration forms."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProviderForm(BaseModel):
    """Create/update payload for one AI provider.

    ``model`` and ``base_url`` are free text on purpose: Chinese vendors rename
    models often, and users run these APIs through official endpoints,
    enterprise endpoints, proxy gateways and self-hosted servers.
    """

    provider_id: str = Field(min_length=1, max_length=64)
    display_name: str = ""
    base_url: str = ""
    default_model: str = Field(default="", max_length=200)
    enabled: bool = True
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4000, ge=256, le=200_000)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    retries: int = Field(default=3, ge=1, le=6)

    @field_validator("provider_id")
    @classmethod
    def clean_provider_id(cls, value: str) -> str:
        provider_id = (value or "").strip().lower()
        if not provider_id:
            raise ValueError("必须选择一个服务商。")
        return provider_id

    @field_validator("base_url")
    @classmethod
    def clean_base_url(cls, value: str) -> str:
        url = (value or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头。")
        return url

    @field_validator("display_name", "default_model")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return (value or "").strip()


class ProviderKeyForm(BaseModel):
    """A credential on its way to the OS keyring.

    Exists only for the lifetime of the request; never persisted to SQLite,
    never logged, never rendered.
    """

    api_key: str = Field(min_length=1, max_length=500)

    @field_validator("api_key")
    @classmethod
    def clean(cls, value: str) -> str:
        key = (value or "").strip()
        if not key:
            raise ValueError("API Key 不能为空。")
        if key.startswith("•"):
            raise ValueError("这是掩码占位符，请粘贴真实的 API Key。")
        return key


class TaskRouteForm(BaseModel):
    """One task's optional provider/model override."""

    task: str = Field(min_length=1, max_length=64)
    #: 0 / empty means "use the default provider".
    provider_config_id: Optional[int] = None
    model: str = Field(default="", max_length=200)

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        return (value or "").strip()


class ProviderView(BaseModel):
    """What the browser is allowed to know about a configured provider."""

    id: int
    provider_id: str
    display_name: str
    base_url: str
    default_model: str
    enabled: bool
    is_default: bool
    configured: bool
    #: Masked tail only - never the full credential.
    masked_key: str = ""
    requires_api_key: bool = True
