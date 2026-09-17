"""Network mode, collector wiring and connectivity diagnostics.

The product reasons about *network availability*, not about how the user
obtained connectivity. There is no "VPN mode" here and nothing in the UI tells
anyone to get a VPN: three honest options ("自动 / 中国大陆网络 / 国际网络") plus
lightweight health checks are enough to stop the collector spending twenty-five
seconds per query proving that Google is unreachable.

This module is also the only place that turns stored settings into live
collector objects, so the pipeline never hard-codes which sources exist.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import USER_AGENT
from ..models import FeedSource
from ..timeutil import utcnow
from . import settings_service
from .collector import (
    NETWORK_CHINA,
    NETWORK_INTERNATIONAL,
    CollectorRegistry,
    CollectorStatus,
    FeedSpec,
    GDELTCollector,
    GoogleNewsCollector,
    RSSCollector,
    classify_exception,
    domain_of,
)
from .net_policy import (
    CONNECTION_CUSTOM,
    CONNECTION_DIRECT,
    CONNECTION_LABELS,
    CONNECTION_SYSTEM,
    CircuitBreaker,
    RateLimiter,
    TransportSettings,
    build_session,
    split_proxy_credentials,
    strip_credentials,
)
from .source_health import HealthState, ProbeResult

logger = logging.getLogger(__name__)


# --- network mode -----------------------------------------------------------

MODE_AUTO = "auto"
MODE_CHINA = "china"
MODE_INTERNATIONAL = "international"

NETWORK_MODES = (MODE_AUTO, MODE_CHINA, MODE_INTERNATIONAL)

MODE_LABELS = {
    MODE_AUTO: "自动（推荐）",
    MODE_CHINA: "中国大陆网络",
    MODE_INTERNATIONAL: "国际网络",
}

MODE_DESCRIPTIONS = {
    MODE_AUTO: "根据当前网络状况自动选择可用数据源。",
    MODE_CHINA: "优先使用大陆网络可访问的数据源。",
    MODE_INTERNATIONAL: "优先使用完整国际数据源。",
}

#: Keyring slot for a proxy password, so it never reaches SQLite.
PROXY_KEYRING_USERNAME = "proxy:credentials"

#: Collector id -> settings key controlling whether it runs at all.
COLLECTOR_TOGGLES = {
    "gdelt": "collector_gdelt_enabled",
    "google_news": "collector_google_news_enabled",
    "rss": "collector_rss_enabled",
}


def network_mode(session: Session) -> str:
    mode = settings_service.get_str(session, "network_mode", MODE_AUTO).strip().lower()
    return mode if mode in NETWORK_MODES else MODE_AUTO


def set_network_mode(session: Session, mode: str) -> str:
    chosen = (mode or "").strip().lower()
    if chosen not in NETWORK_MODES:
        raise ValueError(f"未知的网络模式：{mode}")
    settings_service.set_value(session, "network_mode", chosen)
    return chosen


def collector_enabled(session: Session, collector_id: str) -> bool:
    key = COLLECTOR_TOGGLES.get(collector_id)
    if key is None:
        return True
    return settings_service.get_bool(session, key, True)


def disabled_collectors(session: Session) -> set[str]:
    return {cid for cid in COLLECTOR_TOGGLES if not collector_enabled(session, cid)}


# --- transport --------------------------------------------------------------

def transport_settings(session: Session) -> TransportSettings:
    """Read the connection mode. The proxy password comes from the keyring."""
    mode = settings_service.get_str(session, "connection_mode", CONNECTION_SYSTEM).strip().lower()
    if mode not in (CONNECTION_SYSTEM, CONNECTION_DIRECT, CONNECTION_CUSTOM):
        mode = CONNECTION_SYSTEM

    username = settings_service.get_str(session, "proxy_username", "")
    password = ""
    if username:
        from .keyring_service import get_api_key

        try:
            password = get_api_key(username=PROXY_KEYRING_USERNAME) or ""
        except Exception:  # pragma: no cover - platform dependent
            password = ""

    return TransportSettings(
        mode=mode,
        http_proxy=settings_service.get_str(session, "http_proxy", ""),
        https_proxy=settings_service.get_str(session, "https_proxy", ""),
        proxy_username=username,
        proxy_password=password,
    )


def save_transport(
    session: Session, mode: str, http_proxy: str = "", https_proxy: str = ""
) -> TransportSettings:
    """Persist connection settings, splitting any credential into the keyring."""
    chosen = (mode or "").strip().lower()
    if chosen not in (CONNECTION_SYSTEM, CONNECTION_DIRECT, CONNECTION_CUSTOM):
        raise ValueError(f"未知的连接方式：{mode}")

    http_clean, http_user, http_password = split_proxy_credentials(http_proxy.strip())
    https_clean, https_user, https_password = split_proxy_credentials(https_proxy.strip())
    username = http_user or https_user
    password = http_password or https_password

    settings_service.set_many(
        session,
        {
            "connection_mode": chosen,
            "http_proxy": http_clean,
            "https_proxy": https_clean,
            "proxy_username": username,
        },
    )

    if password:
        from .keyring_service import set_api_key

        try:
            set_api_key(password, username=PROXY_KEYRING_USERNAME)
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("Could not store proxy password in the keyring: %s", type(exc).__name__)
    elif not username:
        from .keyring_service import delete_api_key

        try:
            delete_api_key(username=PROXY_KEYRING_USERNAME)
        except Exception:  # pragma: no cover
            pass

    return transport_settings(session)


# --- feeds ------------------------------------------------------------------

def list_feeds(session: Session, enabled_only: bool = False) -> list[FeedSource]:
    stmt = select(FeedSource)
    if enabled_only:
        stmt = stmt.where(FeedSource.enabled.is_(True))
    return list(session.scalars(stmt.order_by(FeedSource.id)))


def add_feed(
    session: Session, url: str, title: str = "", module_id: Optional[int] = None
) -> FeedSource:
    cleaned = (url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("订阅地址必须以 http:// 或 https:// 开头。")
    if "@" in cleaned.split("//", 1)[-1].split("/", 1)[0]:
        raise ValueError("订阅地址不应包含账号密码。")
    existing = session.scalars(select(FeedSource).where(FeedSource.url == cleaned)).first()
    if existing is not None:
        raise ValueError("该订阅源已存在。")

    row = FeedSource(
        url=cleaned,
        title=(title or "").strip()[:200],
        domain=domain_of(cleaned),
        module_id=module_id,
        enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def delete_feed(session: Session, feed_id: int) -> bool:
    row = session.get(FeedSource, feed_id)
    if row is None:
        return False
    session.delete(row)
    return True


def toggle_feed(session: Session, feed_id: int) -> Optional[FeedSource]:
    row = session.get(FeedSource, feed_id)
    if row is None:
        return None
    row.enabled = not row.enabled
    session.flush()
    return row


def feed_specs(session: Session) -> list[FeedSpec]:
    return [
        FeedSpec(url=row.url, title=row.title, domain=row.domain)
        for row in list_feeds(session, enabled_only=True)
    ]


# --- building the registry --------------------------------------------------

@dataclass
class CollectionSettings:
    """Everything the collection layer needs, read once per run."""

    network_mode: str = MODE_AUTO
    disabled: set[str] = None  # type: ignore[assignment]
    http_timeout: int = 20
    google_timeout: int = 12
    gdelt_min_interval: float = 2.0
    gdelt_max_concurrency: int = 1
    gdelt_failure_threshold: int = 5
    google_failure_threshold: int = 3
    circuit_reset_seconds: float = 300.0
    cache_ttl_minutes: int = 30
    min_candidates: int = 1
    health_check_timeout: int = 6
    transport: TransportSettings = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.disabled is None:
            self.disabled = set()
        if self.transport is None:
            self.transport = TransportSettings()


def collection_settings(session: Session) -> CollectionSettings:
    """Read every collection-tuning value in one place."""
    return CollectionSettings(
        network_mode=network_mode(session),
        disabled=disabled_collectors(session),
        http_timeout=settings_service.get_int(session, "http_timeout", 20),
        google_timeout=settings_service.get_int(session, "google_news_timeout", 12),
        gdelt_min_interval=settings_service.get_float(session, "gdelt_min_interval_seconds", 2.0),
        gdelt_max_concurrency=settings_service.get_int(session, "gdelt_max_concurrency", 1),
        gdelt_failure_threshold=settings_service.get_int(session, "gdelt_failure_threshold", 5),
        google_failure_threshold=settings_service.get_int(
            session, "google_news_failure_threshold", 3
        ),
        circuit_reset_seconds=float(
            settings_service.get_int(session, "collector_circuit_reset_seconds", 300)
        ),
        cache_ttl_minutes=settings_service.get_int(session, "collection_cache_ttl_minutes", 30),
        min_candidates=settings_service.get_int(session, "collection_min_candidates", 1),
        health_check_timeout=settings_service.get_int(session, "health_check_timeout", 6),
        transport=transport_settings(session),
    )


def build_registry(
    config: CollectionSettings,
    feeds: Optional[list[FeedSpec]] = None,
    session: Optional[requests.Session] = None,
) -> CollectorRegistry:
    """Instantiate the collectors this run will use.

    One registry per run: the limiters and breakers the collectors carry are
    what make a run polite, and sharing them across runs with different network
    modes would carry stale verdicts forward.
    """
    http = build_session(config.transport, session)

    gdelt = GDELTCollector(
        timeout=max(10, config.http_timeout),
        session=http,
        limiter=RateLimiter(
            min_interval=max(0.0, config.gdelt_min_interval),
            max_concurrency=max(1, config.gdelt_max_concurrency),
        ),
        breaker=CircuitBreaker(
            threshold=max(1, config.gdelt_failure_threshold),
            reset_after=config.circuit_reset_seconds,
            name="gdelt",
        ),
    )
    google = GoogleNewsCollector(
        # A shorter timeout than the rest: in an unreachable network this is
        # the difference between a 12-second probe and a 25-second one, and the
        # circuit breaker means we only pay it a handful of times.
        timeout=max(5, config.google_timeout),
        session=http,
        breaker=CircuitBreaker(
            threshold=max(1, config.google_failure_threshold),
            reset_after=config.circuit_reset_seconds,
            name="google_news",
        ),
    )
    rss = RSSCollector(
        timeout=max(5, config.http_timeout),
        session=http,
        feeds=feeds or [],
        breaker=CircuitBreaker(
            threshold=8, reset_after=config.circuit_reset_seconds, name="rss"
        ),
    )

    return CollectorRegistry(
        collectors=[gdelt, google, rss],
        network_mode=config.network_mode,
        disabled=set(config.disabled),
    )


def apply_mode_policy(
    registry: CollectorRegistry, config: CollectionSettings, probes: Optional[list[ProbeResult]] = None
) -> list[str]:
    """Decide which sources this run may use, and say why in plain language.

    * **中国大陆网络** - Google News is not assumed reachable. It is not made
      mandatory and it is not silently disabled either: it stays available as a
      best-effort fallback at the end of the chain, and its own circuit breaker
      removes it after a couple of failures rather than after fifty.
    * **国际网络** - the full set is available. That still does not mean every
      query goes to every source; staged collection continues to apply.
    * **自动** - the probes decide. An unreachable source is skipped for this
      run instead of being discovered the expensive way, once per query.
    """
    notes: list[str] = []

    if config.network_mode == MODE_CHINA:
        notes.append("中国大陆网络模式：优先使用可直连的数据源，Google News 仅作尽力而为的兜底。")
    elif config.network_mode == MODE_INTERNATIONAL:
        notes.append("国际网络模式：可使用完整数据源集合。")

    for collector_id in sorted(registry.disabled):
        collector = registry.by_id(collector_id)
        name = collector.info.display_name if collector else collector_id
        notes.append(f"{name}：已在设置中停用，本次跳过。")

    if not probes:
        return notes

    for probe in probes:
        collector = registry.by_id(probe.target)
        if collector is None:
            continue
        if probe.state == HealthState.UNREACHABLE:
            # Pre-open the circuit: the probe already proved the point, and
            # paying the timeout again per query proves nothing new.
            collector.breaker.trip()
            notes.append(
                f"{collector.info.display_name}：连接检测失败（{probe.detail or '不可达'}），"
                "本次优先使用其他数据源。"
            )
        elif probe.state == HealthState.RATE_LIMITED:
            collector.limiter.pause_for(30.0)
            notes.append(
                f"{collector.info.display_name}：检测到限流，本次将放慢请求节奏。"
            )
    return notes


# --- connectivity diagnostics ----------------------------------------------

def _probe(
    name: str,
    display_name: str,
    url: str,
    http: requests.Session,
    timeout: float,
    method: str = "HEAD",
    params: Optional[dict] = None,
) -> ProbeResult:
    """One lightweight reachability check. Never a full search."""
    started = time.monotonic()
    try:
        response = http.request(
            method,
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:
        status, message = classify_exception(exc)
        return ProbeResult(
            target=name,
            display_name=display_name,
            status=status,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=message,
            checked_at=utcnow().isoformat(timespec="seconds"),
        )

    latency = int((time.monotonic() - started) * 1000)
    if response.status_code == 429:
        return ProbeResult(
            target=name, display_name=display_name, status=CollectorStatus.RATE_LIMITED,
            latency_ms=latency, detail="HTTP 429",
            checked_at=utcnow().isoformat(timespec="seconds"),
        )
    if response.status_code >= 400:
        return ProbeResult(
            target=name, display_name=display_name, status=CollectorStatus.HTTP_ERROR,
            latency_ms=latency, detail=f"HTTP {response.status_code}",
            checked_at=utcnow().isoformat(timespec="seconds"),
        )
    return ProbeResult(
        target=name, display_name=display_name, status=CollectorStatus.OK,
        latency_ms=latency, detail="",
        checked_at=utcnow().isoformat(timespec="seconds"),
    )


def probe_sources(
    session: Session,
    config: Optional[CollectionSettings] = None,
    http: Optional[requests.Session] = None,
) -> list[ProbeResult]:
    """Check each enabled collector's reachability. Cheap by design."""
    config = config or collection_settings(session)
    client = http or build_session(config.transport)
    timeout = max(2, config.health_check_timeout)
    results: list[ProbeResult] = []

    for collector_id, display_name in (
        ("google_news", "Google News"),
        ("gdelt", "GDELT"),
    ):
        if collector_id in config.disabled:
            results.append(
                ProbeResult(
                    target=collector_id, display_name=display_name,
                    status=CollectorStatus.DISABLED, detail="已在设置中停用",
                    checked_at=utcnow().isoformat(timespec="seconds"),
                )
            )
            continue
        if collector_id == "google_news":
            results.append(
                _probe(collector_id, display_name, "https://news.google.com/rss",
                       client, timeout, method="HEAD")
            )
        else:
            # GDELT has no HEAD endpoint worth trusting; one record is the
            # smallest honest reachability signal, and a 429 here is itself the
            # answer we are looking for.
            results.append(
                _probe(
                    collector_id, display_name,
                    "https://api.gdeltproject.org/api/v2/doc/doc", client, timeout,
                    method="GET",
                    params={"query": "operating system", "mode": "ArtList",
                            "maxrecords": 1, "format": "json"},
                )
            )

    if "rss" in config.disabled:
        results.append(
            ProbeResult(
                target="rss", display_name="RSS / Atom", status=CollectorStatus.DISABLED,
                detail="已在设置中停用", checked_at=utcnow().isoformat(timespec="seconds"),
            )
        )
    else:
        results.append(_probe_feeds(session, client, timeout))

    return results


