"""Carrying pre-multi-provider installs onto the generic LLM layer.

Before this iteration the app had exactly one backend, configured through
``app_settings`` keys (``deepseek_base_url``, ``deepseek_model`` ...) and a
single keyring slot (``deepseek_api_key``). Both are migrated in place:

* the settings become an ``LLMProviderConfig`` row for ``deepseek``, marked as
  the default, so the user's model/base URL/timeouts are preserved;
* the credential is copied to ``provider:deepseek`` and the legacy slot is left
  untouched, so a downgrade still finds its key.

Runs exactly once - on a database that has no provider rows yet.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..repositories import providers as providers_repo
from . import keyring_service, settings_service
from .llm.presets import get_preset

logger = logging.getLogger(__name__)

#: The provider every pre-existing install was using.
LEGACY_PROVIDER_ID = "deepseek"


def migrate_legacy_provider(session: Session) -> bool:
    """Create the DeepSeek provider row from legacy settings, once.

    Returns True when a row was created by this call.
    """
    if providers_repo.count_providers(session) > 0:
        return False

    preset = get_preset(LEGACY_PROVIDER_ID)
    assert preset is not None, "deepseek preset must exist"

    # Read whatever the old single-provider settings held; the getters fall back
    # to sane defaults so a brand-new database also lands here cleanly.
    base_url = settings_service.get_str(
        session, "deepseek_base_url", preset.default_base_url
    )
    model = settings_service.get_str(session, "deepseek_model", "")
    temperature = settings_service.get_float(session, "deepseek_temperature", 0.2)
    max_tokens = settings_service.get_int(session, "deepseek_max_tokens", 4000)
    timeout = settings_service.get_int(session, "deepseek_timeout", 180)
    retries = settings_service.get_int(session, "deepseek_retries", 3)

    api_key = _migrate_credential()
    last_four = keyring_service.last4(api_key) if api_key else settings_service.get_str(
        session, settings_service.KEY_LAST4, ""
    )

    row = providers_repo.create_provider(
        session,
        provider_id=LEGACY_PROVIDER_ID,
        display_name=preset.display_name,
        base_url=base_url or preset.default_base_url,
        default_model=model or preset.example_model,
        enabled=True,
        is_default=True,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout,
        retries=retries,
        has_api_key=bool(api_key),
        api_key_last_four=last_four,
        extra_config_json=None,
    )
    logger.info(
        "Migrated legacy DeepSeek settings into provider config #%s (key present: %s)",
        row.id, bool(api_key),
    )
    return True


def _migrate_credential() -> str | None:
    """Copy the legacy keyring slot to ``provider:deepseek`` if needed.

    Never forces the user to re-enter a key that is already stored.
    """
    existing = keyring_service.get_provider_key(LEGACY_PROVIDER_ID)
    if existing:
        return existing

    legacy = keyring_service.get_api_key()
    if not legacy:
        return None

    try:
        keyring_service.set_provider_key(LEGACY_PROVIDER_ID, legacy)
        logger.info("Copied the legacy DeepSeek credential to provider:deepseek")
    except Exception as exc:  # pragma: no cover - depends on the OS vault
        logger.warning("Could not migrate the legacy credential: %s", type(exc).__name__)
        return legacy
    return legacy


def reconcile_key_state(session: Session) -> int:
    """Realign every provider's key mirror with the OS vault.

    ``has_api_key`` is a *mirror* of the keyring, kept so the UI can render
    without opening the vault on every request. Mirrors drift: a credential
    written while the vault was briefly unavailable, a vault restored from a
    different machine, a key added out of band. When it drifts the application
    refuses to start a run while being perfectly able to call the model, which
    is the worst of both worlds.

    Clearing the flag requires the vault to actually answer. If the credential
    store is unavailable we leave every row alone rather than declaring the
    user's keys gone.

    Returns the number of rows corrected.
    """
    vault_available = keyring_service.is_available()
    corrected = 0

    for row in providers_repo.list_providers(session):
        try:
            key = keyring_service.get_provider_key(row.provider_id)
        except Exception:  # pragma: no cover - platform dependent
            continue

        if key:
            expected_last_four = keyring_service.last4(key)
            if not row.has_api_key or row.api_key_last_four != expected_last_four:
                row.has_api_key = True
                row.api_key_last_four = expected_last_four
                corrected += 1
                logger.info(
                    "Provider %s has a stored credential the database had not recorded",
                    row.provider_id,
                )
        elif row.has_api_key and vault_available:
            row.has_api_key = False
            row.api_key_last_four = ""
            corrected += 1
            logger.info(
                "Provider %s no longer has a credential in the vault", row.provider_id
            )

    if corrected:
        session.flush()
    return corrected


def sync_key_state(session: Session, provider_id: str) -> None:
    """Refresh a provider row's key mirror from the vault.

    The row only ever stores ``has_api_key`` and the last four characters.
    """
    row = providers_repo.get_by_provider_id(session, provider_id)
    if row is None:
        return
    key = keyring_service.get_provider_key(provider_id)
    row.has_api_key = bool(key)
    row.api_key_last_four = keyring_service.last4(key) if key else ""
    session.flush()
