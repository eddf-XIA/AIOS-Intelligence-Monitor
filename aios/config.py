"""Filesystem layout and process-level configuration.

Runtime *behaviour* (model name, lookback days, scheduler time, ...) lives in
the database and is reached through :mod:`aios.services.settings_service`.
This module only owns things that must be known before the database exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Service name used for the OS credential store (Windows Credential Manager).
KEYRING_SERVICE = "AIOS Intelligence Monitor"
#: Legacy single-provider slot, kept so an upgrade can find and migrate an
#: existing key. New credentials use ``provider:<provider_id>`` instead.
KEYRING_USERNAME = "deepseek_api_key"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Paths:
    """Resolved on-disk locations. All data stays on the user's machine."""

    data_dir: Path
    db_path: Path
    reports_dir: Path
    logs_dir: Path
    cache_dir: Path

    def ensure(self) -> "Paths":
        for directory in (self.data_dir, self.reports_dir, self.logs_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


def build_paths(data_dir: Path | str | None = None) -> Paths:
    base = Path(data_dir).expanduser().resolve() if data_dir else _env_path(
        "AIOS_DATA_DIR", PROJECT_ROOT / "data"
    )
    return Paths(
        data_dir=base,
        db_path=base / "aios.db",
        reports_dir=base / "reports",
        logs_dir=base / "logs",
        cache_dir=base / "cache",
    )


#: Module-level singleton used by the running application.
#:
#: Always reach it through :func:`get_paths` rather than importing this name:
#: ``from .config import PATHS`` binds the object at import time, which makes
#: the data directory impossible to redirect afterwards (and silently shares one
#: directory between tests).
PATHS = build_paths()


def get_paths() -> Paths:
    """The active filesystem layout. Resolved at call time, never cached."""
    return PATHS


def set_paths(paths: Paths) -> Paths:
    """Point the application at a different data directory (tests, ``AIOS_DATA_DIR``)."""
    global PATHS
    PATHS = paths.ensure()
    return PATHS

USER_AGENT = "Mozilla/5.0 (compatible; AIOS-Monitor/2.0; +local-research)"

#: Defaults seeded into the settings table on first launch. Values are strings
#: because the settings table stores everything as text.
#:
#: The ``deepseek_*`` entries are the legacy single-provider block. They are
#: read once by :mod:`aios.services.provider_migration` and then superseded by
#: the ``llm_provider_configs`` table.
DEFAULT_SETTINGS: dict[str, str] = {
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-flash",
    "deepseek_temperature": "0.2",
    "deepseek_max_tokens": "4000",
    "deepseek_timeout": "180",
    "deepseek_retries": "3",
    "deepseek_thinking": "disabled",
    "default_lookback_days": "3",
    "default_max_candidates": "18",
    "default_max_report_items": "2",
    "article_max_chars": "7000",
    "http_timeout": "20",
    "collect_workers": "6",
    "fetch_body_top_n": "8",
    "scheduler_enabled": "false",
    "scheduler_time": "06:00",
    "event_match_lookback_days": "45",
    "event_match_enabled": "true",
    "report_output_dir": "",

    # --- network and data sources ---
    # "auto" asks the sources whether they are reachable before spending a
    # whole run finding out one 25-second timeout at a time.
    "network_mode": "auto",
    "connection_mode": "system",
    "http_proxy": "",
    "https_proxy": "",
    "proxy_username": "",
    "collector_gdelt_enabled": "true",
    "collector_google_news_enabled": "true",
    "collector_rss_enabled": "true",
    # GDELT is strict and shared by everyone; one request at a time, spaced.
    "gdelt_min_interval_seconds": "2.0",
    "gdelt_max_concurrency": "1",
    "gdelt_failure_threshold": "5",
    # Google News costs a full connect timeout when unreachable, so it gets a
    # shorter fuse than the others.
    "google_news_timeout": "12",
    "google_news_failure_threshold": "3",
    "collector_circuit_reset_seconds": "300",
    "collection_cache_ttl_minutes": "30",
    "collection_min_candidates": "1",
    "health_check_timeout": "6",
    "network_precheck_enabled": "true",
}