def _probe_feeds(session: Session, http: requests.Session, timeout: float) -> ProbeResult:
    feeds = list_feeds(session, enabled_only=True)
    if not feeds:
        return ProbeResult(
            target="rss", display_name="RSS / Atom",
            status=CollectorStatus.NOT_CONFIGURED, detail="尚未添加订阅源",
            checked_at=utcnow().isoformat(timespec="seconds"),
        )

    reachable = 0
    latencies: list[int] = []
    last_detail = ""
    for feed in feeds[:8]:
        probe = _probe("rss", "RSS / Atom", feed.url, http, timeout, method="GET")
        feed.last_checked_at = utcnow()
        feed.last_status = probe.status
        if probe.reachable:
            reachable += 1
            latencies.append(probe.latency_ms)
        else:
            last_detail = f"{feed.title or feed.domain or feed.url}: {probe.detail}"
    session.flush()

    checked = min(len(feeds), 8)
    if reachable == 0:
        status = CollectorStatus.NETWORK_ERROR
    elif reachable < checked:
        status = CollectorStatus.OK
    else:
        status = CollectorStatus.OK

    return ProbeResult(
        target="rss", display_name="RSS / Atom", status=status,
        latency_ms=int(sum(latencies) / len(latencies)) if latencies else 0,
        detail=(f"{reachable}/{checked} 个订阅源可用" + (f" · {last_detail}" if last_detail else "")),
        checked_at=utcnow().isoformat(timespec="seconds"),
        extra={"reachable": reachable, "checked": checked},
    )


