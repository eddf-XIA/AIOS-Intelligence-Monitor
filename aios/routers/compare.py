"""Report comparison at the intelligence-event level."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..repositories import reports as reports_repo
from ..services import diff_engine
from ..timeutil import parse_date
from ..web import render

router = APIRouter(prefix="/compare")


@router.get("")
def compare(
    request: Request,
    date_a: str = "",
    date_b: str = "",
    session: Session = Depends(get_db),
):
    """Compare two report dates.

    With no dates supplied, defaults to the two most recent reports so the page
    is useful on first visit.
    """
    available = reports_repo.list_dates(session)
    error = ""
    parsed_a: dt.date | None = None
    parsed_b: dt.date | None = None

    if date_a and date_b:
        try:
            parsed_a = parse_date(date_a)
            parsed_b = parse_date(date_b)
        except ValueError:
            error = "日期格式应为 YYYY-MM-DD。"
    elif len(available) >= 2:
        parsed_b, parsed_a = available[0], available[1]
    elif len(available) == 1:
        error = "只有一份报告，暂时无法对比。"
    else:
        error = "还没有任何报告，请先执行一次监测。"

    result = None
    if parsed_a and parsed_b and not error:
        result = diff_engine.compare_dates(session, parsed_a, parsed_b)
        error = result.error

    return render(
        request,
        "compare.html",
        {
            "nav": "compare",
            "available": available,
            "date_a": parsed_a.isoformat() if parsed_a else "",
            "date_b": parsed_b.isoformat() if parsed_b else "",
            "result": result,
            "error": error,
        },
    )
