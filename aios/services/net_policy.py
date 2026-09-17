"""Politeness and resilience primitives for outbound collection.

Three ideas, all of them about *not* abusing public services:

* :class:`RateLimiter` - one global pacer per source, shared by every worker
  thread in a run. GDELT does not care that our fan-out is per topic; it sees
  one client.
* :class:`CircuitBreaker` - after a source has failed repeatedly there is
  nothing to learn from failing again. Twenty-five seconds per Google News
  timeout times fifty queries is twenty minutes spent proving one fact.
* :func:`build_session` - how requests leave this machine (system settings,
  forced direct, or an explicit proxy). Proxy passwords never touch SQLite.

Everything here is deliberately synchronous and thread-safe: the pipeline runs
in worker threads, not an event loop.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)


# --- rate limiting ----------------------------------------------------------

class RateLimiter:
    """Global pacer: at most ``max_concurrency`` in flight, spaced by ``min_interval``.

    Used as a context manager::

        with limiter.slot():
            response = session.get(...)

    The spacing is enforced across *all* callers, which is the whole point: a
    per-topic limiter would still let eight topics fire at once.
    """

    def __init__(self, min_interval: float = 0.0, max_concurrency: int = 1) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        #: Set by a 429 handler so every other thread waits too.
        self._paused_until = 0.0

    def pause_for(self, seconds: float) -> None:
        """Hold every caller back for ``seconds`` (used for Retry-After)."""
        if seconds <= 0:
            return
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def _wait_turn(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                target = max(self._next_allowed, self._paused_until)
                if now >= target:
                    self._next_allowed = now + self.min_interval
                    return
                delay = target - now
            time.sleep(min(delay, 5.0))

    def slot(self) -> "_RateLimitSlot":
        return _RateLimitSlot(self)


class _RateLimitSlot:
    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    def __enter__(self) -> "_RateLimitSlot":
        self._limiter._semaphore.acquire()
        try:
            self._limiter._wait_turn()
        except BaseException:
            self._limiter._semaphore.release()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._limiter._semaphore.release()


# --- circuit breaking -------------------------------------------------------

@dataclass
class CircuitState:
    """Snapshot of one breaker, for display and for tests."""

    open: bool
    consecutive_failures: int
    opened_at: Optional[float]
    reopen_count: int
    remaining_seconds: float


class CircuitBreaker:
    """Stops calling a source that has failed ``threshold`` times in a row.

    Recovery is time-based: after ``reset_after`` seconds the breaker allows one
    probe through. A success closes it; a failure re-opens it immediately. There
    is no exponential widening - a daily tool does not need one, and a fixed
    window is far easier to explain in the UI.
    """

    def __init__(self, threshold: int = 5, reset_after: float = 300.0, name: str = "") -> None:
        self.threshold = max(1, int(threshold))
        self.reset_after = max(1.0, float(reset_after))
        self.name = name
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._reopen_count = 0

    @property
    def is_open(self) -> bool:
        """True when calls should be refused right now."""
        with self._lock:
            return self._is_open_locked()

    def _is_open_locked(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            # Half-open: let the next call through as a probe.
            self._opened_at = None
            self._failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> bool:
        """Count a failure. Returns True when this failure opened the circuit."""
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                self._reopen_count += 1
                logger.info(
                    "Circuit opened for %s after %s consecutive failures",
                    self.name or "collector", self._failures,
                )
                return True
            return False

    def trip(self) -> None:
        """Open the circuit immediately (a known-fatal condition)."""
        with self._lock:
            if self._opened_at is None:
                self._opened_at = time.monotonic()
                self._reopen_count += 1
            self._failures = max(self._failures, self.threshold)

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def state(self) -> CircuitState:
        with self._lock:
            open_now = self._opened_at is not None and (
                time.monotonic() - self._opened_at < self.reset_after
            )
            remaining = 0.0
            if open_now and self._opened_at is not None:
                remaining = max(0.0, self.reset_after - (time.monotonic() - self._opened_at))
            return CircuitState(
                open=open_now,
                consecutive_failures=self._failures,
                opened_at=self._opened_at,
                reopen_count=self._reopen_count,
                remaining_seconds=remaining,
            )


# --- backoff ----------------------------------------------------------------

def backoff_delay(attempt: int, base: float = 2.0, cap: float = 60.0, jitter: float = 0.3) -> float:
    """Bounded exponential backoff with jitter.

    Jitter matters even for a single-user tool: without it, three queries that
    hit 429 at the same moment retry at the same moment.
    """
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    spread = raw * jitter
    # Clamped after jitter too: "bounded" has to mean bounded, otherwise a
    # jittered 60-second cap can still wait 78 seconds.
    return max(0.0, min(cap, raw + random.uniform(-spread, spread)))


def parse_retry_after(value: Optional[str], cap: float = 120.0) -> Optional[float]:
    """Seconds to wait from a ``Retry-After`` header, or None when unusable.

    Only the delta-seconds form is honoured; the HTTP-date form is rare here and
    an unparsed header should fall back to our own backoff rather than to zero.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            import email.utils
            import datetime as dt

            parsed = email.utils.parsedate_to_datetime(text)
            if parsed is None:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            seconds = (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()
        except Exception:
            return None
    if seconds < 0:
        return None
    return min(seconds, cap)


# --- transport --------------------------------------------------------------

CONNECTION_SYSTEM = "system"
CONNECTION_DIRECT = "direct"
CONNECTION_CUSTOM = "custom"

CONNECTION_LABELS = {
    CONNECTION_SYSTEM: "跟随系统设置",
    CONNECTION_DIRECT: "直连",
    CONNECTION_CUSTOM: "自定义代理",
}


@dataclass
class TransportSettings:
    """How outbound requests should be routed."""

    mode: str = CONNECTION_SYSTEM
    http_proxy: str = ""
    https_proxy: str = ""
    #: Supplied separately (from the keyring), never persisted in SQLite.
    proxy_username: str = ""
    proxy_password: str = field(default="", repr=False)

    def proxies(self) -> Optional[dict[str, str]]:
        """A ``requests`` proxies mapping, or None to use the default."""
        if self.mode == CONNECTION_DIRECT:
            return {"http": "", "https": ""}
        if self.mode != CONNECTION_CUSTOM:
            return None
        mapping: dict[str, str] = {}
        if self.http_proxy:
            mapping["http"] = _with_credentials(
                self.http_proxy, self.proxy_username, self.proxy_password
            )
        if self.https_proxy:
            mapping["https"] = _with_credentials(
                self.https_proxy, self.proxy_username, self.proxy_password
            )
        return mapping or None

    def describe(self) -> str:
        """Human-readable, credential-free description."""
        if self.mode == CONNECTION_DIRECT:
            return "直连（忽略系统代理）"
        if self.mode == CONNECTION_CUSTOM:
            targets = ", ".join(
                strip_credentials(p) for p in (self.http_proxy, self.https_proxy) if p
            )
            return f"自定义代理：{targets or '未填写'}"
        return "跟随系统设置"


def _with_credentials(url: str, username: str, password: str) -> str:
    """Insert proxy credentials at call time. They are never stored in the URL."""
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return url
    userinfo = quote(username, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    netloc = f"{userinfo}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def strip_credentials(url: str) -> str:
    """``http://user:pw@host:8080`` -> ``http://host:8080`` for display and logs."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[invalid proxy url]"
    if not parts.hostname:
        return url
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def split_proxy_credentials(url: str) -> tuple[str, str, str]:
    """Split ``scheme://user:pw@host`` into (clean url, username, password).

    Lets the settings form accept a pasted proxy URL while keeping the password
    out of the database: the caller stores the password in the OS keyring.
    """
    if not url:
        return "", "", ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, "", ""
    username = parts.username or ""
    password = parts.password or ""
    return strip_credentials(url), username, password


def build_session(
    settings: Optional[TransportSettings] = None,
    session: Optional[requests.Session] = None,
) -> requests.Session:
    """A ``requests`` session configured for the chosen connection mode."""
    http = session or requests.Session()
    settings = settings or TransportSettings()
    proxies = settings.proxies()
    if settings.mode == CONNECTION_DIRECT:
        http.trust_env = False
        http.proxies = {}
    elif proxies is not None:
        http.proxies = proxies
    return http
