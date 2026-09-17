"""Typed read models for reports and comparison."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ReportSummary(BaseModel):
    """One row on the report history page."""

    id: int
    report_date: str
    title: str
    item_count: int
    new_events: int = 0
    updated_events: int = 0
    run_duration: str = ""
    legacy_import: bool = False


class CompareRequest(BaseModel):
    """Two dates to diff."""

    date_a: str
    date_b: str


class MetricChangeView(BaseModel):
    """A single metric delta rendered in the compare view."""

    key: str
    old_display: str
    new_display: str
    absolute_display: str = ""
    percentage_display: str = ""
    comparable: bool = False
    direction: str = "flat"


class DiffSummary(BaseModel):
    """Counts shown at the top of the compare page."""

    new: int = 0
    updated: int = 0
    unchanged: int = 0
    data_change: int = 0
    correction: int = 0
    resolved: int = 0
