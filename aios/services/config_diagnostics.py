"""Configuration cost and quality diagnostics.

Every enabled search query becomes real HTTP requests against public APIs on
every run. A module the user thought was cheap can quietly become the reason
GDELT starts answering 429.

These checks make that cost visible. They never block a save: an advanced user
who genuinely wants eighteen queries is allowed to have them - they just get to
see what it means first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from ..schemas.config_generation import normalize_query

#: Above this, a module's collection time and rate-limit exposure grow enough
#: to be worth a warning.
BUSY_QUERY_COUNT = 12
LONG_QUERY_CHARS = 120
#: Jaccard overlap over token sets; two queries above this mostly return the
#: same articles and one of them is wasted budget.
SIMILARITY_THRESHOLD = 0.75

_TOKEN = re.compile(r"[a-z0-9]+|[一-鿿]")


@dataclass(frozen=True)
class Diagnostic:
    """One finding about a module or topic configuration."""

    level: str  # "warn" | "info"
    message: str
    scope: str = ""

    def as_dict(self) -> dict:
        return {"level": self.level, "message": self.message, "scope": self.scope}


def _tokens(query: str) -> set[str]:
    return set(_TOKEN.findall((query or "").lower()))


def similarity(a: str, b: str) -> float:
    """Token-set overlap between two queries, 0.0 - 1.0."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class QuerySpec:
    """One query with enough context to report where it came from."""

    query: str
    topic_name: str = ""
    enabled: bool = True


def analyze_queries(specs: Iterable[QuerySpec], module_name: str = "") -> list[Diagnostic]:
    """Duplicate / near-duplicate / empty / oversized query checks."""
    found: list[Diagnostic] = []
    active = [s for s in specs if s.enabled]

    empty = [s for s in active if not s.query.strip()]
    if empty:
        found.append(Diagnostic("warn", f"存在 {len(empty)} 条空检索式，采集时会被跳过。"))

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for spec in active:
        key = normalize_query(spec.query)
        if not key:
            continue
        if key in seen:
            duplicates.append(spec.query)
        else:
            seen[key] = spec.topic_name
    if duplicates:
        sample = "、".join(f"「{q[:28]}」" for q in duplicates[:3])
        found.append(
            Diagnostic(
                "warn",
                f"有 {len(duplicates)} 条重复检索式（{sample}）。"
                "重复检索式在同一次运行中会被合并，但仍会占用配置空间。",
            )
        )

    unique = list(seen.keys())
    near: list[tuple[str, str]] = []
    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            if similarity(unique[i], unique[j]) >= SIMILARITY_THRESHOLD:
                near.append((unique[i], unique[j]))
    if near:
        first = near[0]
        found.append(
            Diagnostic(
                "info",
                f"有 {len(near)} 组检索式高度相似（如「{first[0][:26]}」与「{first[1][:26]}」），"
                "可能返回大量重复结果。",
            )
        )

    long_ones = [s for s in active if len(s.query) > LONG_QUERY_CHARS]
    if long_ones:
        found.append(
            Diagnostic(
                "info",
                f"有 {len(long_ones)} 条检索式超过 {LONG_QUERY_CHARS} 字符，"
                "部分数据源会截断过长的检索式。",
            )
        )

    count = len([s for s in active if s.query.strip()])
    if count > BUSY_QUERY_COUNT:
        label = f"该模块（{module_name}）" if module_name else "该模块"
        found.append(
            Diagnostic(
                "warn",
                f"{label}包含 {count} 条检索式，可能增加采集时间并触发数据源限流。",
            )
        )
    return found


def analyze_sources(domains: Iterable[str]) -> list[Diagnostic]:
    """Duplicate preferred-source domains."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for domain in domains:
        cleaned = (domain or "").strip().lower()
        if not cleaned:
            continue
        if cleaned in seen:
            duplicates.add(cleaned)
        seen.add(cleaned)
    if duplicates:
        return [
            Diagnostic(
                "info",
                "优先来源存在重复域名：" + "、".join(sorted(duplicates)[:4]) + "。",
            )
        ]
    return []


def diagnose_module(module) -> list[Diagnostic]:
    """Diagnostics for a stored :class:`~aios.models.MonitorModule`."""
    specs: list[QuerySpec] = []
    domains: list[str] = [s.domain for s in module.preferred_sources if s.enabled]
    for topic in module.topics:
        if topic.archived:
            continue
        domains.extend(s.domain for s in topic.preferred_sources if s.enabled)
        for query in topic.queries:
            specs.append(
                QuerySpec(query=query.query, topic_name=topic.name, enabled=query.enabled)
            )

    found = analyze_queries(specs, module_name=module.name)
    found.extend(analyze_sources(domains))

    active_topics = [t for t in module.topics if not t.archived and t.enabled]
    if not active_topics:
        found.append(Diagnostic("warn", "该模块没有启用的主题，监测时会被跳过。"))
    elif not any(t.active_queries for t in active_topics):
        found.append(Diagnostic("warn", "该模块没有任何启用的检索式，监测时会被跳过。"))
    return found


def diagnose_generated(config) -> list[Diagnostic]:
    """Diagnostics for a draft that has not been saved yet."""
    specs = [
        QuerySpec(query=q.query, topic_name=topic.name)
        for topic in config.topics
        for q in topic.queries
    ]
    domains = [s.domain for topic in config.topics for s in topic.preferred_sources]
    found = analyze_queries(specs, module_name=config.name)
    found.extend(analyze_sources(domains))
    return found


def estimated_requests(query_count: int, collectors: int = 1) -> int:
    """Upper bound on external search requests one run of a module performs.

    Staged collection stops at the first collector that answers, so this is the
    worst case (every collector tried for every query), not the expected case.
    """
    return max(0, query_count) * max(1, collectors)


def summarize(diagnostics: list[Diagnostic]) -> Optional[str]:
    """One-line summary for a flash message, or None when all is well."""
    warnings = [d for d in diagnostics if d.level == "warn"]
    if warnings:
        return warnings[0].message
    return None
