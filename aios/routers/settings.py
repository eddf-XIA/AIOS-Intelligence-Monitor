"""Settings: scheduler, collection tuning, Windows auto-start, import, backup.

AI provider credentials and model routing live on their own page - see
:mod:`aios.routers.providers`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import get_paths
from ..database import get_db
from ..repositories import reports as reports_repo
from ..schemas.settings import CollectionSettingsForm, SchedulerForm
from ..repositories import providers as providers_repo
from ..services import backup as backup_service
from ..services import importer, network_service, settings_service, startup_service
from ..services.scheduler import scheduler
from ..timeutil import local_tz_name
from ..web import redirect, render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = error.get("loc", ["字段"])[0]
    return f"{field}: {error.get('msg', '输入无效')}"


def _context(session: Session, extra: dict | None = None) -> dict:
    values = settings_service.all_settings(session)
    context = {
        "nav": "settings",
        "providers": providers_repo.list_providers(session),
        "default_provider": providers_repo.default_provider(session),
        "settings": values,
        "startup": startup_service.get_status(),
        "scheduler": scheduler.status(),
        "timezone": local_tz_name(),
        "paths": {
            "data": str(get_paths().data_dir),
            "db": str(get_paths().db_path),
            "reports": str(get_paths().reports_dir),
            "logs": str(get_paths().logs_dir),
        },
        "backups": backup_service.list_backups(),
        "network": network_service.network_context(session),
        "network_compat": network_service.network_compatibility_label,
    }
    if extra:
        context.update(extra)
    return context


@router.get("")
def settings_home(request: Request, session: Session = Depends(get_db)):
    """The settings page."""
    return render(request, "settings.html", _context(session))


@router.post("/collection")
def save_collection(
    default_lookback_days: int = Form(3),
    default_max_candidates: int = Form(18),
    article_max_chars: int = Form(7000),
    http_timeout: int = Form(20),
    collect_workers: int = Form(6),
    fetch_body_top_n: int = Form(8),
    event_match_enabled: str = Form(None),
    event_match_lookback_days: int = Form(45),
    report_output_dir: str = Form(""),
    session: Session = Depends(get_db),
):
    """Save advanced collection/extraction options."""
    try:
        form = CollectionSettingsForm(
            default_lookback_days=default_lookback_days,
            default_max_candidates=default_max_candidates,
            article_max_chars=article_max_chars,
            http_timeout=http_timeout,
            collect_workers=collect_workers,
            fetch_body_top_n=fetch_body_top_n,
            event_match_enabled=str(event_match_enabled or "").lower() in {"on", "true", "1"},
            event_match_lookback_days=event_match_lookback_days,
            report_output_dir=report_output_dir,
        )
    except ValidationError as exc:
        return redirect("/settings", _first_error(exc), "error")

    if form.report_output_dir:
        try:
            Path(form.report_output_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return redirect("/settings", f"无法创建输出目录：{exc}", "error")

    settings_service.set_many(session, form.model_dump())
    return redirect("/settings", "采集设置已保存。", "ok")


# --- scheduler --------------------------------------------------------------

@router.post("/scheduler")
def save_scheduler(
    enabled: str = Form(None),
    time: str = Form("06:00"),
    session: Session = Depends(get_db),
):
    """Enable/disable automatic daily monitoring and set its time."""
    try:
        form = SchedulerForm(
            enabled=str(enabled or "").lower() in {"on", "true", "1"}, time=time
        )
    except ValidationError as exc:
        return redirect("/settings", _first_error(exc), "error")

    settings_service.set_many(
        session, {"scheduler_enabled": form.enabled, "scheduler_time": form.time}
    )
    session.commit()  # the scheduler reads settings in its own session

    next_run = scheduler.reload()
    if form.enabled:
        message = f"自动监测已开启，每天 {form.time} 执行。"
        if next_run:
            message += f" 下次执行：{next_run}"
    else:
        message = "自动监测已关闭。"
    return redirect("/settings", message, "ok")


# --- Windows auto-start -----------------------------------------------------

@router.post("/startup/enable")
def enable_startup():
    """Register the Windows logon task so AIOS starts itself after reboot."""
    status = startup_service.enable()
    if status.error:
        return redirect("/settings", f"开启自动启动失败：{status.error}", "error")
    return redirect(
        "/settings",
        "已开启 Windows 自动启动。下次登录时 AIOS 会自动运行（不打开浏览器）。",
        "ok",
    )


@router.post("/startup/disable")
def disable_startup():
    """Remove the Windows logon task."""
    status = startup_service.disable()
    if status.error:
        return redirect("/settings", f"关闭自动启动失败：{status.error}", "error")
    return redirect("/settings", "已关闭 Windows 自动启动。", "ok")


# --- historical import ------------------------------------------------------

@router.post("/import")
async def import_report(
    file: UploadFile = File(...),
    overwrite: str = Form(None),
    session: Session = Depends(get_db),
):
    """Import a legacy ``AIOS监测日报-*.json`` audit file."""
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return redirect("/settings", f"文件不是合法的 UTF-8 JSON：{exc}", "error")

    if not isinstance(payload, dict):
        return redirect("/settings", "JSON 根节点必须是对象。", "error")
    if importer.detect_format(payload) == "unknown":
        return redirect("/settings", "无法识别的报告格式。", "error")

    result = importer.import_legacy_payload(
        session,
        payload,
        source_name=file.filename or "uploaded.json",
        overwrite=str(overwrite or "").lower() in {"on", "true", "1"},
    )
    if not result.ok:
        return redirect("/settings", result.summary(), "error")

    session.flush()
    stored = reports_repo.get_report(session, result.report_id)
    if stored is not None and not result.skipped:
        from ..services.report_generator import export_report

        try:
            export_report(session, stored)
        except Exception as exc:
            logger.warning("Import export failed: %s", exc)

    return redirect(
        f"/reports/{result.report_id}",
        result.summary(),
        "warn" if result.skipped else "ok",
    )


# --- backup -----------------------------------------------------------------

@router.post("/backup")
def create_backup(session: Session = Depends(get_db)):
    """Create a ZIP containing the database and the exported reports."""
    try:
        archive = backup_service.create_backup()
    except Exception as exc:
        logger.exception("Backup failed")
        return redirect("/settings", f"备份失败：{exc}", "error")
    return redirect("/settings", f"已创建备份 {archive.name}", "ok")


@router.get("/backup/{name}")
def download_backup(name: str):
    """Download a previously created backup archive."""
    # Reject anything that is not a plain filename from our own backup folder.
    safe_name = Path(name).name
    if not safe_name.startswith("aios-backup-") or not safe_name.endswith(".zip"):
        return redirect("/settings", "无效的备份文件名。", "error")

    path = (get_paths().data_dir / "backups" / safe_name).resolve()
    backups_root = (get_paths().data_dir / "backups").resolve()
    if backups_root not in path.parents or not path.exists():
        return redirect("/settings", "备份文件不存在。", "error")

    return FileResponse(path, filename=safe_name, media_type="application/zip")
