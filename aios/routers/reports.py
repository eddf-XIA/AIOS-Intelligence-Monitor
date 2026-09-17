"""Report history, detail views, evidence view and JSON audit."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..repositories import reports as reports_repo
from ..repositories import runs as runs_repo
from ..services import report_generator
from ..timeutil import duration_text
from ..web import redirect, render

router = APIRouter(prefix="/reports")


@router.get("")
def report_list(request: Request, q: str = "", session: Session = Depends(get_db)):
    """Report history, newest first, with an optional title search."""
    reports = reports_repo.list_reports(session, limit=120, term=q)
    rows = []
    for report in reports:
        run = report.run
        rows.append(
            {
                "report": report,
                # Not "items": Jinja resolves `row.items` to dict.items().
                "item_count": report.item_count,
                "new_events": run.total_new_events if run else 0,
                "updated_events": run.total_updated_events if run else 0,
                "duration": duration_text(run.started_at, run.finished_at) if run else "",
            }
        )
    matches = reports_repo.search_items(session, q, limit=40) if q.strip() else []
    return render(
        request,
        "report_list.html",
        {"nav": "reports", "rows": rows, "q": q, "matches": matches},
    )


@router.get("/{report_id}")
def report_detail(
    report_id: int, request: Request, view: str = "report", session: Session = Depends(get_db)
):
    """Report / evidence / event views over the same stored data."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    previous = reports_repo.previous_report(session, report)
    return render(
        request,
        "report_detail.html",
        {
            "nav": "reports",
            "report": report,
            "view": view if view in {"report", "evidence", "events"} else "report",
            "previous": previous,
            "usage": runs_repo.usage_summary(session, report.run_id) if report.run_id else {},
            "coverage_notice": report_generator.coverage_notice(report),
        },
    )


@router.get("/{report_id}/html", response_class=HTMLResponse)
def report_html(report_id: int, session: Session = Depends(get_db)):
    """The standalone HTML artifact, rendered live from the database."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return HTMLResponse(report_generator.render_html(report))


@router.get("/{report_id}/json")
def report_json(report_id: int, session: Session = Depends(get_db)):
    """The machine-readable audit snapshot."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return JSONResponse(report_generator.build_audit_payload(report))


@router.get("/{report_id}/audit", response_class=PlainTextResponse)
def report_audit_text(report_id: int, session: Session = Depends(get_db)):
    """Pretty-printed audit JSON for reading in the browser."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    payload = report_generator.build_audit_payload(report)
    return PlainTextResponse(
        json.dumps(payload, ensure_ascii=False, indent=2), media_type="text/plain; charset=utf-8"
    )


@router.post("/{report_id}/export")
def report_export(report_id: int, session: Session = Depends(get_db)):
    """Re-write the HTML and JSON files to the reports directory."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    html_path, json_path = report_generator.export_report(session, report)
    return redirect(
        f"/reports/{report_id}",
        f"已导出：{Path(html_path).name} 与 {Path(json_path).name}",
        "ok",
    )


@router.post("/{report_id}/delete")
def report_delete(report_id: int, session: Session = Depends(get_db)):
    """Remove a report. Events and articles are kept - only the publication goes."""
    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    date_text = report.report_date.isoformat()
    reports_repo.delete_report(session, report)
    return redirect("/reports", f"已删除 {date_text} 的报告（情报事件与来源保留）。", "ok")
