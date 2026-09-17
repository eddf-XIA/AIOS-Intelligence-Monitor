"""Validation for monitoring configuration forms."""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ModuleForm(BaseModel):
    """Create/update payload for a monitor module."""

    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    enabled: bool = True
    lookback_days: int = Field(default=3, ge=1, le=60)
    max_candidates: int = Field(default=18, ge=1, le=100)
    max_report_items: int = Field(default=2, ge=1, le=10)
    analysis_prompt: str = ""
    sort_order: int = 0

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = value.strip().lower().replace(" ", "-")
        if not KEY_PATTERN.match(key):
            raise ValueError(
                "Key must be lowercase letters, digits, '-' or '_', starting with a letter or digit."
            )
        return key

    @field_validator("name", "description", "analysis_prompt")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return (value or "").strip()


class TopicForm(BaseModel):
    """Create/update payload for a topic."""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    enabled: bool = True
    analysis_prompt: str = ""
    sort_order: int = 0

    @field_validator("name", "description", "analysis_prompt")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return (value or "").strip()


class QueryForm(BaseModel):
    """A single search expression."""

    query: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=100)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Query cannot be empty.")
        return cleaned


class PreferredSourceForm(BaseModel):
    """A domain to prioritise for a module or topic."""

    domain: str = Field(min_length=3, max_length=200)
    priority: int = Field(default=5, ge=1, le=10)

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, value: str) -> str:
        domain = (value or "").strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://")
        domain = domain.split("/")[0].lstrip(".")
        if domain.startswith("www."):
            domain = domain[4:]
        if not domain or "." not in domain:
            raise ValueError("Enter a domain such as huawei.com.")
        return domain


class ExcludedKeywordForm(BaseModel):
    """A keyword that disqualifies a search result."""

    keyword: str = Field(min_length=1, max_length=200)

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Keyword cannot be empty.")
        return cleaned


class ModuleSummary(BaseModel):
    """Read model for the monitoring list page."""

    id: int
    key: str
    name: str
    enabled: bool
    topic_count: int
    query_count: int
    lookback_days: int
    last_run: Optional[str] = None
