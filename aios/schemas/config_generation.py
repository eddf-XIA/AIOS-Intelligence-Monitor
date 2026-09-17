"""Validated shape of an AI-generated monitoring configuration.

The model never writes to the database directly. It returns JSON, that JSON is
parsed into :class:`GeneratedMonitorConfig`, the user reviews (and may edit) the
result, and only then does :mod:`aios.services.config_generator` apply it.

Two kinds of protection live here:

* **Rejection** - anything structurally unusable (no module name, an empty
  query, a domain that is not a domain) raises ``ValidationError`` so nothing
  half-formed reaches the preview.
* **Budgeting** - a model asked for "queries about humanoid robots" will happily
  produce twenty overlapping ones. Every search query costs real requests
  against public APIs, so the caps in :data:`LIMITS` truncate generously-sized
  output instead of letting it through.

Truncation is deliberate rather than fatal: a slightly over-eager draft is still
useful to the user, and re-validating an already-truncated draft is a no-op.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Hard safety limits for one generation operation. Conservative on purpose:
#: three topics with two good queries each beats three topics with ten
#: overlapping ones, and costs a third of the requests.
MAX_TOPICS_PER_MODULE = 8
MAX_QUERIES_PER_TOPIC = 5
MAX_TOTAL_QUERIES = 24
MAX_SOURCES_PER_TOPIC = 8
MAX_EXCLUSIONS_PER_TOPIC = 12

LIMITS = {
    "max_topics": MAX_TOPICS_PER_MODULE,
    "max_queries_per_topic": MAX_QUERIES_PER_TOPIC,
    "max_total_queries": MAX_TOTAL_QUERIES,
    "max_sources_per_topic": MAX_SOURCES_PER_TOPIC,
    "max_exclusions_per_topic": MAX_EXCLUSIONS_PER_TOPIC,
}

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
#: Anything that looks like an embedded credential disqualifies a source.
_SECRETISH = re.compile(r"(api[_-]?key|token|password|secret|access[_-]?key)", re.IGNORECASE)


def normalize_query(value: str) -> str:
    """Canonical form used for duplicate detection (not for execution)."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def slugify_key(value: str, fallback: str = "module") -> str:
    """Best-effort module key from a name that may be entirely Chinese."""
    ascii_part = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    if ascii_part and KEY_PATTERN.match(ascii_part):
        return ascii_part[:60]
    # A Chinese-only name has no usable ASCII; derive a stable short hash.
    import hashlib

    digest = hashlib.sha1((value or fallback).encode("utf-8")).hexdigest()[:8]
    return f"{fallback}_{digest}"


