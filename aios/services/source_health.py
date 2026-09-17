"""Per-source health, derived from real counters only.

Every number shown in the UI is a count of something that actually happened
during the run. There is no smoothing, no estimated availability and no
invented success percentage: if the denominator is zero the UI says so rather
than printing "100%".

The health *state* is the one derived value, and it is derived by the single
function :func:`derive_state` so the run detail page, the settings page and the
report banner can never disagree about what "degraded" means.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

from .collector import CollectorStatus


class HealthState:
    """How usable a source currently is."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    UNREACHABLE = "unreachable"
    CIRCUIT_OPEN = "circuit_open"
    DISABLED = "disabled"

    #: States that mean coverage this run was not what the user configured.
    IMPAIRED = {DEGRADED, RATE_LIMITED, UNREACHABLE, CIRCUIT_OPEN}
    #: States that mean the source produced nothing usable at all.
    BROKEN = {UNREACHABLE, CIRCUIT_OPEN}


HEALTH_LABELS = {
    HealthState.UNKNOWN: "未检测",
    HealthState.HEALTHY: "正常",
    HealthState.DEGRADED: "部分异常",
    HealthState.RATE_LIMITED: "受限",
    HealthState.UNREACHABLE: "不可用",
    HealthState.CIRCUIT_OPEN: "已暂停",
    HealthState.DISABLED: "已停用",
}

#: Badge classes reused from the shared status palette in ``aios.web``.
HEALTH_BADGES = {
    HealthState.UNKNOWN: "s-muted",
    HealthState.HEALTHY: "s-ok",
    HealthState.DEGRADED: "s-warn",
    HealthState.RATE_LIMITED: "s-warn",
    HealthState.UNREACHABLE: "s-err",
    HealthState.CIRCUIT_OPEN: "s-warn",
    HealthState.DISABLED: "s-muted",
}


@dataclass
class SourceCounters:
    """Raw tallies for one collector during one run."""

    collector: str = ""
    display_name: str = ""
    requests_attempted: int = 0
    requests_succeeded: int = 0
    zero_result_responses: int = 0
    candidates_returned: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    network_errors: int = 0
    parse_errors: int = 0
    http_errors: int = 0
    circuit_open_skips: int = 0
    circuit_activations: int = 0
    cache_hits: int = 0
    disabled: bool = False
    not_configured: bool = False
    last_error: str = ""
    total_latency_ms: int = 0

    @property
    def failures(self) -> int:
        return (
            self.timeouts
            + self.rate_limited
            + self.network_errors
            + self.parse_errors
            + self.http_errors
        )

    @property
    def success_rate(self) -> Optional[float]:
        """Real ratio, or None when nothing was attempted. Never fabricated."""
        if self.requests_attempted <= 0:
            return None
        return self.requests_succeeded / self.requests_attempted

    @property
    def avg_latency_ms(self) -> Optional[int]:
        if self.requests_succeeded <= 0:
            return None
        return int(self.total_latency_ms / self.requests_succeeded)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["state"] = derive_state(self)
        data["state_label"] = HEALTH_LABELS.get(data["state"], data["state"])
        data["failures"] = self.failures
        rate = self.success_rate
        data["success_rate"] = round(rate, 4) if rate is not None else None
        data["avg_latency_ms"] = self.avg_latency_ms
        return data


def derive_state(counters: SourceCounters) -> str:
    """The one place a health state is decided.

    Order matters. A source that answered nothing at all is ``unreachable``
    even if some of those failures were 429s; a source that answered some
    requests but was throttled on others is ``rate_limited``, which is a
    materially different message for the user.
    """
    if counters.disabled:
        return HealthState.DISABLED
    if counters.not_configured and counters.requests_attempted == 0:
        return HealthState.DISABLED
    if counters.requests_attempted == 0:
        if counters.circuit_open_skips:
            return HealthState.CIRCUIT_OPEN
        return HealthState.UNKNOWN
    if counters.requests_succeeded == 0:
        if counters.rate_limited and not (
            counters.timeouts or counters.network_errors or counters.parse_errors
        ):
            return HealthState.RATE_LIMITED
        return HealthState.UNREACHABLE
    if counters.circuit_open_skips or counters.circuit_activations:
        return HealthState.CIRCUIT_OPEN
    if counters.rate_limited:
        return HealthState.RATE_LIMITED
    if counters.failures:
        return HealthState.DEGRADED
    return HealthState.HEALTHY


