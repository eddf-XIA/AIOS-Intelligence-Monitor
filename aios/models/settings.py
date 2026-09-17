"""Key/value application settings."""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    """One configuration value.

    Secrets are never stored here - the DeepSeek key lives in the OS keyring and
    only its masked tail (``deepseek_key_last4``) may be persisted.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AppSetting {self.key}={self.value!r}>"