def probe_ai_model(session: Session) -> ProbeResult:
    """Round-trip the configured model. Credentials never leave the keyring."""
    from ..repositories import providers as providers_repo
    from .llm.service import test_provider_row

    row = providers_repo.default_provider(session)
    if row is None:
        return ProbeResult(
            target="ai", display_name="AI 模型", status=CollectorStatus.NOT_CONFIGURED,
            detail="尚未配置 AI 模型", checked_at=utcnow().isoformat(timespec="seconds"),
        )

    try:
        outcome = test_provider_row(row)
    except Exception as exc:  # pragma: no cover - defensive
        from .keyring_service import redact

        return ProbeResult(
            target="ai", display_name="AI 模型", status=CollectorStatus.NETWORK_ERROR,
            detail=redact(str(exc))[:200], checked_at=utcnow().isoformat(timespec="seconds"),
        )

    if outcome.get("ok"):
        return ProbeResult(
            target="ai", display_name="AI 模型", status=CollectorStatus.OK,
            latency_ms=int(outcome.get("latency_ms") or 0),
            detail=str(outcome.get("model") or row.default_model or ""),
            checked_at=utcnow().isoformat(timespec="seconds"),
        )

    from .keyring_service import redact

    return ProbeResult(
        target="ai", display_name="AI 模型", status=CollectorStatus.HTTP_ERROR,
        latency_ms=int(outcome.get("latency_ms") or 0),
        detail=redact(str(outcome.get("error") or ""))[:200],
        checked_at=utcnow().isoformat(timespec="seconds"),
    )


