"""Run detail, live status polling and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RunStatus
from ..repositories import reports as reports_repo
from ..repositories import runs as runs_repo
from ..repositories import sources as sources_repo
from ..services.run_manager import manager
from ..timeutil import duration_text
from ..web import partial, redirect, render

router = APIRouter(prefix="/runs")


@router.get("")
def run_list(request: Request, session: Session = Depends(get_db)):
    """History of monitoring executions."""
    runs = runs_repo.list_runs(session, limit=60)
    return render(
        request,
        "run_list.html",
        {
            "nav": "runs",
            "runs": runs,
            "durations": {r.id: duration_text(r.started_at, r.finished_at) for r in runs},
        },
    )


@router.get("/{run_id}")
def run_detail(run_id: int, request: Request, session: Session = Depends(get_db)):
    """Per-run log, module progress and source health."""
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    report = reports_repo.get_by_run(session, run_id)
    logs = runs_repo.logs_for(session, run_id, limit=1000)
    return render(
        request,
        "run_detail.html",
        {
            "nav": "runs",
            "run": run,
            "module_runs": run.module_runs,
            "source_stats": sources_repo.stats_for_run(session, run_id),
            "logs": logs,
            "after": logs[-1].id if logs else 0,
            "usage": runs_repo.usage_summary(session, run_id),
            "report": report,
            "duration": duration_text(run.started_at, run.finished_at),
            "poll": run.status in RunStatus.ACTIVE,
        },
    )


@router.get("/{run_id}/live")
def run_live(run_id: int, request: Request, session: Session = Depends(get_db)):
    """HTMX fragment: stage, module rows and source health.

    Polls itself while the run is active and renders without a trigger once the
    run is terminal, so polling stops without any client-side bookkeeping.
    """
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return partial(
        request,
        "partials/run_live.html",
        {
            "run": run,
            "module_runs": run.module_runs,
            "source_stats": sources_repo.stats_for_run(session, run_id),
            "poll": run.status in RunStatus.ACTIVE,
            "duration": duration_text(run.started_at, run.finished_at),
        },
    )


@router.get("/{run_id}/status")
def run_status(run_id: int, request: Request, session: Session = Depends(get_db)):
    """HTMX fragment: module table + stage, polled every two seconds."""
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return partial(
        request,
        "partials/run_status.html",
        {
            "run": run,
            "module_runs": run.module_runs,
            "poll": run.status in RunStatus.ACTIVE,
            "duration": duration_text(run.started_at, run.finished_at),
        },
    )


@router.get("/{run_id}/logs")
def run_logs(
    run_id: int, request: Request, after: int = 0, session: Session = Depends(get_db)
):
    """HTMX fragment: log lines newer than ``after``."""
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    logs = runs_repo.logs_for(session, run_id, after_id=after, limit=500)
    return partial(
        request,
        "partials/run_log.html",
        {
            "run": run,
            "logs": logs,
            "after": logs[-1].id if logs else after,
            "poll": run.status in RunStatus.ACTIVE,
        },
    )


@router.post("/{run_id}/cancel")
def cancel_run(run_id: int, session: Session = Depends(get_db)):
    """Ask an in-flight run to stop at its next checkpoint."""
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in RunStatus.TERMINAL:
        return redirect(f"/runs/{run_id}", "该任务已结束。", "warn")
    manager.cancel(run_id)
    return redirect(f"/runs/{run_id}", "已请求取消，任务将在下一个检查点停止。", "warn")
