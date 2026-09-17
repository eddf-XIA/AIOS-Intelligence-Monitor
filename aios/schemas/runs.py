"""Typed read models for run progress."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ModuleRunView(BaseModel):
    """One module row in the dashboard progress table."""

    id: int
    module_name: str
    status: str
    candidate_count: int = 0
    article_count: int = 0
    selected_count: int = 0
    error_message: str = ""


class RunView(BaseModel):
    """Status payload polled by the browser."""

    id: int
    status: str
    stage: str
    trigger_type: str
    report_date: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration: str = ""
    total_candidates: int = 0
    total_articles: int = 0
    total_events: int = 0
    total_new_events: int = 0
    total_updated_events: int = 0
    total_report_items: int = 0
    error_message: str = ""
    is_active: bool = False
    modules: list[ModuleRunView] = []
