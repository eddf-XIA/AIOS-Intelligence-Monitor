"""The monitoring configuration editor.

Every change here takes effect on the next run with no code edit - that is the
core promise of this application, so all of these endpoints are real CRUD, not
placeholders.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitorModule, SearchQuery, Topic
from ..repositories import modules as modules_repo
from ..repositories import topics as topics_repo
from ..services import config_diagnostics
from ..schemas.monitoring import (
    ExcludedKeywordForm,
    ModuleForm,
    PreferredSourceForm,
    QueryForm,
    TopicForm,
)
from ..web import redirect, render

router = APIRouter(prefix="/monitoring")


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = error.get("loc", ["字段"])[0]
    return f"{field}: {error.get('msg', '输入无效')}"


def _checkbox(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "on", "yes"}


# --- module list ------------------------------------------------------------

@router.get("")
def monitoring_home(request: Request, session: Session = Depends(get_db)):
    """All modules with their topic/query counts."""
    modules = modules_repo.list_modules(session, include_archived=False)
    archived = [
        m for m in modules_repo.list_modules(session, include_archived=True) if m.archived
    ]
    return render(
        request,
        "monitoring.html",
        {"nav": "monitoring", "modules": modules, "archived": archived},
    )


@router.post("/modules")
def create_module(
    request: Request,
    key: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    lookback_days: int = Form(3),
    max_candidates: int = Form(18),
    max_report_items: int = Form(2),
    session: Session = Depends(get_db),
):
    """Add a new monitoring module."""
    try:
        form = ModuleForm(
            key=key,
            name=name,
            description=description,
            lookback_days=lookback_days,
            max_candidates=max_candidates,
            max_report_items=max_report_items,
        )
    except ValidationError as exc:
        return redirect("/monitoring", _first_error(exc), "error")

    if modules_repo.key_exists(session, form.key):
        return redirect("/monitoring", f"模块标识 '{form.key}' 已存在。", "error")

    module = modules_repo.create_module(
        session,
        key=form.key,
        name=form.name,
        description=form.description,
        enabled=True,
        sort_order=modules_repo.next_sort_order(session),
        lookback_days=form.lookback_days,
        max_candidates=form.max_candidates,
        max_report_items=form.max_report_items,
        analysis_prompt="",
    )
    return redirect(f"/monitoring/modules/{module.id}", f"已创建模块「{form.name}」。", "ok")


# --- module detail ----------------------------------------------------------

@router.get("/modules/{module_id}")
def module_edit(module_id: int, request: Request, session: Session = Depends(get_db)):
    """Edit one module and manage its topics."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    return render(
        request,
        "module_edit.html",
        {
            "nav": "monitoring",
            "module": module,
            "topics": [t for t in module.topics if not t.archived],
            # Cost and quality findings are advisory: they make an expensive
            # configuration visible without preventing anyone from saving it.
            "diagnostics": config_diagnostics.diagnose_module(module),
        },
    )


@router.post("/modules/{module_id}")
def module_update(
    module_id: int,
    name: str = Form(...),
    key: str = Form(...),
    description: str = Form(""),
    enabled: str = Form(None),
    lookback_days: int = Form(3),
    max_candidates: int = Form(18),
    max_report_items: int = Form(2),
    analysis_prompt: str = Form(""),
    sort_order: int = Form(0),
    session: Session = Depends(get_db),
):
    """Save module settings."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    try:
        form = ModuleForm(
            key=key,
            name=name,
            description=description,
            enabled=_checkbox(enabled),
            lookback_days=lookback_days,
            max_candidates=max_candidates,
            max_report_items=max_report_items,
            analysis_prompt=analysis_prompt,
            sort_order=sort_order,
        )
    except ValidationError as exc:
        return redirect(f"/monitoring/modules/{module_id}", _first_error(exc), "error")

    if modules_repo.key_exists(session, form.key, exclude_id=module_id):
        return redirect(
            f"/monitoring/modules/{module_id}", f"模块标识 '{form.key}' 已被占用。", "error"
        )

    module.key = form.key
    module.name = form.name
    module.description = form.description
    module.enabled = form.enabled
    module.lookback_days = form.lookback_days
    module.max_candidates = form.max_candidates
    module.max_report_items = form.max_report_items
    module.analysis_prompt = form.analysis_prompt
    module.sort_order = form.sort_order
    session.flush()
    return redirect(f"/monitoring/modules/{module_id}", "模块已保存。", "ok")


@router.post("/modules/{module_id}/toggle")
def module_toggle(module_id: int, session: Session = Depends(get_db)):
    """Enable/disable a module without leaving the list page."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    module.enabled = not module.enabled
    session.flush()
    state = "已启用" if module.enabled else "已停用"
    return redirect("/monitoring", f"「{module.name}」{state}。", "ok")