def run_diagnostics(session: Session, include_ai: bool = True) -> list[ProbeResult]:
    """Everything the 连接诊断 button runs. Output carries no secrets."""
    results: list[ProbeResult] = []
    if include_ai:
        results.append(probe_ai_model(session))
    results.extend(probe_sources(session))
    return results


# --- UI helpers -------------------------------------------------------------

def describe_modes() -> list[dict]:
    return [
        {"id": mode, "label": MODE_LABELS[mode], "description": MODE_DESCRIPTIONS[mode]}
        for mode in NETWORK_MODES
    ]


def describe_connection_modes() -> list[dict]:
    return [
        {"id": key, "label": label}
        for key, label in CONNECTION_LABELS.items()
    ]


def collector_overview(session: Session) -> list[dict]:
    """Per-collector rows for Settings → 网络与数据源."""
    config = collection_settings(session)
    registry = build_registry(config, feeds=feed_specs(session))
    feeds = list_feeds(session)

    rows: list[dict] = []
    for collector in registry.collectors:
        info = collector.info
        enabled = collector.name not in registry.disabled
        row = {
            "collector_id": info.collector_id,
            "display_name": info.display_name,
            "description": info.description,
            "network_compatibility": info.network_compatibility,
            "requires_api_key": info.requires_api_key,
            "supports_search": info.supports_search,
            "supports_rss": info.supports_rss,
            "rate_limit_policy": info.rate_limit_policy,
            "enabled": enabled,
            "toggle_key": COLLECTOR_TOGGLES.get(info.collector_id, ""),
        }
        if info.collector_id == "rss":
            row["feed_count"] = len([f for f in feeds if f.enabled])
            checked = [f.last_checked_at for f in feeds if f.last_checked_at]
            row["last_checked_at"] = max(checked) if checked else None
        rows.append(row)
    return rows


def network_context(session: Session) -> dict:
    """Everything the Settings network section needs, in one call."""
    config = collection_settings(session)
    transport = config.transport
    return {
        "network_mode": config.network_mode,
        "network_modes": describe_modes(),
        "connection_mode": transport.mode,
        "connection_modes": describe_connection_modes(),
        "connection_description": transport.describe(),
        "http_proxy": strip_credentials(transport.http_proxy),
        "https_proxy": strip_credentials(transport.https_proxy),
        "proxy_username": transport.proxy_username,
        "collectors": collector_overview(session),
        "feeds": list_feeds(session),
        "cache_ttl_minutes": config.cache_ttl_minutes,
        "gdelt_min_interval": config.gdelt_min_interval,
    }


def network_compatibility_label(value: str) -> str:
    return {
        NETWORK_INTERNATIONAL: "需要国际网络",
        NETWORK_CHINA: "大陆网络可用",
    }.get(value, "任意网络")
