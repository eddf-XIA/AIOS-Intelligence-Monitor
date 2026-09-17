"""AI provider configuration and task routing.

Security invariants, unchanged from the single-provider version and now applied
per provider: the credential goes from the form straight to the OS keyring and
is dropped; only ``masked``/``last_four`` ever reach the browser; test-connection
errors are scrubbed before display.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..repositories import providers as providers_repo
from ..schemas.providers import ProviderForm, ProviderKeyForm
from ..services import keyring_service
from ..services.llm.presets import (
    chinese_presets,
    international_presets,
    preset_or_custom,
)
from ..services.llm.service import OVERRIDABLE_TASKS, TASKS, LLMService, test_provider_row
from ..services.provider_migration import sync_key_state
from ..web import redirect, render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/ai")


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = error.get("loc", ["字段"])[0]
    return f"{field}: {error.get('msg', '输入无效')}"


def _checkbox(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "on", "yes"}


def _routing_summary(session: Session) -> dict:
    """What each task would use right now, resolved through the live router."""
    service = LLMService()
    try:
        service.load()
        resolved = service.describe_routing()
    except Exception:  # pragma: no cover - display only
        resolved = {}
    return {
        "resolved": resolved,
        "routes": providers_repo.routes_by_task(session),
    }


@router.get("")
def providers_home(request: Request, session: Session = Depends(get_db)):
    """The AI Providers page."""
    configured = providers_repo.list_providers(session)
    configured_ids = {row.provider_id for row in configured}
    routing = _routing_summary(session)

    return render(
        request,
        "providers.html",
        {
            "nav": "settings",
            "providers": configured,
            "presets": {p.provider_id: p for p in chinese_presets() + international_presets()},
            "chinese_presets": [p for p in chinese_presets() if p.provider_id not in configured_ids],
            "international_presets": [
                p for p in international_presets() if p.provider_id not in configured_ids
            ],
            "all_chinese": chinese_presets(),
            "all_international": international_presets(),
            "tasks": TASKS,
            "overridable_tasks": OVERRIDABLE_TASKS,
            "routing": routing,
            "keyring_backend": keyring_service.backend_name(),
        },
    )


@router.post("/providers")
def upsert_provider(
    provider_id: str = Form(...),
    display_name: str = Form(""),
    base_url: str = Form(""),
    default_model: str = Form(""),
    enabled: str = Form(None),
    temperature: float = Form(0.2),
    max_tokens: int = Form(4000),
    timeout_seconds: int = Form(180),
    retries: int = Form(3),
    api_key: str = Form(""),
    session: Session = Depends(get_db),
):
    """Create or update a provider, optionally storing a credential with it."""
    try:
        form = ProviderForm(
            provider_id=provider_id,
            display_name=display_name,
            base_url=base_url,
            default_model=default_model,
            enabled=_checkbox(enabled) if enabled is not None else True,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
    except ValidationError as exc:
        return redirect("/settings/ai", _first_error(exc), "error")

    preset = preset_or_custom(form.provider_id)
    row = providers_repo.get_by_provider_id(session, form.provider_id)
    created = row is None

    if created:
        row = providers_repo.create_provider(
            session,
            provider_id=form.provider_id,
            display_name=form.display_name or preset.display_name,
            base_url=form.base_url or preset.default_base_url,
            default_model=form.default_model,
            enabled=form.enabled,
            temperature=form.temperature,
            max_tokens=form.max_tokens,
            timeout_seconds=form.timeout_seconds,
            retries=form.retries,
        )
        # The first provider a user configures becomes the default.
        if providers_repo.count_providers(session) == 1:
            providers_repo.set_default(session, row)
    else:
        row.display_name = form.display_name or preset.display_name
        row.base_url = form.base_url or preset.default_base_url
        row.default_model = form.default_model
        row.enabled = form.enabled
        row.temperature = form.temperature
        row.max_tokens = form.max_tokens
        row.timeout_seconds = form.timeout_seconds
        row.retries = form.retries
        session.flush()

    # An optional key supplied on the same form goes straight to the vault.
    if api_key.strip():
        try:
            key_form = ProviderKeyForm(api_key=api_key)
        except ValidationError as exc:
            return redirect("/settings/ai", _first_error(exc), "error")
        try:
            keyring_service.set_provider_key(form.provider_id, key_form.api_key)
        except Exception as exc:
            logger.error("Could not store credential: %s", type(exc).__name__)
            return redirect("/settings/ai", "无法写入系统凭据存储，请检查权限。", "error")
        sync_key_state(session, form.provider_id)

    action = "已添加" if created else "已更新"
    return redirect("/settings/ai", f"{row.display_name} {action}。", "ok")


@router.post("/providers/{config_id}/key")
def save_key(config_id: int, api_key: str = Form(...), session: Session = Depends(get_db)):
    """Store one provider's credential in the OS keyring."""
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        form = ProviderKeyForm(api_key=api_key)
    except ValidationError as exc:
        return redirect("/settings/ai", _first_error(exc), "error")

    try:
        keyring_service.set_provider_key(row.provider_id, form.api_key)
    except Exception as exc:
        logger.error("Could not store credential: %s", type(exc).__name__)
        return redirect("/settings/ai", "无法写入系统凭据存储，请检查权限。", "error")

    sync_key_state(session, row.provider_id)
    return redirect(
        "/settings/ai",
        f"{row.display_name} 的 API Key 已保存到 {keyring_service.backend_name()}。",
        "ok",
    )