class SourceHealthTracker:
    """Thread-safe accumulator of collector outcomes for one run."""

    def __init__(self, display_names: Optional[dict[str, str]] = None) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, SourceCounters] = {}
        self._names = dict(display_names or {})

    def register(self, collector_id: str, display_name: str = "", disabled: bool = False) -> None:
        """Make a collector visible in the report even if it is never called."""
        with self._lock:
            counters = self._ensure(collector_id)
            if display_name:
                counters.display_name = display_name
            counters.disabled = disabled

    def _ensure(self, collector_id: str) -> SourceCounters:
        counters = self._counters.get(collector_id)
        if counters is None:
            counters = SourceCounters(
                collector=collector_id,
                display_name=self._names.get(collector_id, collector_id),
            )
            self._counters[collector_id] = counters
        return counters

    def record(self, result) -> None:
        """Fold one :class:`~aios.services.collector.CollectorResult` in."""
        with self._lock:
            counters = self._ensure(result.collector)

            if result.from_cache:
                counters.cache_hits += 1
                counters.candidates_returned += len(result.candidates)
                return

            if result.status == CollectorStatus.DISABLED:
                counters.disabled = True
                return
            if result.status == CollectorStatus.NOT_CONFIGURED:
                counters.not_configured = True
                if result.error:
                    counters.last_error = result.error[:300]
                return
            if result.status == CollectorStatus.CIRCUIT_OPEN:
                counters.circuit_open_skips += 1
                if result.error:
                    counters.last_error = result.error[:300]
                return

            counters.requests_attempted += 1
            if result.status == CollectorStatus.OK:
                counters.requests_succeeded += 1
                counters.total_latency_ms += max(0, result.latency_ms)
                counters.candidates_returned += len(result.candidates)
                if not result.candidates:
                    counters.zero_result_responses += 1
                if result.error:
                    # A partially-failing RSS batch still succeeded overall.
                    counters.last_error = result.error[:300]
                return

            if result.status == CollectorStatus.TIMEOUT:
                counters.timeouts += 1
            elif result.status == CollectorStatus.RATE_LIMITED:
                counters.rate_limited += 1
            elif result.status == CollectorStatus.PARSE_ERROR:
                counters.parse_errors += 1
            elif result.status == CollectorStatus.HTTP_ERROR:
                counters.http_errors += 1
            else:
                counters.network_errors += 1
            if result.error:
                counters.last_error = result.error[:300]

    def note_circuit_open(self, collector_id: str) -> None:
        with self._lock:
            self._ensure(collector_id).circuit_activations += 1

    def snapshot(self) -> list[SourceCounters]:
        with self._lock:
            return [
                SourceCounters(**{k: v for k, v in asdict(c).items()})
                for c in self._counters.values()
            ]

    def as_dicts(self) -> list[dict]:
        return [c.as_dict() for c in self.snapshot()]

    def state_of(self, collector_id: str) -> str:
        with self._lock:
            counters = self._counters.get(collector_id)
        return derive_state(counters) if counters else HealthState.UNKNOWN

    # -- run-level judgements ------------------------------------------------

    def impaired_sources(self) -> list[SourceCounters]:
        """Sources whose coverage this run was not what was configured."""
        return [c for c in self.snapshot() if derive_state(c) in HealthState.IMPAIRED]

    def is_degraded(self) -> bool:
        """True when the run's source coverage cannot be called complete."""
        return bool(self.impaired_sources())

    def has_broken_source(self) -> bool:
        """True when a source we asked never once answered successfully.

        The failure class does not matter here: a collector that returned 429
        to every request produced exactly as much evidence as one that timed out
        on every request - none - and in both cases the run's coverage is not
        what the user configured. This is what separates
        ``completed_with_errors`` from ``completed``.

        A source that answered *some* requests is degraded, not broken: the
        report still carries a coverage banner, but the run itself did collect
        what it set out to.
        """
        for counters in self.snapshot():
            if counters.disabled:
                continue
            if counters.requests_attempted > 0 and counters.requests_succeeded == 0:
                return True
            if derive_state(counters) in HealthState.BROKEN:
                return True
        return False

    def summary_lines(self) -> list[str]:
        """Short ``Google News：连接失败`` style lines for the report banner."""
        lines: list[str] = []
        for counters in self.impaired_sources():
            state = derive_state(counters)
            name = counters.display_name or counters.collector
            detail = HEALTH_LABELS.get(state, state)
            if state == HealthState.RATE_LIMITED and counters.rate_limited:
                detail = f"部分请求受限（{counters.rate_limited} 次 429）"
            elif state == HealthState.UNREACHABLE:
                detail = "连接失败"
            elif state == HealthState.CIRCUIT_OPEN:
                detail = "连续失败后已暂停"
            lines.append(f"{name}：{detail}")
        return lines


@dataclass
class ProbeResult:
    """One connectivity probe (Settings → 连接诊断)."""

    target: str
    display_name: str
    status: str = CollectorStatus.OK
    latency_ms: int = 0
    detail: str = ""
    checked_at: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def reachable(self) -> bool:
        return self.status == CollectorStatus.OK

    @property
    def state(self) -> str:
        """Map a probe outcome onto the same health vocabulary as a run."""
        if self.status == CollectorStatus.OK:
            return HealthState.HEALTHY
        if self.status == CollectorStatus.RATE_LIMITED:
            return HealthState.RATE_LIMITED
        if self.status == CollectorStatus.DISABLED:
            return HealthState.DISABLED
        if self.status == CollectorStatus.NOT_CONFIGURED:
            return HealthState.DISABLED
        return HealthState.UNREACHABLE

    def as_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state
        data["state_label"] = HEALTH_LABELS.get(self.state, self.state)
        data["status_label"] = {
            CollectorStatus.OK: "可用",
            CollectorStatus.TIMEOUT: "超时",
            CollectorStatus.RATE_LIMITED: "限流",
            CollectorStatus.DISABLED: "已停用",
            CollectorStatus.NOT_CONFIGURED: "未配置",
        }.get(self.status, "不可用")
        return data
