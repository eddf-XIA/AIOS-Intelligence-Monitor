"""AI-assisted monitoring configuration.

Two entry points, both optional accelerators on top of the manual editor:

* ``/monitoring/ai/new`` - one sentence becomes a whole module draft.
* ``/monitoring/modules/{id}/ai/topic`` - one sentence becomes one extra topic
  inside a module the user is already editing.

The flow is always describe → generate → **preview** → edit → apply. Nothing is
written to the database until the user posts the apply form, and the apply is
transactional: a draft that fails validation halfway leaves nothing behind.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..repositories import modules as modules_repo
from ..schemas.config_generation import (
    GeneratedMonitorConfig,
    GeneratedTopic,
    describe_limits,
    first_error,
)
from ..services import config_diagnostics, config_generator
from ..services.config_generator import (
    NO_PROVIDER_MESSAGE,
    ApplyError,
    ConfigGenerationError,
)
from ..web import partial, redirect, render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring/ai")

#: Shown when generation is unavailable because nothing is configured yet.
SETTINGS_LINK = "/settings/ai"


EXAMPLES = [
    "帮我监测中国人形机器人操作系统、ROS 2、具身智能基础模型和主要厂商的最新动态。",
    "监测中国和美国商业航天公司在太空算力、卫星边缘 AI 和星载操作系统方面的进展。",
    "关注国产 AI PC 操作系统、端侧模型、NPU 和 Windows Copilot Runtime 的动态。",
]


def _readiness(session: Session) -> Optional[str]:
    """'' / None when generation can run; otherwise the reason it cannot."""
    from ..repositories import providers as providers_repo

    if not providers_repo.list_providers(session, enabled_only=True):
        return NO_PROVIDER_MESSAGE
    try:
        generator = config_generator.ConfigGenerator()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not build config generator: %s", exc)
        return NO_PROVIDER_MESSAGE
    return generator.readiness_error() or None


# --- module generation ------------------------------------------------------

@router.get("/new")
def ai_new_module(request: Request, session: Session = Depends(get_db)):
    """The 'describe it in one sentence' page."""
    return render(
        request,
        "ai_config.html",
        {
            "nav": "monitoring",
            "mode": "module",
            "examples": EXAMPLES,
            "limits": describe_limits(),
            "readiness_error": _readiness(session),
            "settings_link": SETTINGS_LINK,
            "action": "/monitoring/ai/generate",
            "title": "AI 创建监测模块",
            "subtitle": "用一句话描述你想监测什么，AI 会生成完整的模块草稿供你确认。",
        },
    )


@router.post("/generate")
def generate_module(
    request: Request,
    description: str = Form(""),
    session: Session = Depends(get_db),
):
    """Generate a draft and render the preview. Writes nothing."""
    error = _readiness(session)
    if error:
        return partial(
            request,
            "partials/config_draft.html",
            {"error": error, "settings_link": SETTINGS_LINK, "mode": "module"},
        )

    existing_keys = [m.key for m in modules_repo.list_modules(session, include_archived=True)]
    try:
        generator = config_generator.ConfigGenerator()
        result = generator.generate_module(description, existing_keys=existing_keys)
    except ConfigGenerationError as exc:
        return partial(
            request,
            "partials/config_draft.html",
            {"error": str(exc), "mode": "module", "description": description},
        )

    config_generator.record_usage(session, result.usage)

    config = result.config
    assert config is not None
    return partial(
        request,
        "partials/config_draft.html",
        {
            "mode": "module",
            "draft": config,
            "description": description,
            "notes": result.notes,
            "warnings": result.warnings,
            "diagnostics": config_diagnostics.diagnose_generated(config),
            "estimated_requests": config_diagnostics.estimated_requests(
                config.total_queries, collectors=1
            ),
            "provider_label": f"{result.provider_id} / {result.model}" if result.model else "",
            "apply_action": "/monitoring/ai/apply",
        },
    )


@router.post("/apply")
async def apply_module(request: Request, session: Session = Depends(get_db)):
    """Apply the (possibly edited) draft in one transaction."""
    form = await request.form()
    try:
        config = _config_from_form(form)
    except ValidationError as exc:
        return redirect("/monitoring/ai/new", f"配置校验失败：{first_error(exc)}", "error")
    except ValueError as exc:
        return redirect("/monitoring/ai/new", str(exc), "error")

    try:
        result = config_generator.apply_module_draft(session, config)
        session.flush()
    except ApplyError as exc:
        # Nothing is committed until the request session commits, so discarding
        # the work here leaves no half-created module behind.
        session.rollback()
        return redirect("/monitoring/ai/new", str(exc), "error")
    except Exception as exc:  # pragma: no cover - defensive
        session.rollback()
        logger.exception("Applying generated config failed")
        return redirect("/monitoring/ai/new", f"应用配置失败：{exc}", "error")

    return redirect(f"/monitoring/modules/{result.module_id}", result.message, "ok")


# --- topic generation inside an existing module -----------------------------

@router.get("/modules/{module_id}/topic")
def ai_new_topic(module_id: int, request: Request, session: Session = Depends(get_db)):
    """The 'add one topic by describing it' page for an existing module."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    return render(
        request,
        "ai_config.html",
        {
            "nav": "monitoring",
            "mode": "topic",
            "module": module,
            "examples": ["再加一个关注商业空间站边缘计算和自主任务规划的主题。"],
            "limits": describe_limits(),
            "readiness_error": _readiness(session),
            "settings_link": SETTINGS_LINK,
            "action": f"/monitoring/ai/modules/{module_id}/topic/generate",
            "title": f"AI 添加主题 · {module.name}",
            "subtitle": "只会新增一个主题，模块的其他主题与设置不受影响。",
        },
    )


