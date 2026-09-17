"""Task-aware LLM router.

One place resolves everything a call needs: which task is running, which
provider serves that task, which model, which credential, and where usage is
recorded. Intelligence services call :meth:`LLMService.complete_json` and never
learn which vendor answered.

The method name and signature match the original ``DeepSeekClient.complete_json``
so migrating the pipeline to multi-provider needed no changes in the analysis
prompts or the event matcher's control flow.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from ...database import session_scope
from .base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    MissingAPIKey,
    ProviderNotConfigured,
)
from .presets import preset_or_custom
from .registry import build_from_row, build_provider, runtime_config

logger = logging.getLogger(__name__)


#: Tasks that may be routed to a different model. Keys are stable; the labels
#: are what the Settings page shows.
TASKS: dict[str, str] = {
    "topic_analysis": "主题情报分析",
    "module_analysis": "领域小结",
    "event_matching": "事件归并判断",
    "synthesis": "日报综合研判",
}

#: Tasks the Settings page exposes as overrides (kept small on purpose).
OVERRIDABLE_TASKS = ("topic_analysis", "event_matching", "synthesis")


@dataclass(frozen=True)
class Resolution:
    """Which backend will serve one task."""

    provider_id: str
    display_name: str
    model: str
    config_id: Optional[int]
    from_route: bool


class LLMService:
    """Resolves and invokes providers on behalf of the intelligence pipeline."""

    def __init__(
        self,
        usage_sink: Optional[Callable[[dict], None]] = None,
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self._usage_sink = usage_sink
        self._http = http_session or requests.Session()
        self._lock = threading.Lock()
        #: (config_id, model) -> provider, so one run reuses connections.
        self._cache: dict[tuple[Optional[int], str], LLMProvider] = {}
        self._snapshot: Optional[dict[str, Any]] = None

    # -- configuration snapshot ---------------------------------------------

    def load(self) -> dict[str, Any]:
        """Read provider config once, so a long run is not re-querying per call.

        Returns a plain dict - no ORM objects escape the session.
        """
        from ...repositories import providers as providers_repo
        from ..keyring_service import get_provider_key

        with session_scope() as session:
            default_row = providers_repo.default_provider(session)
            rows = {}
            for row in providers_repo.list_providers(session, enabled_only=True):
                rows[row.id] = {
                    "id": row.id,
                    "provider_id": row.provider_id,
                    "display_name": row.display_name,
                    "base_url": row.base_url,
                    "default_model": row.default_model,
                    "temperature": row.temperature,
                    "max_tokens": row.max_tokens,
                    "timeout_seconds": row.timeout_seconds,
                    "retries": row.retries,
                    "extra_config_json": row.extra_config_json or {},
                }
            routes = {
                task: {
                    "provider_config_id": route.provider_config_id,
                    "model": route.model,
                }
                for task, route in providers_repo.routes_by_task(session).items()
            }
            snapshot = {
                "default_id": default_row.id if default_row else None,
                "providers": rows,
                "routes": routes,
            }

        # Credentials are fetched from the keyring, never from the database.
        for config_id, row in snapshot["providers"].items():
            row["api_key"] = get_provider_key(row["provider_id"])

        self._snapshot = snapshot
        return snapshot

    def _ensure_snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            self.load()
        assert self._snapshot is not None
        return self._snapshot

    # -- resolution ----------------------------------------------------------

    def resolve(self, task: str = "generic") -> Resolution:
        """Decide which provider and model serve ``task``."""
        snapshot = self._ensure_snapshot()
        providers = snapshot["providers"]
        if not providers:
            raise ProviderNotConfigured(
                "尚未配置任何 AI 模型服务。请在「设置 → AI 模型」中添加。"
            )

        route = snapshot["routes"].get(task) or {}
        config_id = route.get("provider_config_id")
        model_override = (route.get("model") or "").strip()

        row = providers.get(config_id) if config_id else None
        from_route = row is not None
        if row is None:
            row = providers.get(snapshot["default_id"])
        if row is None:
            # Default was disabled or removed; fall back to any enabled provider.
            row = next(iter(providers.values()))
            from_route = False

        model = model_override or row["default_model"]
        if not model:
            model = preset_or_custom(row["provider_id"]).example_model
        if not model:
            raise ProviderNotConfigured(
                f"{row['display_name'] or row['provider_id']} 尚未设置模型名称。"
            )

        return Resolution(
            provider_id=row["provider_id"],
            display_name=row["display_name"] or row["provider_id"],
            model=model,
            config_id=row["id"],
            from_route=from_route,
        )

    def provider_for(self, task: str = "generic") -> LLMProvider:
        """A ready provider instance for ``task``, cached per (config, model)."""
        resolution = self.resolve(task)
        cache_key = (resolution.config_id, resolution.model)

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            snapshot = self._ensure_snapshot()
            row = snapshot["providers"][resolution.config_id]
            config = runtime_config(
                provider_id=row["provider_id"],
                base_url=row["base_url"],
                model=resolution.model,
                api_key=row.get("api_key"),
                temperature=row["temperature"] if row["temperature"] is not None else 0.2,
                max_tokens=row["max_tokens"] or 4000,
                timeout=row["timeout_seconds"] or 180,
                retries=row["retries"] or 3,
                extra_body=row["extra_config_json"],
                display_name=row["display_name"],
            )
            provider = build_provider(
                config, usage_sink=self._usage_sink, session=self._http
            )
            self._cache[cache_key] = provider
            return provider

    # -- what business code calls -------------------------------------------

    def complete_json(
        self,
        messages: list[dict],
        purpose: str = "generic",
        max_tokens: Optional[int] = None,
        module_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> LLMResponse:
        """Structured call for one task. ``purpose`` is the routing key."""
        provider = self.provider_for(purpose)
        return provider.generate_json(
            messages,
            purpose=purpose,
            max_tokens=max_tokens,
            retries=retries,
            module_id=module_id,
            topic_id=topic_id,
        )

    def generate_json(self, messages: list[dict], task: str = "generic", **kwargs) -> LLMResponse:
        """Spec-facing alias for :meth:`complete_json`."""
        return self.complete_json(messages, purpose=task, **kwargs)

    def generate_text(
        self,
        messages: list[dict],
        task: str = "generic",
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
        **context,
    ) -> LLMResponse:
        """Free-form call for one task."""
        provider = self.provider_for(task)
        return provider.generate_text(
            messages, purpose=task, max_tokens=max_tokens, retries=retries, **context
        )

    # -- readiness -----------------------------------------------------------

    @property
    def has_key(self) -> bool:
        """True when the default task can actually be served.

        Named for the attribute the pipeline already checked before the
        multi-provider refactor.
        """
        try:
            provider = self.provider_for("topic_analysis")
        except (ProviderNotConfigured, LLMError):
            return False
        return provider.has_key

    def readiness_error(self) -> str:
        """A user-facing reason the LLM layer cannot run, or '' when it can."""
        try:
            provider = self.provider_for("topic_analysis")
        except ProviderNotConfigured as exc:
            return str(exc)
        except LLMError as exc:
            return str(exc)
        if not provider.has_key:
            return (
                f"{provider.config.display_name or provider.provider_id} "
                "尚未配置 API Key，请在「设置 → AI 模型」中填写。"
            )
        return ""

    def describe_routing(self) -> dict[str, Resolution]:
        """What each task would use right now - shown on the Settings page."""
        described: dict[str, Resolution] = {}
        for task in TASKS:
            try:
                described[task] = self.resolve(task)
            except (ProviderNotConfigured, LLMError):
                continue
        return described


def build_llm_service(
    usage_sink: Optional[Callable[[dict], None]] = None,
) -> LLMService:
    """Factory used by the pipeline so credential lookup stays in one place."""
    service = LLMService(usage_sink=usage_sink)
    service.load()
    return service


def test_provider_row(row, api_key: Optional[str] = None, model: str = "") -> dict:
    """Run a Test Connection against one stored provider row.

    The credential is read from the keyring unless one is supplied (used when
    testing a key the user just typed but has not saved yet).
    """
    from ..keyring_service import get_provider_key

    key = api_key if api_key is not None else get_provider_key(row.provider_id)
    provider = build_from_row(row, api_key=key, model_override=model)
    if not provider.config.model:
        return {"ok": False, "error": "尚未设置模型名称。", "latency_ms": 0}
    return provider.test_connection()