@router.post("/providers/{config_id}/key/delete")
def delete_key(config_id: int, session: Session = Depends(get_db)):
    """Remove one provider's credential, leaving the others untouched."""
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    removed = keyring_service.delete_provider_key(row.provider_id)
    sync_key_state(session, row.provider_id)
    message = (
        f"{row.display_name} 的 API Key 已删除。" if removed else "凭据存储中没有该 Key。"
    )
    return redirect("/settings/ai", message, "ok" if removed else "warn")


@router.post("/providers/{config_id}/test")
def test_provider(config_id: int, session: Session = Depends(get_db)):
    """Send a minimal request and report the outcome."""
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    result = test_provider_row(row)
    if result.get("ok"):
        return redirect(
            "/settings/ai",
            f"连接成功 · {row.display_name} · 模型 {result.get('model')} "
            f"· 响应 {result.get('latency_ms')} ms",
            "ok",
        )
    return redirect(
        "/settings/ai", f"{row.display_name} 连接失败：{result.get('error', '未知错误')}", "error"
    )


@router.post("/providers/{config_id}/default")
def set_default(config_id: int, session: Session = Depends(get_db)):
    """Make this provider serve every task that has no override."""
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    providers_repo.set_default(session, row)
    return redirect("/settings/ai", f"已将 {row.display_name} 设为默认模型服务。", "ok")


@router.post("/providers/{config_id}/toggle")
def toggle_provider(config_id: int, session: Session = Depends(get_db)):
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    row.enabled = not row.enabled
    session.flush()
    state = "已启用" if row.enabled else "已停用"
    return redirect("/settings/ai", f"{row.display_name} {state}。", "ok")


@router.post("/providers/{config_id}/delete")
def delete_provider(config_id: int, session: Session = Depends(get_db)):
    """Remove a provider configuration and its stored credential."""
    row = providers_repo.get_provider(session, config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    name = row.display_name
    keyring_service.delete_provider_key(row.provider_id)
    providers_repo.delete_provider(session, row)
    return redirect("/settings/ai", f"已删除 {name} 的配置与凭据。", "ok")


@router.post("/routes")
def save_routes(
    route_topic_analysis: str = Form(""),
    model_topic_analysis: str = Form(""),
    route_event_matching: str = Form(""),
    model_event_matching: str = Form(""),
    route_synthesis: str = Form(""),
    model_synthesis: str = Form(""),
    session: Session = Depends(get_db),
):
    """Save the per-task provider/model overrides.

    An empty provider field means "use the default provider", which is stored as
    the absence of a route rather than as a row pointing at today's default -
    otherwise changing the default would silently not apply to that task.
    """
    submitted = {
        "topic_analysis": (route_topic_analysis, model_topic_analysis),
        "event_matching": (route_event_matching, model_event_matching),
        "synthesis": (route_synthesis, model_synthesis),
    }

    valid_ids = {row.id for row in providers_repo.list_providers(session)}
    changed = 0

    for task, (raw_id, model) in submitted.items():
        if task not in TASKS:
            continue
        raw_id = (raw_id or "").strip()
        if not raw_id or raw_id == "0":
            providers_repo.clear_route(session, task)
            changed += 1
            continue
        try:
            config_id = int(raw_id)
        except ValueError:
            return redirect("/settings/ai", f"{TASKS[task]}：无效的服务商选择。", "error")
        if config_id not in valid_ids:
            return redirect("/settings/ai", f"{TASKS[task]}：所选服务商不存在。", "error")
        providers_repo.set_route(session, task, config_id, model)
        changed += 1

    return redirect("/settings/ai", f"已保存 {changed} 项任务模型配置。", "ok")
