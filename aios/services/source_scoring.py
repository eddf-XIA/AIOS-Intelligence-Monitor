"""Source quality ranking.

Rules only - the model never scores its own evidence. Extends the original
``TRUST_HINTS`` list with per-module / per-topic preferred domains configured
through the web UI.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..timeutil import utcnow
from .collector import Candidate

#: Baseline trusted domains, carried over from the original script.
TRUST_HINTS: tuple[str, ...] = (
    ".gov", ".edu", "huawei.com", "microsoft.com", "google.com", "android.com",
    "apple.com", "openeuler.org", "openatom.org", "openharmony", "ros.org",
    "rt-thread.org", "nvidia.com", "alibabacloud.com", "openanolis.cn", "cas.cn",
    "stdaily.com", "people.com.cn", "xinhuanet.com", "gitee.com", "github.com",
)

#: Aggregators and syndication hosts - real but never primary.
AGGREGATOR_HINTS: tuple[str, ...] = (
    "news.google.com", "msn.com", "finance.yahoo.com", "sina.com.cn", "sohu.com",
    "163.com", "toutiao.com", "baijiahao.baidu.com",
)


@dataclass(frozen=True)
class ScoringContext:
    """Configured domain preferences for the topic being scored."""

    preferred_domains: dict[str, int]

    @classmethod
    def from_rows(cls, rows: Iterable) -> "ScoringContext":
        """Build from :class:`~aios.models.PreferredSource` rows."""
        mapping: dict[str, int] = {}
        for row in rows:
            if not getattr(row, "enabled", True):
                continue
            domain = (row.domain or "").strip().lower().lstrip(".")
            if not domain:
                continue
            mapping[domain] = max(mapping.get(domain, 0), int(row.priority or 5))
        return cls(preferred_domains=mapping)

    @classmethod
    def empty(cls) -> "ScoringContext":
        return cls(preferred_domains={})


def _domain_matches(domain: str, needle: str) -> bool:
    return bool(domain) and (domain == needle or domain.endswith("." + needle) or needle in domain)


def score_candidate(
    candidate: Candidate,
    context: Optional[ScoringContext] = None,
    now: Optional[dt.datetime] = None,
) -> float:
    """Rank a candidate by provenance, extractability and freshness.

    The scale is open-ended but in practice lands between -2 and ~20. Higher is
    better; ties are broken by the caller on publication date.
    """
    context = context or ScoringContext.empty()
    domain = (candidate.domain or "").lower()
    score = 0.0

    # An aggregator is never a primary source, even when its host contains a
    # trusted brand (news.google.com must not inherit google.com's standing).
    is_aggregator = any(hint in domain for hint in AGGREGATOR_HINTS)

    # 1. Explicitly preferred (official) domains dominate.
    for needle, priority in context.preferred_domains.items():
        if _domain_matches(domain, needle):
            score += 5.0 + min(priority, 10)
            break
    else:
        # 2. Otherwise fall back to the baseline trust list.
        if not is_aggregator and any(hint in domain for hint in TRUST_HINTS):
            score += 4.0

    # 3. Aggregators are usable but should never outrank a primary source.
    if is_aggregator:
        score -= 2.0

    # 4. Evidence we could actually read is worth more than a headline.
    if candidate.body_text:
        score += 2.0
        if len(candidate.body_text) > 1200:
            score += 1.0
    elif candidate.snippet:
        score += 0.5

    # 5. Freshness.
    if candidate.published_at:
        score += 1.0
        reference = now or utcnow()
        age_days = (reference - candidate.published_at).total_seconds() / 86400
        if age_days <= 1:
            score += 1.5
        elif age_days <= 3:
            score += 0.75

    if not candidate.domain:
        score -= 0.5

    return round(score, 3)


def rank(
    candidates: Sequence[Candidate],
    context: Optional[ScoringContext] = None,
    now: Optional[dt.datetime] = None,
) -> list[Candidate]:
    """Score every candidate in place and return them best-first."""
    for candidate in candidates:
        candidate.trust_score = score_candidate(candidate, context, now)
    return sorted(
        candidates,
        key=lambda c: (c.trust_score, c.published_at or dt.datetime.min),
        reverse=True,
    )
