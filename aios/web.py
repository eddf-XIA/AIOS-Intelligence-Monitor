"""Shared web plumbing: templates, filters and flash messages.

Kept out of :mod:`aios.app` so routers can import the template environment
without importing the application factory (and creating an import cycle).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __app_name__, __version__
from .services.diff_engine import CHANGE_LABELS
from .timeutil import (
    duration_text,
    iso_utc,
    fmt_local,
    fmt_local_short,
    fmt_time_local,
    local_now,
    local_today,
    to_local,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- Jinja filters ----------------------------------------------------------

def _date_text(value: Optional[dt.date]) -> str:
    return value.isoformat() if value else ""


def _weekday_cn(value: Optional[dt.date]) -> str:
    if not value:
        return ""
    return "周" + "一二三四五六日"[value.weekday()]


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value if value is not None else "")


def _cn_number(value: Any) -> str:
    """Chinese-scale number: 8720万 / 1.2亿.

    One convention project-wide, so the same figure never appears as both
    ``85M`` and ``8500万`` on one screen. Small values stay exact.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")

    magnitude = abs(number)
    if magnitude >= 100_000_000:
        scaled = number / 100_000_000
        return f"{scaled:.2f}".rstrip("0").rstrip(".") + "亿"
    if magnitude >= 10_000:
        scaled = number / 10_000
        return f"{scaled:.2f}".rstrip("0").rstrip(".") + "万"
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _cn_date(value: Optional[dt.date]) -> str:
    """9 月 15 日 - for editorial and conversational contexts."""
    if not value:
        return ""
    return f"{value.month} 月 {value.day} 日"


def _cn_duration(value: Any) -> str:
    """2 分 41 秒 rather than 161 seconds."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def _relative_time(value: Optional[dt.datetime]) -> str:
    """刚刚 / 8 分钟前 / 今天 08:42 / 昨天 18:20 / 9 月 14 日.

    A full timestamp belongs in logs and audit views, not in every row.
    """
    local = to_local(value)
    if local is None:
        return ""

    now = local_now()
    delta = now - local
    seconds = delta.total_seconds()

    if seconds < 0:
        return local.strftime("%H:%M")
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"

    today = now.date()
    day = local.date()
    if day == today:
        return f"今天 {local.strftime('%H:%M')}"
    if (today - day).days == 1:
        return f"昨天 {local.strftime('%H:%M')}"
    if (today - day).days < 365:
        return f"{local.month} 月 {local.day} 日"
    return local.strftime("%Y-%m-%d")


def _truncate(value: Any, length: int = 120) -> str:
    text = str(value or "")
    return text if len(text) <= length else text[: length - 1] + "…"


templates.env.filters["localtime"] = fmt_local
templates.env.filters["localshort"] = fmt_local_short
templates.env.filters["localclock"] = fmt_time_local
templates.env.filters["datetext"] = _date_text
templates.env.filters["weekday"] = _weekday_cn
templates.env.filters["number"] = _number
templates.env.filters["cn_number"] = _cn_number
templates.env.filters["cn_date"] = _cn_date
templates.env.filters["cn_duration"] = _cn_duration
templates.env.filters["ago"] = _relative_time
templates.env.filters["truncate_text"] = _truncate
templates.env.filters["duration"] = lambda pair: duration_text(pair[0], pair[1])
#: Naive-UTC datetime -> ISO 8601 with an explicit offset, for client-side JS.
templates.env.filters["isoutc"] = iso_utc

templates.env.globals["app_name"] = __app_name__
templates.env.globals["app_version"] = __version__
templates.env.globals["change_labels"] = CHANGE_LABELS
templates.env.globals["today"] = local_today


#: Status -> (badge class, label, dot class). Every state carries text as well
#: as colour, so nothing is communicated by hue alone.
STATUS_STYLES: dict[str, tuple[str, str, str]] = {
    "pending": ("s-pending", "等待中", "dot"),
    "queued": ("s-pending", "排队中", "dot"),
    "running": ("s-running", "监测中", "dot dot-running"),
    "collecting": ("s-running", "采集中", "dot dot-running"),
    "extracting": ("s-running", "抓取正文", "dot dot-running"),
    "analyzing": ("s-running", "分析中", "dot dot-running"),
    "matching": ("s-running", "事件归并", "dot dot-running"),
    "synthesizing": ("s-running", "综合研判", "dot dot-running"),
    "generating_report": ("s-running", "生成报告", "dot dot-running"),
    "planning": ("s-running", "准备中", "dot dot-running"),
    "completed": ("s-ok", "已完成", "dot dot-success"),
    "completed_with_errors": ("s-warn", "部分失败", "dot dot-warning"),
    "completed_with_warnings": ("s-warn", "部分数据源异常", "dot dot-warning"),
    "checking_sources": ("s-running", "检测数据源", "dot dot-running"),
    "failed": ("s-err", "失败", "dot dot-error"),
    "cancelled": ("s-muted", "已取消", "dot"),
    "skipped": ("s-muted", "已跳过", "dot"),
    "active": ("s-ok", "追踪中", "dot dot-success"),
    "watching": ("s-running", "观察中", "dot dot-info"),
    "resolved": ("s-muted", "已结束", "dot"),
    "archived": ("s-muted", "已归档", "dot"),
}


def status_style(status: str) -> tuple[str, str, str]:
    """(badge class, Chinese label, dot class) for any status value."""
    return STATUS_STYLES.get(status or "", ("s-muted", status or "-", "dot"))


templates.env.globals["status_style"] = status_style


def health_label(state: str) -> str:
    """Chinese label for a source-health state."""
    from .services.source_health import HEALTH_LABELS

    return HEALTH_LABELS.get(state or "", state or "-")


def health_badge(state: str) -> str:
    """Badge class for a source-health state."""
    from .services.source_health import HEALTH_BADGES

    return HEALTH_BADGES.get(state or "", "s-muted")


def collection_status_label(state: str) -> str:
    """Chinese label for a topic/module collection outcome."""
    from .services.collection_planner import TOPIC_STATUS_LABELS

    return TOPIC_STATUS_LABELS.get(state or "", state or "")


templates.env.globals["health_label"] = health_label
templates.env.globals["health_badge"] = health_badge
def empty_section_text(coverage_state: str) -> str:
    """What an empty report section may claim, given how collection went."""
    from .services.report_generator import empty_section_text as _text

    return _text(coverage_state or "")


templates.env.globals["collection_status_label"] = collection_status_label
templates.env.globals["empty_section_text"] = empty_section_text


# --- flash messages ---------------------------------------------------------

def redirect(url: str, message: str = "", level: str = "ok") -> RedirectResponse:
    """Redirect after a POST, carrying a one-shot message in the query string."""
    if message:
        from urllib.parse import quote

        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}msg={quote(message)}&lvl={level}"
    return RedirectResponse(url=url, status_code=303)


def render(
    request: Request, template: str, context: Optional[dict] = None, status_code: int = 200
) -> HTMLResponse:
    """Render a template with the standard context additions."""
    data = dict(context or {})
    data["request"] = request
    data.setdefault("flash", request.query_params.get("msg", ""))
    data.setdefault("flash_level", request.query_params.get("lvl", "ok"))
    data.setdefault("nav", "")
    return templates.TemplateResponse(request, template, data, status_code=status_code)


def partial(request: Request, template: str, context: Optional[dict] = None) -> HTMLResponse:
    """Render an HTMX fragment (no flash handling, no layout)."""
    data = dict(context or {})
    data["request"] = request
    return templates.TemplateResponse(request, template, data)