@router.post("/modules/{module_id}/topic/generate")
def generate_topic(
    module_id: int,
    request: Request,
    description: str = Form(""),
    session: Session = Depends(get_db),
):
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    error = _readiness(session)
    if error:
        return partial(
            request,
            "partials/config_draft.html",
            {"error": error, "settings_link": SETTINGS_LINK, "mode": "topic"},
        )

    existing = [t.name for t in module.topics if not t.archived]
    try:
        generator = config_generator.ConfigGenerator()
        result = generator.generate_topic(
            description,
            module_name=module.name,
            module_description=module.description or "",
            existing_topics=existing,
        )
    except ConfigGenerationError as exc:
        return partial(
            request,
            "partials/config_draft.html",
            {"error": str(exc), "mode": "topic", "description": description},
        )

    config_generator.record_usage(session, result.usage)

    topic = result.topic
    assert topic is not None
    query_count = len(topic.queries)
    return partial(
        request,
        "partials/config_draft.html",
        {
            "mode": "topic",
            "module": module,
            "topic": topic,
            "description": description,
            "notes": result.notes,
            "warnings": result.warnings,
            "diagnostics": config_diagnostics.analyze_queries(
                [config_diagnostics.QuerySpec(query=q.query, topic_name=topic.name)
                 for q in topic.queries]
            ),
            "estimated_requests": config_diagnostics.estimated_requests(query_count),
            "provider_label": f"{result.provider_id} / {result.model}" if result.model else "",
            "apply_action": f"/monitoring/ai/modules/{module_id}/topic/apply",
        },
    )


@router.post("/modules/{module_id}/topic/apply")
async def apply_topic(module_id: int, request: Request, session: Session = Depends(get_db)):
    """Add exactly the previewed topic. Existing topics are never touched."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    form = await request.form()
    back = f"/monitoring/ai/modules/{module_id}/topic"
    try:
        topic = _topic_from_form(form, index=0)
    except ValidationError as exc:
        return redirect(back, f"主题校验失败：{first_error(exc)}", "error")
    except ValueError as exc:
        return redirect(back, str(exc), "error")

    try:
        result = config_generator.apply_topic_draft(session, module_id, topic)
        session.flush()
    except ApplyError as exc:
        session.rollback()
        return redirect(back, str(exc), "error")
    except Exception as exc:  # pragma: no cover - defensive
        session.rollback()
        logger.exception("Applying generated topic failed")
        return redirect(back, f"应用主题失败：{exc}", "error")

    return redirect(f"/monitoring/modules/{module_id}", result.message, "ok")


# --- form <-> draft ---------------------------------------------------------

def _lines(value: str, limit: int = 40) -> list[str]:
    """One item per line, blank lines ignored."""
    return [line.strip() for line in str(value or "").splitlines() if line.strip()][:limit]


def _topic_from_form(form, index: int) -> GeneratedTopic:
    """Rebuild one topic from the editable preview fields."""
    name = str(form.get(f"topic_name_{index}") or "").strip()
    if not name:
        raise ValueError("主题名称不能为空。")

    queries = [{"query": q, "priority": 5} for q in _lines(form.get(f"topic_queries_{index}", ""))]
    sources = [{"domain": d, "reason": ""} for d in _lines(form.get(f"topic_sources_{index}", ""))]

    return GeneratedTopic.model_validate(
        {
            "name": name,
            "description": str(form.get(f"topic_description_{index}") or "").strip(),
            "queries": queries,
            "preferred_sources": sources,
            "excluded_keywords": _lines(form.get(f"topic_excluded_{index}", "")),
            "analysis_instructions": str(
                form.get(f"topic_instructions_{index}") or ""
            ).strip(),
        }
    )


def _config_from_form(form) -> GeneratedMonitorConfig:
    """Rebuild the module draft from the editable preview fields.

    Re-validating here rather than trusting a hidden JSON blob is what makes
    "user edits one query, then clicks 应用配置" safe: the edited values go
    through exactly the same schema, budget caps and domain checks as the
    generated ones.
    """
    try:
        topic_count = int(form.get("topic_count") or 0)
    except (TypeError, ValueError):
        topic_count = 0
    if topic_count <= 0:
        raise ValueError("草稿已失效，请重新生成。")

    topics = []
    for index in range(topic_count):
        if form.get(f"topic_enabled_{index}") in (None, "", "0", "off"):
            continue
        topics.append(_topic_from_form(form, index).model_dump(mode="json"))

    if not topics:
        raise ValueError("至少要保留一个主题。")

    def _int(field: str, default: int) -> int:
        try:
            return int(form.get(field) or default)
        except (TypeError, ValueError):
            return default

    return GeneratedMonitorConfig.model_validate(
        {
            "name": str(form.get("module_name") or "").strip(),
            "key": str(form.get("module_key") or "").strip(),
            "description": str(form.get("module_description") or "").strip(),
            "topics": topics,
            "recommended_lookback_days": _int("lookback_days", 3),
            "recommended_report_limit": _int("max_report_items", 2),
            "analysis_instructions": str(form.get("module_instructions") or "").strip(),
        }
    )
