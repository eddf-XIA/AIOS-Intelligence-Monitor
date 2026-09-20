"""The global 简易版 / 本地专业版 switch.

One endpoint, one setting. Switching modes writes exactly one row in
``app_settings`` and touches nothing else - no monitoring configuration, no
events, no reports, no credentials. That is what makes the switch safe to press
at any time, from any page, including mid-report.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import mode_service
from ..web import redirect

router = APIRouter()

#: Where the switch lands when the caller gives no usable ``next``.
DEFAULT_TARGET = "/"


def safe_next(value: str) -> str:
    """Only same-origin absolute paths are honoured.

    The target comes from a form field, so it is attacker-controllable in
    principle; anything that is not a plain in-app path (``//host``,
    ``https://...``, a backslash trick) falls back to the home page rather than
    turning the switch into an open redirect.
    """
    target = (value or "").strip()
    if not target.startswith("/"):
        return DEFAULT_TARGET
    if target.startswith("//") or "\\" in target:
        return DEFAULT_TARGET
    return target


#: Pages that only exist in one mode. Switching *to* the other mode from one of
#: them has to land somewhere that exists, so it goes home instead of 404ing.
_SIMPLE_ONLY_PREFIXES = ("/simple",)
_PROFESSIONAL_ONLY_PREFIXES = (
    "/monitoring",
    "/settings",
    "/runs",
    "/compare",
    "/events",
)


def landing_for(mode: str, requested: str) -> str:
    """Where to send the user after switching to ``mode``."""
    target = safe_next(requested)

    if mode == mode_service.MODE_SIMPLE:
        if any(target.startswith(prefix) for prefix in _PROFESSIONAL_ONLY_PREFIXES):
            return DEFAULT_TARGET
    elif any(target.startswith(prefix) for prefix in _SIMPLE_ONLY_PREFIXES):
        return DEFAULT_TARGET

    return target


@router.post("/mode")
def set_mode(
    mode: str = Form(""),
    next: str = Form(DEFAULT_TARGET),  # noqa: A002 - matches the form field name
    session: Session = Depends(get_db),
):
    """Persist the mode preference and return the user to where they were."""
    applied = mode_service.set_mode(session, mode)
    return redirect(
        landing_for(applied, next),
        f"已切换到{mode_service.label(applied)}。配置与数据未变动。",
        "ok",
    )
