"""Typed access to the key/value settings table.

Everything is stored as text; the getters coerce. A missing key falls back to
:data:`aios.config.DEFAULT_SETTINGS` so a partially-populated table never
crashes the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DEFAULT_SETTINGS
from ..models import AppSetting
from . import keyring_service

logger = logging.getLogger(__name__)

#: Persisted so the UI can show a mask without touching the credential store.
KEY_LAST4 = "deepseek_key_last4"

_TRUE = {"1", "true", "yes", "on"}


def ensure_default_settings(session: Session) -> None:
    """Insert any missing default. Never overwrites a user value."""
    existing = set(session.scalars(select(AppSetting.key)))
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(AppSetting(key=key, value=value))
    session.flush()


def get_raw(session: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    row = session.get(AppSetting, key)
    if row is not None and row.value != "":
        return row.value
    if row is not None and row.value == "" and key in DEFAULT_SETTINGS:
        # An explicitly blanked value is still a real value for free-text keys.
        return row.value if default is None else row.value
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key)


def get_str(session: Session, key: str, default: str = "") -> str:
    value = get_raw(session, key)
    return value if value is not None else default


def get_int(session: Session, key: str, default: int = 0) -> int:
    try:
        return int(str(get_raw(session, key) or default).strip())
    except (TypeError, ValueError):
        return default


def get_float(session: Session, key: str, default: float = 0.0) -> float:
    try:
        return float(str(get_raw(session, key) or default).strip())
    except (TypeError, ValueError):
        return default


def get_bool(session: Session, key: str, default: bool = False) -> bool:
    value = get_raw(session, key)
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE


def set_value(session: Session, key: str, value) -> None:
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = "" if value is None else str(value)
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=text))
    else:
        row.value = text
    session.flush()


def set_many(session: Session, values: dict) -> None:
    for key, value in values.items():
        set_value(session, key, value)


def all_settings(session: Session) -> dict[str, str]:
    stored = {row.key: row.value for row in session.scalars(select(AppSetting))}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(stored)
    return merged


@dataclass(frozen=True)
class DeepSeekConfig:
    """Legacy single-provider settings.

    Kept only so :mod:`aios.services.provider_migration` can read what a
    pre-multi-provider install had configured. New code resolves providers
    through :class:`aios.services.llm.LLMService`.
    """

    base_url: str
    model: str
    temperature: float
    max_tokens: int
    timeout: int
    retries: int
    thinking: str

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def deepseek_config(session: Session) -> DeepSeekConfig:
    """Read the legacy DeepSeek settings block (migration path only)."""
    return DeepSeekConfig(
        base_url=get_str(session, "deepseek_base_url", "https://api.deepseek.com"),
        model=get_str(session, "deepseek_model", "deepseek-flash"),
        temperature=get_float(session, "deepseek_temperature", 0.2),
        max_tokens=get_int(session, "deepseek_max_tokens", 4000),
        timeout=get_int(session, "deepseek_timeout", 180),
        retries=get_int(session, "deepseek_retries", 3),
        thinking=get_str(session, "deepseek_thinking", "disabled"),
    )


# --- API key state ----------------------------------------------------------

def api_key_status(session: Session) -> dict:
    """What the Settings page may safely display about the key."""
    key = keyring_service.get_api_key()
    if key:
        tail = keyring_service.last4(key)
        if get_str(session, KEY_LAST4) != tail:
            set_value(session, KEY_LAST4, tail)
        return {
            "configured": True,
            "masked": keyring_service.mask(key),
            "last4": tail,
            "backend": keyring_service.backend_name(),
        }
    return {
        "configured": False,
        "masked": "",
        "last4": get_str(session, KEY_LAST4),
        "backend": keyring_service.backend_name(),
    }


def save_api_key(session: Session, value: str) -> dict:
    """Store the key in the OS vault; persist only its masked tail."""
    keyring_service.set_api_key(value)
    set_value(session, KEY_LAST4, keyring_service.last4(value))
    return api_key_status(session)


def clear_api_key(session: Session) -> bool:
    removed = keyring_service.delete_api_key()
    set_value(session, KEY_LAST4, "")
    return removed