class GeneratedQuery(BaseModel):
    """One search expression proposed by the model."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=300)
    priority: int = Field(default=5, ge=0, le=10)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", (value or "").strip())
        if not cleaned:
            raise ValueError("检索式不能为空。")
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            raise ValueError("检索式应是关键词，不是 URL。")
        return cleaned


class GeneratedSource(BaseModel):
    """A domain the model believes is a good evidence source.

    Presented to the user as a *suggestion*. The application does not verify
    that the domain exists or that it is reachable, so nothing in the UI claims
    it does.
    """

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=3, max_length=200)
    reason: str = Field(default="", max_length=200)

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, value: str) -> str:
        raw = (value or "").strip().lower()
        # Checked before any normalisation: stripping "?api_key=..." off a URL
        # would quietly turn a credential-bearing suggestion into an innocent
        # domain, and we would rather refuse it and say why.
        if "@" in raw or _SECRETISH.search(raw):
            raise ValueError("来源域名不得包含凭据信息。")

        domain = raw.removeprefix("https://").removeprefix("http://")
        domain = domain.split("/")[0].split("?")[0].lstrip(".")
        if domain.startswith("www."):
            domain = domain[4:]
        if not _DOMAIN_PATTERN.match(domain):
            raise ValueError(f"'{value}' 不是合法的域名。")
        return domain

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip())


class GeneratedTopic(BaseModel):
    """One monitored subject with everything the pipeline needs to run it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=600)
    queries: list[GeneratedQuery] = Field(default_factory=list)
    preferred_sources: list[GeneratedSource] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    analysis_instructions: str = Field(default="", max_length=1200)

    @field_validator("name", "description", "analysis_instructions")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return re.sub(r"[ \t]+", " ", (value or "").strip())

    @field_validator("excluded_keywords")
    @classmethod
    def clean_exclusions(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in value or []:
            keyword = re.sub(r"\s+", " ", str(raw or "").strip())[:60]
            if not keyword or keyword.lower() in seen:
                continue
            seen.add(keyword.lower())
            cleaned.append(keyword)
        return cleaned[:MAX_EXCLUSIONS_PER_TOPIC]

    @model_validator(mode="after")
    def enforce_topic_budget(self) -> "GeneratedTopic":
        """Drop duplicate queries and cap the per-topic query/source budget."""
        seen: set[str] = set()
        unique: list[GeneratedQuery] = []
        for item in self.queries:
            key = normalize_query(item.query)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        object.__setattr__(self, "queries", unique[:MAX_QUERIES_PER_TOPIC])

        seen_domains: set[str] = set()
        sources: list[GeneratedSource] = []
        for source in self.preferred_sources:
            if source.domain in seen_domains:
                continue
            seen_domains.add(source.domain)
            sources.append(source)
        object.__setattr__(self, "preferred_sources", sources[:MAX_SOURCES_PER_TOPIC])

        if not self.queries:
            raise ValueError(f"主题「{self.name}」没有任何可用检索式。")
        return self


class GeneratedMonitorConfig(BaseModel):
    """A complete monitoring module draft, ready for preview.

    ``optional_notes`` carries anything the model wanted to tell the user that
    does not belong in the configuration itself.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    key: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=600)
    topics: list[GeneratedTopic] = Field(default_factory=list)
    recommended_lookback_days: int = Field(default=3, ge=1, le=60)
    recommended_report_limit: int = Field(default=2, ge=1, le=10)
    analysis_instructions: str = Field(default="", max_length=1200)
    optional_notes: str = Field(default="", max_length=800)

    @field_validator("name", "description", "analysis_instructions", "optional_notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return re.sub(r"[ \t]+", " ", (value or "").strip())

    @field_validator("key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        key = (value or "").strip().lower().replace(" ", "_")
        key = re.sub(r"[^a-z0-9_-]", "", key)
        return key[:60]

    @model_validator(mode="after")
    def enforce_module_budget(self) -> "GeneratedMonitorConfig":
        if not self.key or not KEY_PATTERN.match(self.key):
            object.__setattr__(self, "key", slugify_key(self.name))

        # Topic names must be unique inside one module: the DB enforces it and
        # a partially-applied module is worse than a rejected one.
        seen: set[str] = set()
        topics: list[GeneratedTopic] = []
        for topic in self.topics:
            lowered = topic.name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            topics.append(topic)
        topics = topics[:MAX_TOPICS_PER_MODULE]

        # Global query budget, spent in topic order so the first (usually most
        # relevant) topics keep their queries.
        budget = MAX_TOTAL_QUERIES
        kept: list[GeneratedTopic] = []
        for topic in topics:
            if budget <= 0:
                break
            if len(topic.queries) > budget:
                object.__setattr__(topic, "queries", topic.queries[:budget])
            budget -= len(topic.queries)
            kept.append(topic)
        object.__setattr__(self, "topics", kept)

        if not self.topics:
            raise ValueError("生成结果不包含任何可用主题。")
        return self

    @property
    def total_queries(self) -> int:
        return sum(len(t.queries) for t in self.topics)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class GeneratedTopicOnly(BaseModel):
    """Result of "AI 添加主题" inside an existing module.

    Deliberately a different model from :class:`GeneratedMonitorConfig`: adding
    a topic must never carry module-level fields that could overwrite the
    module the user is editing.
    """

    model_config = ConfigDict(extra="forbid")

    topic: GeneratedTopic
    optional_notes: str = Field(default="", max_length=800)

    @field_validator("optional_notes")
    @classmethod
    def strip_notes(cls, value: str) -> str:
        return (value or "").strip()

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def describe_limits() -> dict[str, int]:
    """The caps, for display in the UI and in the generation prompt."""
    return dict(LIMITS)


def first_error(exc: Exception) -> str:
    """A single human-readable line from a pydantic ValidationError."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    try:
        detail: Optional[dict] = errors()[0]
    except Exception:  # pragma: no cover - defensive
        return str(exc)
    if not detail:
        return str(exc)
    location = ".".join(str(part) for part in detail.get("loc", ()))
    message = detail.get("msg", "输入无效")
    message = message.removeprefix("Value error, ")
    return f"{location}: {message}" if location else message
