"""Source collectors.

Migrated from the original ``collect_gdelt`` / ``collect_google_news`` helpers
and put behind a small interface so new sources (vendor newsrooms, RSS feeds)
can be added without touching the pipeline or the UI.

The central rule of this module: **a request that succeeded and found nothing is
not the same thing as a request that failed.** Every collector returns a
:class:`CollectorResult` carrying an explicit :class:`CollectorStatus`, and the
pipeline is required to tell those apart. Collapsing them is how a run in which
every source timed out ends up reported as "no news today".

Politeness lives here too: a global :class:`~aios.services.net_policy.RateLimiter`
per source, ``Retry-After``-aware backoff, and a circuit breaker so a source
that is unreachable is asked once, not fifty times.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests

from ..config import USER_AGENT
from ..timeutil import utcnow
from .net_policy import CircuitBreaker, RateLimiter, backoff_delay, parse_retry_after

logger = logging.getLogger(__name__)


# --- result semantics -------------------------------------------------------

class CollectorStatus:
    """Why a collector call ended the way it did.

    ``OK`` means the transport and the parse both succeeded. It says nothing
    about how many candidates came back - zero results from a healthy source is
    ``OK`` with an empty list, and that distinction is the entire point.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    SSL_ERROR = "ssl_error"
    PROXY_ERROR = "proxy_error"
    PARSE_ERROR = "parse_error"
    HTTP_ERROR = "http_error"
    CIRCUIT_OPEN = "circuit_open"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"

    #: Everything that means "we did not get an answer we can trust".
    FAILURES = {
        TIMEOUT, RATE_LIMITED, NETWORK_ERROR, SSL_ERROR,
        PROXY_ERROR, PARSE_ERROR, HTTP_ERROR, CIRCUIT_OPEN,
    }
    #: Not a failure of the source - we chose not to ask.
    SKIPPED = {DISABLED, NOT_CONFIGURED}


STATUS_LABELS = {
    CollectorStatus.OK: "正常",
    CollectorStatus.TIMEOUT: "超时",
    CollectorStatus.RATE_LIMITED: "受限",
    CollectorStatus.NETWORK_ERROR: "连接失败",
    CollectorStatus.SSL_ERROR: "证书错误",
    CollectorStatus.PROXY_ERROR: "代理错误",
    CollectorStatus.PARSE_ERROR: "响应无法解析",
    CollectorStatus.HTTP_ERROR: "服务端错误",
    CollectorStatus.CIRCUIT_OPEN: "已暂停",
    CollectorStatus.DISABLED: "已停用",
    CollectorStatus.NOT_CONFIGURED: "未配置",
}


@dataclass
class Candidate:
    """A search hit before any body extraction or persistence."""

    title: str
    url: str
    domain: str = ""
    source: str = ""
    snippet: str = ""
    published_at: Optional[dt.datetime] = None
    published_raw: str = ""
    collector: str = ""
    body_text: str = ""
    language: str = ""
    trust_score: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.domain and self.url:
            self.domain = domain_of(self.url)
        if not self.source:
            self.source = self.domain


@dataclass
class CollectorResult:
    """One collector's answer to one query."""

    collector: str
    status: str = CollectorStatus.OK
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0
    attempts: int = 1
    #: True when the result came from the short-term cache, not the network.
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        """The source answered and we understood the answer."""
        return self.status == CollectorStatus.OK

    @property
    def failed(self) -> bool:
        return self.status in CollectorStatus.FAILURES

    @property
    def skipped(self) -> bool:
        return self.status in CollectorStatus.SKIPPED

    @property
    def zero_results(self) -> bool:
        """A healthy source that genuinely found nothing."""
        return self.ok and not self.candidates

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


# --- helpers ----------------------------------------------------------------

