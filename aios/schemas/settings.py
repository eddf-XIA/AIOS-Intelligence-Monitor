"""Validation for the Settings page.

AI provider forms live in :mod:`aios.schemas.providers`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..timeutil import parse_hhmm


class SchedulerForm(BaseModel):
    """Automatic monitoring settings."""

    enabled: bool = False
    time: str = Field(default="06:00", max_length=5)

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        text = (value or "").strip()
        try:
            hour, minute = parse_hhmm(text)
        except ValueError as exc:
            raise ValueError("Enter a time as HH:MM, for example 06:00.") from exc
        return f"{hour:02d}:{minute:02d}"


class CollectionSettingsForm(BaseModel):
    """Advanced collection and extraction tuning."""

    default_lookback_days: int = Field(default=3, ge=1, le=60)
    default_max_candidates: int = Field(default=18, ge=1, le=100)
    article_max_chars: int = Field(default=7000, ge=500, le=50000)
    http_timeout: int = Field(default=20, ge=5, le=120)
    collect_workers: int = Field(default=6, ge=1, le=16)
    fetch_body_top_n: int = Field(default=8, ge=1, le=40)
    event_match_enabled: bool = True
    event_match_lookback_days: int = Field(default=45, ge=1, le=365)
    report_output_dir: str = ""

    @field_validator("report_output_dir")
    @classmethod
    def clean_dir(cls, value: str) -> str:
        return (value or "").strip()