@router.post("/modules/{module_id}/move")
def module_move(module_id: int, direction: str = Form("up"), session: Session = Depends(get_db)):
    """Swap sort order with the neighbouring module."""
    modules = modules_repo.list_modules(session)
    index = next((i for i, m in enumerate(modules) if m.id == module_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="Module not found")

    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(modules):
        current, other = modules[index], modules[target]
        current.sort_order, other.sort_order = other.sort_order, current.sort_order
        session.flush()
    return redirect("/monitoring", "", "ok")


@router.post("/modules/{module_id}/archive")
def module_archive(module_id: int, session: Session = Depends(get_db)):
    """Soft delete so existing reports keep rendering."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    modules_repo.archive_module(session, module)
    return redirect("/monitoring", f"「{module.name}」已归档，历史报告不受影响。", "ok")


@router.post("/modules/{module_id}/restore")
def module_restore(module_id: int, session: Session = Depends(get_db)):
    """Bring an archived module back."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    modules_repo.restore_module(session, module)
    return redirect("/monitoring", f"「{module.name}」已恢复。", "ok")


@router.post("/modules/{module_id}/sources")
def module_add_source(
    module_id: int, domain: str = Form(...), session: Session = Depends(get_db)
):
    """Add a preferred domain for the whole module."""
    try:
        form = PreferredSourceForm(domain=domain)
    except ValidationError as exc:
        return redirect(f"/monitoring/modules/{module_id}", _first_error(exc), "error")
    topics_repo.add_preferred_source(session, form.domain, module_id=module_id, priority=form.priority)
    return redirect(f"/monitoring/modules/{module_id}", f"已添加优先来源 {form.domain}。", "ok")


@router.post("/sources/{source_id}/delete")
def delete_source(source_id: int, back: str = Form("/monitoring"), session: Session = Depends(get_db)):
    """Remove a preferred domain."""
    row = topics_repo.get_preferred_source(session, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    topics_repo.delete_preferred_source(session, row)
    return redirect(back, "已删除优先来源。", "ok")


# --- topics -----------------------------------------------------------------

@router.post("/modules/{module_id}/topics")
def create_topic(
    module_id: int,
    name: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_db),
):
    """Add a topic to a module."""
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    try:
        form = TopicForm(name=name, description=description)
    except ValidationError as exc:
        return redirect(f"/monitoring/modules/{module_id}", _first_error(exc), "error")

    duplicate = any(
        t.name.lower() == form.name.lower() and not t.archived for t in module.topics
    )
    if duplicate:
        return redirect(
            f"/monitoring/modules/{module_id}", f"主题「{form.name}」已存在。", "error"
        )

    topic = topics_repo.create_topic(
        session,
        module_id=module_id,
        name=form.name,
        description=form.description,
        enabled=True,
        sort_order=topics_repo.next_sort_order(session, module_id),
        analysis_prompt="",
    )
    return redirect(f"/monitoring/topics/{topic.id}", f"已创建主题「{form.name}」。", "ok")


@router.get("/topics/{topic_id}")
def topic_edit(topic_id: int, request: Request, session: Session = Depends(get_db)):
    """Edit a topic, its queries and its preferred sources."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return render(request, "topic_edit.html", {"nav": "monitoring", "topic": topic})


@router.post("/topics/{topic_id}")
def topic_update(
    topic_id: int,
    name: str = Form(...),
    description: str = Form(""),
    enabled: str = Form(None),
    analysis_prompt: str = Form(""),
    session: Session = Depends(get_db),
):
    """Save topic settings."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    try:
        form = TopicForm(
            name=name,
            description=description,
            enabled=_checkbox(enabled),
            analysis_prompt=analysis_prompt,
        )
    except ValidationError as exc:
        return redirect(f"/monitoring/topics/{topic_id}", _first_error(exc), "error")

    topic.name = form.name
    topic.description = form.description
    topic.enabled = form.enabled
    topic.analysis_prompt = form.analysis_prompt
    session.flush()
    return redirect(f"/monitoring/topics/{topic_id}", "主题已保存。", "ok")


@router.post("/topics/{topic_id}/toggle")
def topic_toggle(topic_id: int, session: Session = Depends(get_db)):
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.enabled = not topic.enabled
    session.flush()
    state = "已启用" if topic.enabled else "已停用"
    return redirect(f"/monitoring/modules/{topic.module_id}", f"「{topic.name}」{state}。", "ok")


@router.post("/topics/{topic_id}/archive")
def topic_archive(topic_id: int, session: Session = Depends(get_db)):
    """Soft delete a topic; historical events keep pointing at it."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    module_id = topic.module_id
    topics_repo.archive_topic(session, topic)
    return redirect(f"/monitoring/modules/{module_id}", f"「{topic.name}」已归档。", "ok")


@router.post("/topics/{topic_id}/sources")
def topic_add_source(topic_id: int, domain: str = Form(...), session: Session = Depends(get_db)):
    """Add a preferred domain for this topic only."""
    try:
        form = PreferredSourceForm(domain=domain)
    except ValidationError as exc:
        return redirect(f"/monitoring/topics/{topic_id}", _first_error(exc), "error")
    topics_repo.add_preferred_source(
        session, form.domain, topic_id=topic_id, priority=form.priority
    )
    return redirect(f"/monitoring/topics/{topic_id}", f"已添加优先来源 {form.domain}。", "ok")


@router.post("/topics/{topic_id}/exclusions")
def topic_add_exclusion(topic_id: int, keyword: str = Form(...), session: Session = Depends(get_db)):
    """Add a keyword that disqualifies results for this topic."""
    try:
        form = ExcludedKeywordForm(keyword=keyword)
    except ValidationError as exc:
        return redirect(f"/monitoring/topics/{topic_id}", _first_error(exc), "error")
    topics_repo.add_excluded_keyword(session, form.keyword, topic_id=topic_id)
    return redirect(f"/monitoring/topics/{topic_id}", f"已添加排除词「{form.keyword}」。", "ok")


@router.post("/exclusions/{keyword_id}/delete")
def delete_exclusion(
    keyword_id: int, back: str = Form("/monitoring"), session: Session = Depends(get_db)
):
    row = topics_repo.get_excluded_keyword(session, keyword_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Keyword not found")
    topics_repo.delete_excluded_keyword(session, row)
    return redirect(back, "已删除排除词。", "ok")


# --- search queries ---------------------------------------------------------

@router.post("/topics/{topic_id}/queries")
def create_query(topic_id: int, query: str = Form(...), session: Session = Depends(get_db)):
    """Add a search query to a topic."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    try:
        form = QueryForm(query=query)
    except ValidationError as exc:
        return redirect(f"/monitoring/topics/{topic_id}", _first_error(exc), "error")

    if any(q.query.strip().lower() == form.query.lower() for q in topic.queries):
        return redirect(f"/monitoring/topics/{topic_id}", "该检索式已存在。", "warn")

    topics_repo.create_query(session, topic_id, form.query)
    session.flush()

    # Never block a valid configuration - just make its cost visible.
    module = modules_repo.get_module(session, topic.module_id)
    warning = config_diagnostics.summarize(config_diagnostics.diagnose_module(module))
    if warning:
        return redirect(f"/monitoring/topics/{topic_id}", f"已添加检索式。{warning}", "warn")
    return redirect(f"/monitoring/topics/{topic_id}", "已添加检索式。", "ok")


@router.post("/queries/{query_id}")
def update_query(
    query_id: int,
    query: str = Form(...),
    priority: int = Form(0),
    session: Session = Depends(get_db),
):
    """Edit an existing query."""
    row = topics_repo.get_query(session, query_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")
    try:
        form = QueryForm(query=query, priority=priority)
    except ValidationError as exc:
        return redirect(f"/monitoring/topics/{row.topic_id}", _first_error(exc), "error")
    row.query = form.query
    row.priority = form.priority
    session.flush()
    return redirect(f"/monitoring/topics/{row.topic_id}", "检索式已更新。", "ok")


@router.post("/queries/{query_id}/toggle")
def toggle_query(query_id: int, session: Session = Depends(get_db)):
    row = topics_repo.get_query(session, query_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")
    row.enabled = not row.enabled
    session.flush()
    return redirect(f"/monitoring/topics/{row.topic_id}", "", "ok")


@router.post("/queries/{query_id}/delete")
def delete_query(query_id: int, session: Session = Depends(get_db)):
    """Queries carry no history, so a hard delete is fine here."""
    row = topics_repo.get_query(session, query_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")
    topic_id = row.topic_id
    topics_repo.delete_query(session, row)
    return redirect(f"/monitoring/topics/{topic_id}", "已删除检索式。", "ok")
