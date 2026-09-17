"""Typed read models for events and observations."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class SourceView(BaseModel):
    """One piece of evidence behind an observation."""

    id: int
    title: str
    url: str
    source: str
    domain: str
    published: str = ""


class ObservationView(BaseModel):
    """One day's entry on an event timeline."""

    id: int
    observation_date: str
    title: str
    fact_summary: str
    assessment: str
    importance: int
    confidence: str
    is_correction: bool = False
    metrics: dict[str, Any] = {}
    sources: list[SourceView] = []


class EventView(BaseModel):
    """Event detail page payload."""

    id: int
    event_key: str
    title: str
    summary: str
    status: str
    module_name: str = ""
    topic_name: str = ""
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    observation_count: int = 0
    observations: list[ObservationView] = []
