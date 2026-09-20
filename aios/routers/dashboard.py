"""Dashboard: last run, live module progress, and the daily diff."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RunStatus
from ..repositories import articles as articles_repo
from ..repositories import events as events_repo
from ..repositories import modules as modules_repo
from ..repositories import reports as reports_repo
from ..repositories import runs as runs_repo
from ..services import diff_engine
from ..services.run_manager import RunAlreadyActive, manager
from ..services.scheduler import scheduler
from ..repositories import providers as providers_repo
from ..timeutil import duration_text
from ..web import partial, redirect, render

router = APIRouter()


def _llm_status(session: Session) -> dict:
    """Whether any AI provider is ready, for the dashboard banner."""
    default = providers_repo.default_provider(session)
    usable = providers_repo.configured_providers(session)
    return {
        "configured": bool(usable),
        "default_name": default.display_name if default else "",
        "default_model": default.default_model if default else "",
        "count": len(usable),
    }


def _module_health(session: Session, last_run) -> list[dict]:
    """Every enabled module with the state it reached in the last run.

    A module that has never run reports as pending rather than inventing a
    healthy-looking status for it.
    """
    by_module = {m.module_id: m for m in (last_run.module_runs if last_run else [])}
    health: list[dict] = []
    for module in modules_repo.list_enabled_modules(session):
        module_run = by_module.get(module.id)
        health.append(
            {
                "name": module.name,
                "id": module.id,
                "status": module_run.status if module_run else "pending",
                "articles": module_run.article_count if module_run else 0,
                "findings": module_run.selected_count if module_run else 0,
                "topics": len(module.active_topics),
                "queries": module.query_count,
            }
        )
    return health


def _recent_findings(report, limit: int = 8) -> list[dict]:
    """Flatten the latest report into scannable rows, most important first."""
    if report is None:
        return []
    rows: list[dict] = []
    for section in report.sections:
        for item in section.items:
            rows.append(
                {
                    "item": item,
                    "module_name": section.module_name,
                    "event_id": item.event_id,
                    "state": item.event_state or "updated",
                    "importance": item.importance or 3,
                    "source": _first_source(item),
                }
            )
    rows.sort(key=lambda r: (0 if r["state"] == "new" else 1, r["importance"]))
    return rows[:limit]


def _first_source(item) -> str:
    observation = item.observation
    if observation is None:
        return ""
    for link in observation.sources:
        if link.article is not None:
            return link.article.source or link.article.domain or ""
    return ""


def _source_count(item) -> int:
    observation = item.observation
    if observation is None:
        return 0
    return len([link for link in observation.sources if link.article is not None])


def dashboard_context(session: Session) -> dict:
    """The Overview page context.

    Public because ``GET /`` is mode-aware and lives in
    :mod:`aios.routers.simple`: in 本地专业版 that handler renders this exact
    context, so there is one Overview implementation rather than two that can
    drift apart.
    """
    last_run = runs_repo.latest_run(session)
    latest_report = reports_repo.latest_report(session)

    daily_diff = None

    usage = runs_repo.usage_summary(session, last_run.id) if last_run else {}
    full_report = None
    if latest_report is not None:
        full_report = reports_repo.get_report(session, latest_report.id)
        if full_report is not None:
            daily_diff = diff_engine.diff_against_previous(session, full_report)

    return {
        "nav": "dashboard",
        "last_run": last_run,
        "module_runs": last_run.module_runs if last_run else [],
        "module_health": _module_health(session, last_run),
        "recent_findings": _recent_findings(full_report),
        "source_count": _source_count,
        "run_duration": duration_text(last_run.started_at, last_run.finished_at)
        if last_run
        else "",
        "latest_report": latest_report,
        "daily_diff": daily_diff,
        "usage": usage,
        "totals": {
            "modules": modules_repo.count_modules(session),
            "events": events_repo.count_events(session),
            "reports": reports_repo.count_reports(session),
            "articles": articles_repo.total_count(session),
            "runs": runs_repo.count_runs(session),
        },
        "llm": _llm_status(session),
        "scheduler": scheduler.status(),
        "busy": manager.is_busy(),
    }


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_db)):
    """The Professional-mode Overview.

    ``/`` also renders this whenever 本地专业版 is the active mode; this route
    is the stable direct address for it.
    """
    return render(request, "dashboard.html", dashboard_context(session))


@router.get("/dashboard/status")
def dashboard_status(request: Request, session: Session = Depends(get_db)):
    """HTMX fragment polled while a run is active."""
    last_run = runs_repo.latest_run(session)
    return partial(
        request,
        "partials/run_status.html",
        {
            "run": last_run,
            "module_runs": last_run.module_runs if last_run else [],
            "poll": bool(last_run and last_run.status in RunStatus.ACTIVE),
            # The Overview lists module health separately; keep this panel short.
            "compact": True,
            "duration": duration_text(last_run.started_at, last_run.finished_at)
            if last_run
            else "",
        },
    )


@router.post("/run")
def start_monitoring(session: Session = Depends(get_db)):
    """Queue a manual monitoring run and return to the dashboard."""
    if not _llm_status(session)["configured"]:
        return redirect(
            "/settings/ai",
            "请先配置 AI 模型服务（API Key 与模型名称），然后再启动监测。",
            "warn",
        )
    try:
        run_id = manager.start_run(trigger_type="manual")
    except RunAlreadyActive as exc:
        return redirect(f"/runs/{exc.run_id}", "已有监测任务正在执行。", "warn")
    return redirect(f"/runs/{run_id}", f"监测任务 #{run_id} 已启动。", "ok")
