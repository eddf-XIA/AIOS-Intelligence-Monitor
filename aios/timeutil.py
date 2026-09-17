"""Centralised time handling.

Rules for the whole project:

* Everything persisted to the database is **naive UTC**.
* Everything shown to the user is **local time**.
* "Report date" is the **local** calendar date, so a run started at 23:30 local
  on 2026-09-15 is filed under 2026-09-15 and not the following UTC day.

Never call ``datetime.now()`` directly elsewhere in the codebase; use the
helpers here so the above stays true.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

UTC = dt.timezone.utc


def utcnow() -> dt.datetime:
    """Current time as a naive UTC datetime (what we store)."""
    return dt.datetime.now(UTC).replace(tzinfo=None)


def local_now() -> dt.datetime:
    """Current wall-clock time of the machine running the app."""
    return dt.datetime.now()


def local_today() -> dt.date:
    """Local calendar date - the basis for every ``report_date``."""
    return dt.date.today()


def local_tz_name() -> str:
    """Best-effort name of the local timezone, for display in Settings."""
    tz = dt.datetime.now().astimezone().tzinfo
    try:
        name = tz.tzname(dt.datetime.now()) if tz else None
    except Exception:  # pragma: no cover - platform dependent
        name = None
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total), 60)
    return f"{name or 'Local'} (UTC{sign}{hh:02d}:{mm:02d})"


def to_local(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Convert a stored naive-UTC datetime to naive local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().replace(tzinfo=None)


def fmt_local(value: Optional[dt.datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a stored UTC datetime in local time, or '' when missing."""
    local = to_local(value)
    return local.strftime(fmt) if local else ""


def fmt_local_short(value: Optional[dt.datetime]) -> str:
    return fmt_local(value, "%m-%d %H:%M")


def fmt_time_local(value: Optional[dt.datetime]) -> str:
    return fmt_local(value, "%H:%M:%S")


def cn_duration_text(total_seconds: float) -> str:
    """``27 秒`` / ``2 分 41 秒`` / ``1 小时 08 分``.

    One convention project-wide, and deliberately mirrored character for
    character by ``formatElapsed`` in ``static/js/app.js``: the live run clock
    is rendered by the browser between polls, and a server-rendered duration
    must never disagree with the ticking one on the same screen.

    Whole seconds only - a monitoring run is not a stopwatch, and milliseconds
    would just make the value flicker.
    """
    seconds = int(total_seconds)
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def duration_text(start: Optional[dt.datetime], end: Optional[dt.datetime]) -> str:
    """Human readable duration between two stored datetimes.

    With no ``end`` this measures against *now*, so an in-flight run gets its
    elapsed time so far. That value is a snapshot taken at render time; the Run
    Detail page keeps it moving client-side rather than by re-polling for it.
    """
    if not start:
        return ""
    end = end or utcnow()
    return cn_duration_text((end - start).total_seconds())


def iso_utc(value: Optional[dt.datetime]) -> str:
    """A stored naive-UTC datetime as an unambiguous ISO 8601 instant.

    Datetimes are persisted naive (see :class:`~aios.models.base.UTCDateTime`),
    so the offset has to be reattached explicitly. Handing ``Date.parse`` a
    string without one would let the browser read a UTC timestamp as local
    time, which is a whole-timezone error in the elapsed clock.
    """
    if value is None:
        return ""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds")


def parse_date(value: str) -> dt.date:
    """Parse ``YYYY-MM-DD`` into a date, raising ValueError on bad input."""
    return dt.datetime.strptime(value.strip(), "%Y-%m-%d").date()


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into (hour, minute)."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time: {value!r}")
    return hour, minute
