"""Declarative base plus shared column mixins."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, JSON, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..timeutil import utcnow


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class UTCDateTime(TypeDecorator):
    """Stores naive UTC datetimes; strips tzinfo defensively on the way in."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: D102
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # noqa: D102
        return value


class TimestampMixin:
    """``created_at`` / ``updated_at`` in UTC."""

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
