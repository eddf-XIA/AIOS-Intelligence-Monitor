"""Secret storage backed by the OS credential vault.

On Windows this resolves to the Windows Credential Manager. The full DeepSeek
key is *only* ever held here and in process memory. It must never reach the
database, a log line, an HTML report or a JSON audit file - :func:`redact`
exists so any text on its way to one of those places can be scrubbed first.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..config import KEYRING_SERVICE, KEYRING_USERNAME

#: Every provider credential is stored under its own username slot.
PROVIDER_PREFIX = "provider:"

logger = logging.getLogger(__name__)

#: Matches common API-key shapes so they can be masked in free text.
_SECRET_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{12,})")


class KeyringUnavailable(RuntimeError):
    """Raised when no OS credential backend can store secrets."""


def _backend():
    import keyring

    return keyring


def backend_name() -> str:
    """Human-readable name of the active credential backend."""
    try:
        import keyring

        return type(keyring.get_keyring()).__name__
    except Exception as exc:  # pragma: no cover - platform dependent
        return f"unavailable ({exc})"


def is_available() -> bool:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:  # pragma: no cover
        return False


def set_api_key(value: str, service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME) -> None:
    """Store the DeepSeek key in the OS vault."""
    value = (value or "").strip()
    if not value:
        raise ValueError("API key is empty")
    try:
        _backend().set_password(service, username, value)
    except Exception as exc:  # pragma: no cover - depends on OS vault
        raise KeyringUnavailable(str(exc)) from exc
    logger.info("DeepSeek API key stored in %s", backend_name())


def get_api_key(service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME) -> Optional[str]:
    """Read the DeepSeek key, or None when it has not been configured."""
    try:
        value = _backend().get_password(service, username)
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not read credential store: %s", exc)
        return None
    return value.strip() if value else None


def delete_api_key(service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME) -> bool:
    """Remove the stored key. Returns True when something was deleted."""
    from keyring.errors import PasswordDeleteError

    try:
        _backend().delete_password(service, username)
        return True
    except PasswordDeleteError:
        return False
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.warning("Could not delete credential: %s", type(exc).__name__)
        return False


def has_api_key() -> bool:
    return bool(get_api_key())


def provider_username(provider_id: str) -> str:
    """Keyring username slot for one provider, e.g. ``provider:deepseek``.

    Credentials are isolated per provider so configuring 通义千问 never disturbs
    an existing DeepSeek key.
    """
    return f"{PROVIDER_PREFIX}{(provider_id or '').strip().lower()}"


def set_provider_key(provider_id: str, value: str) -> None:
    """Store one provider's credential."""
    set_api_key(value, username=provider_username(provider_id))


def get_provider_key(provider_id: str) -> Optional[str]:
    """Read one provider's credential, or None when unset."""
    return get_api_key(username=provider_username(provider_id))


def delete_provider_key(provider_id: str) -> bool:
    """Remove one provider's credential."""
    return delete_api_key(username=provider_username(provider_id))


def has_provider_key(provider_id: str) -> bool:
    return bool(get_provider_key(provider_id))


def mask(value: Optional[str], visible: int = 4) -> str:
    """Render a key as ``********6307`` - never the full value.

    This is the only representation allowed to leave the process.
    """
    if not value:
        return ""
    tail = value[-visible:] if len(value) > visible else value
    return "•" * 12 + tail


def last4(value: Optional[str], visible: int = 4) -> str:
    """The trailing characters that may safely be persisted for display."""
    if not value:
        return ""
    return value[-visible:] if len(value) > visible else value


def redact(text: str, *extra_secrets: Optional[str]) -> str:
    """Strip anything key-shaped, plus any explicitly supplied secret."""
    if not text:
        return text
    cleaned = _SECRET_PATTERN.sub("[REDACTED]", text)
    for secret in extra_secrets:
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned
