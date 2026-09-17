"""Provider and task-routing configuration.

Secrets are absent by construction: this table can only hold
``has_api_key`` and ``api_key_last_four``. The credential itself lives in the OS
keyring under ``provider:<provider_id>``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class LLMProviderConfig(Base, TimestampMixin):
    """One configured AI provider.

    Several may be configured at once; ``is_default`` marks the one used when a
    task has no specific route.
    """

    __tablename__ = "llm_provider_configs"
    __table_args__ = (UniqueConstraint("provider_id", name="uq_llm_provider_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Stable English identifier matching a preset, e.g. ``deepseek``, ``qwen``.
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")

    base_url: Mapped[str] = mapped_column(String(400), default="")
    default_model: Mapped[str] = mapped_column(String(200), default="")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4000)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=180)
    retries: Mapped[int] = mapped_column(Integer, default=3)

    #: Mirror of the keyring state, so the UI need not touch the vault to render.
    has_api_key: Mapped[bool] = mapped_column(Boolean, default=False)
    api_key_last_four: Mapped[str] = mapped_column(String(8), default="")

    #: Non-secret vendor extras only. Never store credentials here.
    extra_config_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    routes: Mapped[list["TaskModelRoute"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )

    @property
    def is_configured(self) -> bool:
        """Usable for a real call: a model, and a key unless the vendor needs none."""
        from ..services.llm.presets import preset_or_custom

        if not self.default_model.strip():
            return False
        preset = preset_or_custom(self.provider_id)
        if not preset.requires_api_key:
            return True
        return self.has_api_key

    @property
    def masked_key(self) -> str:
        """The only credential representation allowed to reach a template."""
        if not self.has_api_key:
            return ""
        return "•" * 12 + (self.api_key_last_four or "")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<LLMProviderConfig {self.provider_id}>"


class TaskModelRoute(Base, TimestampMixin):
    """Optional provider/model override for one intelligence task.

    A row with ``provider_config_id`` NULL means "use the default provider".
    Cheap models suffice for filtering and event matching; synthesis may deserve
    a stronger one. Routing lives here so business code never chooses a vendor.
    """

    __tablename__ = "task_model_routes"
    __table_args__ = (UniqueConstraint("task", name="uq_task_route"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    #: One of :data:`aios.services.llm.service.TASKS`.
    task: Mapped[str] = mapped_column(String(64), index=True)

    provider_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Blank means "the provider's default model".
    model: Mapped[str] = mapped_column(String(200), default="")

    provider: Mapped[Optional[LLMProviderConfig]] = relationship(back_populates="routes")

    @property
    def uses_default(self) -> bool:
        return self.provider_config_id is None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TaskModelRoute {self.task}>"