def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    if not value:
        return ""
    try:
        from bs4 import BeautifulSoup

        return " ".join(BeautifulSoup(value, "html.parser").stripped_strings)
    except Exception:
        return re.sub(r"<[^>]+>", " ", value)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _parse_gdelt_date(value: str) -> Optional[dt.datetime]:
    """GDELT uses ``20260915T061500Z``."""
    if not value:
        return None
    cleaned = value.strip().replace("T", "").replace("Z", "")
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _parse_rfc822(value: str) -> Optional[dt.datetime]:
    """RSS ``pubDate`` -> naive UTC."""
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_iso8601(value: str) -> Optional[dt.datetime]:
    """Atom ``updated``/``published`` -> naive UTC."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map a transport exception onto (status, short message).

    Keeps the taxonomy in one place so ``requests`` internals do not leak into
    the pipeline, the health model or the UI.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        return CollectorStatus.PROXY_ERROR, "代理连接失败"
    if isinstance(exc, requests.exceptions.SSLError):
        return CollectorStatus.SSL_ERROR, "TLS/SSL 握手失败"
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return CollectorStatus.TIMEOUT, "连接超时"
    if isinstance(exc, requests.exceptions.Timeout):
        return CollectorStatus.TIMEOUT, "连接超时"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return CollectorStatus.NETWORK_ERROR, "无法建立连接"
    if isinstance(exc, ET.ParseError):
        return CollectorStatus.PARSE_ERROR, "响应不是合法 XML"
    if isinstance(exc, ValueError):
        # json.JSONDecodeError subclasses ValueError: "Expecting value: line 1
        # column 1" means we were served HTML (an error page or a captcha
        # interstitial), which is emphatically not "no results".
        return CollectorStatus.PARSE_ERROR, "响应不是合法 JSON"
    return CollectorStatus.NETWORK_ERROR, type(exc).__name__


def _describe_body(text: str, limit: int = 120) -> str:
    """A short, safe fingerprint of an unexpected response body.

    Never log the whole thing: an unexpected body is usually a full HTML error
    page, and dumping it into the run log buries every other line.
    """
    snippet = re.sub(r"\s+", " ", (text or "")[:400]).strip()
    if not snippet:
        return "空响应"
    lowered = snippet.lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return f"返回了 HTML 页面（{len(text or '')} 字节）"
    return snippet[:limit]


# --- collector base ---------------------------------------------------------

#: Which network environments a source is normally reachable from.
NETWORK_ANY = "any"
NETWORK_INTERNATIONAL = "international"
NETWORK_CHINA = "china"


@dataclass(frozen=True)
class CollectorInfo:
    """Static metadata the planner and the Settings UI read.

    Kept as data rather than as ``if collector.name == "gdelt"`` branches spread
    through the pipeline: adding a source should not mean editing the pipeline.
    """

    collector_id: str
    display_name: str
    network_compatibility: str = NETWORK_ANY
    requires_api_key: bool = False
    supports_search: bool = True
    supports_rss: bool = False
    rate_limit_policy: str = ""
    description: str = ""


class SourceCollector(ABC):
    """Interface every collector implements."""

    name: str = "collector"
    info: CollectorInfo = CollectorInfo(collector_id="collector", display_name="Collector")

    #: Per-source pacing and failure handling. Instances share these through the
    #: registry so one run has exactly one limiter and one breaker per source.
    def __init__(
        self,
        timeout: int = 25,
        session: Optional[requests.Session] = None,
        limiter: Optional[RateLimiter] = None,
        breaker: Optional[CircuitBreaker] = None,
        max_attempts: int = 2,
    ) -> None:
        self.timeout = timeout
        self._http = session or requests.Session()
        self.limiter = limiter or RateLimiter()
        self.breaker = breaker or CircuitBreaker(name=self.name)
        self.max_attempts = max(1, int(max_attempts))

    # -- what the pipeline calls --------------------------------------------

    def fetch(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int = 20
    ) -> CollectorResult:
        """Run one query, returning an explicit status alongside the hits."""
        if self.breaker.is_open:
            state = self.breaker.state()
            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.CIRCUIT_OPEN,
                error=f"该数据源连续失败，已暂停 {int(state.remaining_seconds)} 秒",
                attempts=0,
            )

        started = time.monotonic()
        result = self._fetch(query, start, end, limit)
        result.latency_ms = int((time.monotonic() - started) * 1000)

        if result.ok:
            self.breaker.record_success()
        elif result.failed:
            if self.breaker.record_failure():
                result.error = (result.error + " · 已暂停该数据源").strip(" ·")
        return result

    def search(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int = 20
    ) -> list[Candidate]:
        """Backwards-compatible shape: just the candidates."""
        return self.fetch(query, start, end, limit).candidates

    @abstractmethod
    def _fetch(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int
    ) -> CollectorResult:
        """Perform the request. Must not raise; return a failure status instead."""

    # -- transport helpers ---------------------------------------------------

    def _get(self, url: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", USER_AGENT)
        timeout = kwargs.pop("timeout", self.timeout)
        return self._http.get(url, headers=headers, timeout=timeout, **kwargs)

    def _paced_get(self, url: str, **kwargs):
        with self.limiter.slot():
            return self._get(url, **kwargs)


# --- GDELT ------------------------------------------------------------------

class GDELTCollector(SourceCollector):
    """GDELT DOC 2.0 article search - no API key required.

    GDELT rate-limits aggressively and answers 429 with an HTML body, so this
    collector paces every request through a shared limiter, honours
    ``Retry-After``, and treats an unparseable body as ``parse_error`` rather
    than as an empty result.
    """

    name = "gdelt"
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
    info = CollectorInfo(
        collector_id="gdelt",
        display_name="GDELT",
        network_compatibility=NETWORK_ANY,
        requires_api_key=False,
        supports_search=True,
        rate_limit_policy="全局串行，默认最小间隔 2 秒",
        description="全球新闻索引，无需 API Key，限流较严格。",
    )

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("limiter", RateLimiter(min_interval=2.0, max_concurrency=1))
        kwargs.setdefault("breaker", CircuitBreaker(threshold=5, reset_after=300.0, name="gdelt"))
        kwargs.setdefault("max_attempts", 3)
        super().__init__(*args, **kwargs)

    def _fetch(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int
    ) -> CollectorResult:
        params = {
            "query": query,
            "mode": "ArtList",
            "maxrecords": min(max(limit, 1), 250),
            "format": "json",
            "sort": "DateDesc",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }

        last: CollectorResult = CollectorResult(collector=self.name)
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._paced_get(self.endpoint, params=params)
            except Exception as exc:
                status, message = classify_exception(exc)
                last = CollectorResult(
                    collector=self.name, status=status, error=message, attempts=attempt
                )
                if attempt < self.max_attempts:
                    time.sleep(backoff_delay(attempt))
                    continue
                return last

            if response.status_code == 429:
                wait = parse_retry_after(response.headers.get("Retry-After"))
                delay = wait if wait is not None else backoff_delay(attempt, base=5.0)
                # Hold every other worker back too, not just this one.
                self.limiter.pause_for(delay)
                last = CollectorResult(
                    collector=self.name,
                    status=CollectorStatus.RATE_LIMITED,
                    error=(
                        f"HTTP 429，Retry-After {int(wait)} 秒"
                        if wait is not None
                        else "HTTP 429 Too Many Requests"
                    ),
                    attempts=attempt,
                )
                if attempt < self.max_attempts:
                    time.sleep(delay)
                    continue
                return last

            if response.status_code >= 400:
                return CollectorResult(
                    collector=self.name,
                    status=CollectorStatus.HTTP_ERROR,
                    error=f"HTTP {response.status_code}",
                    attempts=attempt,
                )

            try:
                payload = response.json()
            except Exception as exc:
                status, message = classify_exception(exc)
                body = _describe_body(getattr(response, "text", ""))
                return CollectorResult(
                    collector=self.name,
                    status=status,
                    error=f"{message}：{body}",
                    attempts=attempt,
                )

            if not isinstance(payload, dict):
                return CollectorResult(
                    collector=self.name,
                    status=CollectorStatus.PARSE_ERROR,
                    error="响应 JSON 不是对象",
                    attempts=attempt,
                )

            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.OK,
                candidates=self._to_candidates(payload),
                attempts=attempt,
            )

        return last

    def _to_candidates(self, payload: dict) -> list[Candidate]:
        results: list[Candidate] = []
        for article in payload.get("articles", []) or []:
            url = article.get("url") or ""
            if not url:
                continue
            raw_date = article.get("seendate", "")
            results.append(
                Candidate(
                    title=normalize_space(article.get("title", "")),
                    url=url,
                    domain=(article.get("domain") or domain_of(url)).lower(),
                    source=article.get("domain") or domain_of(url),
                    published_at=_parse_gdelt_date(raw_date),
                    published_raw=raw_date,
                    language=(article.get("language") or "").lower()[:16],
                    collector=self.name,
                )
            )
        return results


# --- Google News ------------------------------------------------------------

class GoogleNewsCollector(SourceCollector):
    """Google News RSS.

    Unreachable from mainland China without extra connectivity, where every
    request burns the full connect timeout. The circuit breaker here is
    deliberately impatient: three timeouts are enough evidence, and fifty more
    would cost twenty minutes to learn nothing.
    """

    name = "google_news"
    info = CollectorInfo(
        collector_id="google_news",
        display_name="Google News",
        network_compatibility=NETWORK_INTERNATIONAL,
        requires_api_key=False,
        supports_search=True,
        supports_rss=True,
        rate_limit_policy="默认最小间隔 0.5 秒",
        description="Google News RSS 检索，需要可访问 Google 的网络环境。",
    )

    def __init__(self, *args, locale: str = "zh-CN", **kwargs) -> None:
        kwargs.setdefault("limiter", RateLimiter(min_interval=0.5, max_concurrency=3))
        kwargs.setdefault(
            "breaker", CircuitBreaker(threshold=3, reset_after=600.0, name="google_news")
        )
        kwargs.setdefault("max_attempts", 2)
        super().__init__(*args, **kwargs)
        self.locale = locale

    def _fetch(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int
    ) -> CollectorResult:
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )

        last: CollectorResult = CollectorResult(collector=self.name)
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._paced_get(url)
            except Exception as exc:
                status, message = classify_exception(exc)
                last = CollectorResult(
                    collector=self.name, status=status, error=message, attempts=attempt
                )
                # A timeout is not worth a second 25-second wait; a transient
                # connection reset is.
                if attempt < self.max_attempts and status != CollectorStatus.TIMEOUT:
                    time.sleep(backoff_delay(attempt, base=1.5, cap=8.0))
                    continue
                return last

            if response.status_code == 429:
                wait = parse_retry_after(response.headers.get("Retry-After"))
                if wait is not None:
                    self.limiter.pause_for(wait)
                return CollectorResult(
                    collector=self.name,
                    status=CollectorStatus.RATE_LIMITED,
                    error="HTTP 429 Too Many Requests",
                    attempts=attempt,
                )
            if response.status_code >= 400:
                return CollectorResult(
                    collector=self.name,
                    status=CollectorStatus.HTTP_ERROR,
                    error=f"HTTP {response.status_code}",
                    attempts=attempt,
                )

            try:
                root = ET.fromstring(response.content)
            except Exception as exc:
                status, message = classify_exception(exc)
                return CollectorResult(
                    collector=self.name,
                    status=status,
                    error=f"{message}：{_describe_body(getattr(response, 'text', ''))}",
                    attempts=attempt,
                )

            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.OK,
                candidates=parse_feed_items(
                    root, start, end, limit, collector=self.name, default_source="Google News"
                ),
                attempts=attempt,
            )

        return last


# --- generic RSS / Atom -----------------------------------------------------

def parse_feed_items(
    root: ET.Element,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
    collector: str,
    default_source: str = "",
    feed_title: str = "",
) -> list[Candidate]:
    """Parse RSS 2.0 or Atom entries into candidates inside a time window.

    Undated entries are kept: many vendor feeds omit dates, and dropping them
    would silently lose exactly the official sources worth having.
    """
    atom = "{http://www.w3.org/2005/Atom}"
    nodes = root.findall(".//item")
    is_atom = False
    if not nodes:
        nodes = root.findall(f".//{atom}entry")
        is_atom = bool(nodes)

    results: list[Candidate] = []
    for node in nodes:
        if is_atom:
            link = ""
            for link_node in node.findall(f"{atom}link"):
                rel = link_node.get("rel", "alternate")
                if rel == "alternate" or not link:
                    link = link_node.get("href", "") or link
            title = node.findtext(f"{atom}title") or ""
            summary = node.findtext(f"{atom}summary") or node.findtext(f"{atom}content") or ""
            published_raw = (
                node.findtext(f"{atom}published") or node.findtext(f"{atom}updated") or ""
            )
            published_at = _parse_iso8601(published_raw)
            source = feed_title or default_source
        else:
            link = node.findtext("link") or ""
            title = node.findtext("title") or ""
            summary = node.findtext("description") or ""
            published_raw = node.findtext("pubDate") or ""
            published_at = _parse_rfc822(published_raw) or _parse_iso8601(published_raw)
            source_node = node.find("source")
            source = (source_node.text if source_node is not None else "") or (
                feed_title or default_source
            )

        if not link:
            continue
        # The feed decides what it publishes; the window is enforced locally.
        if published_at and not (start <= published_at <= end):
            continue

        results.append(
            Candidate(
                title=normalize_space(title),
                url=link.strip(),
                domain=domain_of(link),
                source=normalize_space(source) or domain_of(link),
                snippet=normalize_space(strip_html(summary)),
                published_at=published_at,
                published_raw=published_raw,
                collector=collector,
            )
        )
        if len(results) >= max(limit, 1):
            break
    return results


#: Strips search operators so a query can be matched against feed text.
_QUERY_NOISE = re.compile(r'["\'()]|(?<![\w])(OR|AND|NOT)(?![\w])', re.IGNORECASE)
_QUERY_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+\-]{1,}|[一-鿿]{2,}")


def query_tokens(query: str) -> list[str]:
    """Meaningful terms from a search expression, for feed-side filtering."""
    cleaned = _QUERY_NOISE.sub(" ", query or "")
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _QUERY_TOKEN.findall(cleaned):
        token = raw.lower()
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _is_cjk(token: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in token)


def token_matches(token: str, haystack: str) -> bool:
    """Does one query term occur in a feed item's text?

    Latin terms must match on word boundaries. Substring matching looks
    harmless until a two-letter term like ``ai`` matches inside *av-ai-lable*
    and every release note in every feed answers every AI query. CJK has no
    word boundaries to anchor to, so those terms stay substring matches - which
    is correct for Chinese, where compounds are written without spaces.
    """
    if not token:
        return False
    if _is_cjk(token):
        return token in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack) is not None


#: Long queries must match on more than one term. A four-term query that hits
#: exactly one generic term is almost always a coincidence, and every false
#: candidate costs body extraction and analysis tokens downstream.
MULTI_MATCH_TOKEN_THRESHOLD = 4


def matches_query(candidate: Candidate, tokens: list[str]) -> bool:
    """True when a feed item plausibly answers the query.

    Feeds cannot be searched, so filtering happens here. The bar is kept low on
    purpose - one solid term is enough for a short query - because ranking and
    analysis downstream are what judge relevance, and an over-strict filter
    would hide official announcements that word things differently than the
    query does.
    """
    if not tokens:
        return True
    haystack = f"{candidate.title} {candidate.snippet}".lower()
    hits = sum(1 for token in tokens if token_matches(token, haystack))
    if not hits:
        return False
    required = 2 if len(tokens) >= MULTI_MATCH_TOKEN_THRESHOLD else 1
    return hits >= required


@dataclass
class FeedSpec:
    """A user-configured feed."""

    url: str
    title: str = ""
    domain: str = ""


class RSSCollector(SourceCollector):
    """Generic RSS/Atom collector over user-configured feeds.

    Valuable precisely because it does not depend on a search engine: official
    company blogs, open-source project feeds, standards bodies and technology
    media all publish feeds that stay reachable when Google News does not.

    Only feeds the user configured are fetched, and each feed is fetched once
    per window no matter how many queries are evaluated against it.
    """

    name = "rss"
    info = CollectorInfo(
        collector_id="rss",
        display_name="RSS / Atom",
        network_compatibility=NETWORK_ANY,
        requires_api_key=False,
        supports_search=False,
        supports_rss=True,
        rate_limit_policy="每个订阅源在一次运行内只抓取一次",
        description="用户自定义的官方博客、项目与媒体订阅源。",
    )

    def __init__(self, *args, feeds: Optional[list[FeedSpec]] = None, **kwargs) -> None:
        kwargs.setdefault("limiter", RateLimiter(min_interval=0.3, max_concurrency=3))
        kwargs.setdefault("breaker", CircuitBreaker(threshold=8, reset_after=300.0, name="rss"))
        kwargs.setdefault("max_attempts", 2)
        super().__init__(*args, **kwargs)
        self.feeds = list(feeds or [])
        #: feed url -> (candidates, window) fetched during this run.
        self._feed_cache: dict[tuple[str, str, str], list[Candidate]] = {}
        self._feed_errors: dict[str, str] = {}

    def _fetch(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int
    ) -> CollectorResult:
        if not self.feeds:
            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.NOT_CONFIGURED,
                error="尚未配置任何 RSS/Atom 订阅源",
                attempts=0,
            )

        tokens = query_tokens(query)
        merged: list[Candidate] = []
        failures = 0
        last_error = ""

        for feed in self.feeds:
            key = (feed.url, start.isoformat(), end.isoformat())
            cached = self._feed_cache.get(key)
            if cached is None:
                cached, error = self._load_feed(feed, start, end)
                if error:
                    failures += 1
                    last_error = f"{feed.title or feed.url}: {error}"
                    self._feed_errors[feed.url] = error
                    continue
                self._feed_cache[key] = cached
            merged.extend(c for c in cached if matches_query(c, tokens))

        if failures and failures == len(self.feeds):
            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.NETWORK_ERROR,
                error=last_error or "所有订阅源均不可访问",
            )

        return CollectorResult(
            collector=self.name,
            status=CollectorStatus.OK,
            candidates=merged[: max(limit, 1)],
            error=last_error if failures else "",
        )

    def _load_feed(
        self, feed: FeedSpec, start: dt.datetime, end: dt.datetime
    ) -> tuple[list[Candidate], str]:
        try:
            response = self._paced_get(feed.url)
            if response.status_code >= 400:
                return [], f"HTTP {response.status_code}"
            root = ET.fromstring(response.content)
        except Exception as exc:
            _status, message = classify_exception(exc)
            return [], message

        title = feed.title or (
            root.findtext(".//channel/title")
            or root.findtext("{http://www.w3.org/2005/Atom}title")
            or domain_of(feed.url)
        )
        items = parse_feed_items(
            root, start, end, limit=200, collector=self.name, feed_title=normalize_space(title)
        )
        return items, ""


# --- registry ---------------------------------------------------------------

#: Preferred collector order per network mode. The planner walks this list and
#: stops at the first source that answers, so the head of the list is the one
#: that should normally be asked.
NETWORK_ORDER: dict[str, tuple[str, ...]] = {
    "international": ("gdelt", "google_news", "rss"),
    "china": ("rss", "gdelt", "google_news"),
    "auto": ("gdelt", "google_news", "rss"),
}


class CollectorRegistry:
    """The collectors available to a run, with their metadata and order.

    Instances are per-run: the limiters and breakers the collectors carry are
    what make the run polite, and they must not be shared with a different run's
    (possibly different) network mode.
    """

    def __init__(
        self,
        collectors: Optional[list[SourceCollector]] = None,
        network_mode: str = "auto",
        disabled: Optional[set[str]] = None,
    ) -> None:
        self.collectors = collectors or [GDELTCollector(), GoogleNewsCollector()]
        self.network_mode = network_mode if network_mode in NETWORK_ORDER else "auto"
        self.disabled = set(disabled or ())

    # -- lookup --------------------------------------------------------------

    def by_id(self, collector_id: str) -> Optional[SourceCollector]:
        for collector in self.collectors:
            if collector.name == collector_id:
                return collector
        return None

    def describe(self) -> list[CollectorInfo]:
        return [collector.info for collector in self.collectors]

    def enabled_collectors(self) -> list[SourceCollector]:
        return [c for c in self.collectors if c.name not in self.disabled]

    def ordered(self, network_mode: Optional[str] = None) -> list[SourceCollector]:
        """Enabled collectors in the order this network mode should try them."""
        mode = network_mode or self.network_mode
        order = NETWORK_ORDER.get(mode, NETWORK_ORDER["auto"])
        available = {c.name: c for c in self.enabled_collectors()}
        ranked = [available[name] for name in order if name in available]
        ranked.extend(c for name, c in available.items() if name not in order)
        return ranked

    # -- collection ----------------------------------------------------------

    def collect_detailed(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int = 20
    ) -> list[CollectorResult]:
        """Try collectors in order, stopping at the first that returns hits.

        Every attempt is reported, including the ones that failed - the caller
        needs them to tell "nothing happened" from "nothing worked".
        """
        results: list[CollectorResult] = []
        for collector in self.ordered():
            try:
                result = collector.fetch(query, start, end, limit)
            except Exception as exc:  # a broken source must not kill the run
                logger.warning("Collector %s raised: %s", collector.name, exc)
                status, message = classify_exception(exc)
                result = CollectorResult(
                    collector=collector.name, status=status, error=message
                )
            results.append(result)
            if result.candidates:
                break
        return results

    def collect(
        self, query: str, start: dt.datetime, end: dt.datetime, limit: int = 20
    ) -> list[Candidate]:
        """Backwards-compatible shape: merged candidates only."""
        for result in self.collect_detailed(query, start, end, limit):
            if result.candidates:
                return result.candidates
        return []


def window_for(report_date: dt.date, lookback_days: int) -> tuple[dt.datetime, dt.datetime]:
    """Inclusive UTC search window ending at the close of ``report_date``."""
    end = dt.datetime.combine(report_date, dt.time.max).replace(microsecond=0)
    now = utcnow()
    if end > now:
        end = now
    start = end - dt.timedelta(days=max(lookback_days, 1))
    return start, end
