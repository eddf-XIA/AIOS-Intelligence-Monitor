"""简易版: the one-page research workflow.

Everything a normal user needs happens on ``GET /``: connect an AI, pick or
describe a topic, let AI shape it, run, watch, read the result. The four
information-heavy views (full report, changes, sources, history) are separate
pages with an obvious way back.

Two deliberate choices worth knowing about:

* **Brief previews are never persisted.** A generated brief travels through
  hidden form fields and is re-validated by
  :class:`~aios.schemas.research_topic.ResearchBrief` on the way back in, so
  "AI 完善主题" writes nothing and an edited brief gets the same caps and
  cleaning as a generated one.
* **The active topic is remembered as a setting.** That is what lets the user
  open a report, read it, press 返回主页 and find their own context intact -
  rather than an empty page they have to rebuild.

This router contains no vendor names and no knowledge of which backend performs
research; it asks :mod:`aios.services.research.registry` and renders what it
gets back.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RUN_ENGINE_AGENT, RunStatus
from ..repositories import providers as providers_repo
from ..repositories import reports as reports_repo
from ..repositories import research_topics as topics_repo
from ..repositories import runs as runs_repo
from ..schemas.research import (
    COVERAGE_EMPTY_NOTICE,
    COVERAGE_PARTIAL,
    COVERAGE_PARTIAL_NOTICE,
)
from ..schemas.research_topic import BriefError, ResearchBrief, validate_brief
from ..services import keyring_service, mode_service, research_topic_service, settings_service
from ..services.research import (
    RESEARCH_STAGES,
    STAGE_ORDER,
    advanced_agents_for_provider_choice,
    agents_for_provider_choice,
    all_agents,
    default_agent_for_provider,
    engine_status,
    get_agent,
    set_selection,
    unavailable_reason_for_provider,
)
from ..services.research_pipeline import stage_of
from ..services.research_topic_service import (
    BriefGenerationError,
    ResearchTopicService,
    fallback_brief,
)
from ..services.run_manager import RunAlreadyActive, manager
from ..timeutil import duration_text
from ..web import redirect, render, partial

logger = logging.getLogger(__name__)

router = APIRouter()

#: Remembers which topic the user is working on, so navigating to a report and
#: back restores their context instead of an empty home page.
ACTIVE_TOPIC_KEY = "simple_active_topic_id"

#: Time windows offered in the editor. Hours, in user words.
WINDOW_CHOICES: tuple[tuple[int, str], ...] = (
    (24, "最近24小时"),
    (48, "最近48小时"),
    (72, "最近72小时"),
    (168, "最近7天"),
    (336, "最近14天"),
    (720, "最近30天"),
)


# --- shared state helpers ---------------------------------------------------

def _active_topic_id(session: Session) -> Optional[int]:
    raw = settings_service.get_str(session, ACTIVE_TOPIC_KEY, "").strip()
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


def _set_active_topic(session: Session, topic_id: Optional[int]) -> None:
    settings_service.set_value(session, ACTIVE_TOPIC_KEY, str(topic_id or ""))


def _brief_from_form(form) -> ResearchBrief:
    """Re-validate a brief coming back from the page.

    Goes through the same schema as a generated one - editing must not be a way
    around the caps and cleaning.
    """
    return validate_brief(
        {
            "name": form.get("name") or "",
            "brief": form.get("brief") or "",
            "scope": form.get("scope") or "",
            "focus_areas": form.get("focus_areas") or "",
            "exclusions": form.get("exclusions") or "",
            "keywords": form.get("keywords") or "",
            "regions": form.get("regions") or "",
            "window_hours": form.get("window_hours"),
            "depth": form.get("depth") or "standard",
        }
    )


def _recent_report_rows(session: Session, limit: int = 6) -> list[dict]:
    """Recent reports with the topic name they belong to."""
    rows: list[dict] = []
    for report in reports_repo.list_reports(session, limit=limit):
        topic_name = ""
        if report.research_topic_id:
            topic = session.get(
                topics_repo.ResearchTopic, report.research_topic_id
            ) if hasattr(topics_repo, "ResearchTopic") else None
            if topic is None:
                topic = topics_repo.get_topic(session, report.research_topic_id)
            topic_name = topic.name if topic is not None else ""
        rows.append({"report": report, "topic_name": topic_name})
    return rows


def _latest_agent_run(session: Session, topic_id: Optional[int]):
    """The most recent research run, preferring the active topic's own."""
    from sqlalchemy import select

    from ..models import MonitoringRun

    stmt = select(MonitoringRun).where(MonitoringRun.engine == RUN_ENGINE_AGENT)
    if topic_id is not None:
        stmt = stmt.where(MonitoringRun.research_topic_id == topic_id)
    stmt = stmt.order_by(MonitoringRun.id.desc()).limit(1)
    run = session.scalars(stmt).first()
    if run is not None or topic_id is None:
        return run
    # No run for this topic yet, but an active run elsewhere still has to be
    # visible - otherwise 开始研究 would appear to do nothing.
    stmt = (
        select(MonitoringRun)
        .where(
            MonitoringRun.engine == RUN_ENGINE_AGENT,
            MonitoringRun.status.in_(list(RunStatus.ACTIVE)),
        )
        .order_by(MonitoringRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _run_context(session: Session, run) -> dict:
    """Everything the live/result region needs for one run."""
    if run is None:
        return {"run": None}

    stage = stage_of(run.stage or "")
    current_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
    if not run.is_active:
        current_index = len(STAGE_ORDER)

    report = reports_repo.get_by_run(session, run.id) if not run.is_active else None
    failed = run.status in {RunStatus.FAILED, RunStatus.CANCELLED}
    topic = (
        topics_repo.get_topic(session, run.research_topic_id)
        if run.research_topic_id
        else None
    )

    return {
        "run": run,
        "poll": run.is_active,
        "duration": duration_text(run.started_at, run.finished_at),
        "research_stages": RESEARCH_STAGES,
        "current_index": current_index,
        "report": report,
        "failed": failed,
        "failure_reason": _failure_reason(run),
        "partial": (run.coverage_status or "") == COVERAGE_PARTIAL,
        "partial_notice": COVERAGE_PARTIAL_NOTICE,
        "empty_notice": COVERAGE_EMPTY_NOTICE,
        "topic_name": topic.name if topic is not None else "研究",
        "topic_id": run.research_topic_id,
    }


def _failure_reason(run) -> str:
    """A calm, non-technical reason a run did not finish."""
    if run.status == RunStatus.CANCELLED:
        return "研究已被手动停止。"
    message = (run.error_message or "").strip()
    return message or "研究服务暂时不可用。"


# --- home -------------------------------------------------------------------

def _home_context(session: Session, extra: Optional[dict] = None) -> dict:
    topic_id = _active_topic_id(session)
    topic = topics_repo.get_topic(session, topic_id) if topic_id else None
    if topic is None:
        topic_id = None

    context: dict = {
        "nav": "home",
        "engine": engine_status(session),
        "recent_topics": topics_repo.recent_topics(session, limit=6),
        "recent_reports": _recent_report_rows(session),
        "active_topic": topic,
        "brief": ResearchBrief.from_topic(topic) if topic is not None else None,
        "description": "",
        "brief_error": "",
    }
    context.update(_run_context(session, _latest_agent_run(session, topic_id)))
    if extra:
        context.update(extra)
    return context


@router.get("/")
def home(request: Request, new: str = "", session: Session = Depends(get_db)):
    """The application home page - whichever mode the user prefers.

    ``/`` is mode-aware rather than redirecting, so a bookmark keeps working
    and the switch never bounces the user through an extra navigation.
    """
    if not mode_service.is_simple(session):
        from .dashboard import dashboard_context

        return render(request, "dashboard.html", dashboard_context(session))

    if new:
        # "+ 新主题" clears the remembered context so the textarea is empty.
        _set_active_topic(session, None)
    return render(request, "simple_home.html", _home_context(session))


@router.get("/simple")
def simple_home(request: Request, session: Session = Depends(get_db)):
    """Explicit Simple home, reachable even while Professional mode is active."""
    return render(request, "simple_home.html", _home_context(session))


# --- 研究引擎 ---------------------------------------------------------------

def _setup_context(session: Session, provider_id: str = "", extra: Optional[dict] = None) -> dict:
    from ..services.llm.presets import ordered_presets, preset_or_custom

    status = engine_status(session)
    chosen = (provider_id or status.provider_id or "deepseek").strip().lower()
    preset = preset_or_custom(chosen)
    current = providers_repo.get_by_provider_id(session, chosen)
    # Two separate lists, never merged. 研究引擎 offers remote agents only;
    # the local collection backend lives behind 高级 so choosing it is always
    # a deliberate act.
    agents = agents_for_provider_choice(chosen)
    advanced_agents = advanced_agents_for_provider_choice(chosen)
    selectable = [*agents, *advanced_agents]
    selected_agent = status.agent_id
    if not any(spec.agent_id == selected_agent for spec in selectable):
        default = default_agent_for_provider(chosen)
        selected_agent = default.agent_id if default else ""

    context: dict = {
        "nav": "home",
        "engine": status,
        "presets": ordered_presets(),
        "selected_provider": chosen,
        "current": current,
        "agents": agents,
        "advanced_agents": advanced_agents,
        "all_agents": all_agents(),
        "selected_agent": selected_agent,
        "supports_remote_research": bool(agents),
        "model_placeholder": (
            f"例如 {preset.example_model}" if preset.example_model else "填写模型名称"
        ),
        "base_url_placeholder": preset.default_base_url or "留空使用默认地址",
        "unavailable_reason": unavailable_reason_for_provider(chosen),
        "keyring_backend": keyring_service.backend_name(),
        "providers": providers_repo.list_providers(session),
        "test_result": None,
    }
    if extra:
        context.update(extra)
    return context


@router.get("/simple/engine")
def engine_setup(request: Request, provider: str = "", session: Session = Depends(get_db)):
    """Inline first-time setup / management of the research engine."""
    return render(request, "simple_engine_setup.html", _setup_context(session, provider))


@router.post("/simple/engine")
async def save_engine(request: Request, session: Session = Depends(get_db)):
    """Save (or test) the research engine.

    The credential goes straight from the form to the OS keyring and is then
    dropped. Only ``has_api_key`` and the last four characters are persisted -
    the provider table is structurally incapable of holding a secret.
    """
    form = await request.form()
    action = (form.get("action") or "save").strip()
    provider_id = (form.get("provider_id") or "").strip().lower()
    api_key = (form.get("api_key") or "").strip()
    agent_id = (form.get("agent_id") or "").strip()

    if not provider_id:
        return render(
            request,
            "simple_engine_setup.html",
            _setup_context(
                session,
                provider_id,
                {"test_result": {"ok": False, "message": "请选择一个服务商。"}},
            ),
        )

    from ..services.llm.presets import preset_or_custom
    from ..services.llm.service import test_provider_row
    from ..services.provider_migration import sync_key_state

    preset = preset_or_custom(provider_id)
    model = (form.get("default_model") or "").strip() or preset.example_model
    base_url = (form.get("base_url") or "").strip() or preset.default_base_url

    row = providers_repo.get_by_provider_id(session, provider_id)
    if row is None:
        row = providers_repo.create_provider(
            session,
            provider_id=provider_id,
            display_name=preset.display_name,
            base_url=base_url,
            default_model=model,
            enabled=True,
        )
        # The first provider a user configures becomes the default, so the
        # Professional page and the research engine agree out of the box.
        if providers_repo.count_providers(session) == 1:
            providers_repo.set_default(session, row)
    else:
        row.display_name = row.display_name or preset.display_name
        row.base_url = base_url
        row.default_model = model
        row.enabled = True
        session.flush()

    if api_key and not api_key.startswith("•"):
        try:
            keyring_service.set_provider_key(provider_id, api_key)
        except Exception as exc:
            logger.error("Could not store credential: %s", type(exc).__name__)
            return render(
                request,
                "simple_engine_setup.html",
                _setup_context(
                    session,
                    provider_id,
                    {"test_result": {"ok": False, "message": "无法写入系统凭据存储，请检查权限。"}},
                ),
            )
        sync_key_state(session, provider_id)

    # Capability is checked at the point of choice, not only at run time. A
    # chat-only endpoint posted here - by an out-of-date page, a bookmarked
    # form or a hand-rolled request - is refused with the reason, never stored
    # as a Research Agent and never quietly swapped for local collection.
    requested = get_agent(agent_id) if agent_id else None
    if requested is not None and not requested.is_research_capable:
        return render(
            request,
            "simple_engine_setup.html",
            _setup_context(
                session,
                provider_id,
                {
                    "test_result": {
                        "ok": False,
                        "message": (
                            requested.unavailable_reason
                            or f"{requested.display_name} 不能作为研究 Agent。"
                        ),
                    }
                },
            ),
        )
    # A vendor agent belongs to its vendor; pairing it with another provider's
    # credentials would fail at the API with a confusing error.
    if requested is not None and requested.provider_id and requested.provider_id != provider_id:
        requested = None

    spec = requested or default_agent_for_provider(provider_id)
    if spec is not None:
        set_selection(session, provider_id, spec.agent_id)
    else:
        # No remote agent for this provider and none chosen. Record the
        # provider so the rest of AIOS can use it, but leave the research
        # engine unselected rather than defaulting into local collection.
        set_selection(session, provider_id, "")

    if action == "test":
        result = test_provider_row(row)
        message = (
            f"连接成功 · {row.display_name} · 模型 {result.get('model')} "
            f"· 响应 {result.get('latency_ms')} ms"
            if result.get("ok")
            else f"连接失败：{result.get('error', '未知错误')}"
        )
        return render(
            request,
            "simple_engine_setup.html",
            _setup_context(
                session,
                provider_id,
                {"test_result": {"ok": bool(result.get("ok")), "message": message}},
            ),
        )

    # Straight back to the workflow, which is the whole point of inline setup.
    return redirect("/", f"{row.display_name} 已配置为研究引擎。", "ok")


# --- 研究主题 ---------------------------------------------------------------

@router.post("/simple/topics/refine")
async def refine_topic(request: Request, session: Session = Depends(get_db)):
    """AI 完善主题: one sentence becomes a Research Brief. Writes nothing."""
    form = await request.form()
    description = (form.get("description") or "").strip()

    _set_active_topic(session, None)

    try:
        service = ResearchTopicService()
        result = service.refine(description)
    except BriefGenerationError as exc:
        return render(
            request,
            "simple_home.html",
            _home_context(
                session, {"description": description, "brief_error": str(exc)}
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Brief generation failed")
        return render(
            request,
            "simple_home.html",
            _home_context(
                session,
                {"description": description, "brief_error": f"主题生成失败：{exc}"},
            ),
        )

    research_topic_service.record_usage(session, result.usage)
    return render(
        request,
        "simple_home.html",
        _home_context(
            session,
            {"brief": result.brief, "description": description, "active_topic": None},
        ),
    )


@router.post("/simple/topics")
async def save_topic(request: Request, session: Session = Depends(get_db)):
    """保存主题: persist a previewed brief as a reusable research topic."""
    form = await request.form()
    try:
        brief = _brief_from_form(form)
    except BriefError as exc:
        return redirect("/", f"主题保存失败：{exc}", "error")

    topic = topics_repo.create_topic(
        session,
        name=brief.name,
        brief=brief.brief,
        scope=brief.scope,
        focus_areas=brief.focus_areas,
        exclusions=brief.exclusions,
        keywords=brief.keywords,
        regions=brief.regions,
        window_hours=brief.window_hours,
        depth=brief.depth,
    )
    _set_active_topic(session, topic.id)
    return redirect("/", f"已保存研究主题「{topic.name}」。", "ok")


@router.post("/simple/topics/{topic_id}/load")
def load_topic(topic_id: int, session: Session = Depends(get_db)):
    """Clicking a 最近使用 chip loads that topic's full brief."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Research topic not found")
    _set_active_topic(session, topic.id)
    return redirect("/", "", "ok")


@router.post("/simple/topics/edit")
async def edit_topic_form(request: Request, session: Session = Depends(get_db)):
    """修改: open the explicit editor, seeded from the previewed brief."""
    form = await request.form()
    raw_topic_id = (form.get("topic_id") or "").strip()
    topic = None
    if raw_topic_id:
        try:
            topic = topics_repo.get_topic(session, int(raw_topic_id))
        except (TypeError, ValueError):
            topic = None

    try:
        brief = _brief_from_form(form)
    except BriefError:
        brief = ResearchBrief.from_topic(topic) if topic is not None else fallback_brief("")

    return render(
        request,
        "simple_topic_edit.html",
        {
            "nav": "home",
            "engine": engine_status(session),
            "brief": brief,
            "topic": topic,
            "window_choices": WINDOW_CHOICES,
            "action": "/simple/topics/save",
        },
    )


@router.post("/simple/topics/save")
async def save_edited_topic(request: Request, session: Session = Depends(get_db)):
    """Persist an edited brief, then optionally start research immediately."""
    form = await request.form()
    action = (form.get("action") or "save").strip()
    raw_topic_id = (form.get("topic_id") or "").strip()

    try:
        brief = _brief_from_form(form)
    except BriefError as exc:
        return redirect("/", f"主题保存失败：{exc}", "error")

    topic = None
    if raw_topic_id:
        try:
            topic = topics_repo.get_topic(session, int(raw_topic_id))
        except (TypeError, ValueError):
            topic = None

    if topic is None:
        topic = topics_repo.create_topic(
            session,
            name=brief.name,
            brief=brief.brief,
            scope=brief.scope,
            focus_areas=brief.focus_areas,
            exclusions=brief.exclusions,
            keywords=brief.keywords,
            regions=brief.regions,
            window_hours=brief.window_hours,
            depth=brief.depth,
        )
        message = f"已保存研究主题「{topic.name}」。"
    else:
        # Same id, new revision. Historical reports stay attached.
        topics_repo.update_topic(
            session, topic, brief.as_changes(), change_note="手动修改"
        )
        message = f"已更新研究主题「{topic.name}」。"

    _set_active_topic(session, topic.id)
    session.commit()

    if action == "run":
        return _start_research(session, topic.id)
    return redirect("/", message, "ok")


@router.post("/simple/topics/{topic_id}/revise")
async def revise_topic(
    topic_id: int, instruction: str = Form(""), session: Session = Depends(get_db)
):
    """Natural-language edit of an existing topic.

    The topic id never changes, and the previous brief is snapshotted as a
    revision - so every report already produced stays linked to this topic and
    readable against the brief that produced it.
    """
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Research topic not found")

    current = ResearchBrief.from_topic(topic)
    try:
        service = ResearchTopicService()
        result = service.revise(current, instruction)
    except BriefGenerationError as exc:
        return redirect("/", f"主题调整失败：{exc}", "error")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Brief revision failed")
        return redirect("/", f"主题调整失败：{exc}", "error")

    research_topic_service.record_usage(session, result.usage)
    assert result.brief is not None
    topics_repo.update_topic(
        session, topic, result.brief.as_changes(), change_note=instruction
    )
    _set_active_topic(session, topic.id)
    note = result.notes or "已根据你的说明调整该主题。"
    return redirect("/", note, "ok")


@router.post("/simple/topics/{topic_id}/schedule")
def save_schedule(
    topic_id: int,
    enabled: str = Form(None),
    time: str = Form("08:00"),  # noqa: A002 - matches the form field name
    session: Session = Depends(get_db),
):
    """每天自动研究 for one topic. No cron syntax is ever exposed."""
    topic = topics_repo.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Research topic not found")

    from ..timeutil import parse_hhmm

    text = (time or "08:00").strip()
    try:
        parse_hhmm(text)
    except ValueError:
        return redirect("/", "时间格式无效，请使用 HH:MM。", "error")

    is_on = str(enabled or "").lower() in {"on", "true", "1", "yes"}
    topics_repo.set_schedule(session, topic, is_on, text)
    session.commit()

    from ..services.scheduler import scheduler

    scheduler.reload()
    message = f"已开启每天 {text} 自动研究「{topic.name}」。" if is_on else "已关闭自动研究。"
    return redirect("/", message, "ok")


# --- running ----------------------------------------------------------------

def _start_research(session: Session, topic_id: int):
    """Queue a research run for one topic, or explain why it cannot start."""
    status = engine_status(session)
    if not status.ready:
        return redirect(
            "/simple/engine",
            status.error or "请先配置研究引擎。",
            "warn",
        )
    try:
        run_id = manager.start_research_run(topic_id)
    except RunAlreadyActive:
        return redirect("/", "已有研究任务正在执行。", "warn")
    logger.info("Queued research run %s for topic %s", run_id, topic_id)
    return redirect("/", "研究已开始。", "ok")


@router.post("/simple/research")
async def start_research(request: Request, session: Session = Depends(get_db)):
    """开始研究.

    A brief that was only previewed is saved first: a run has to point at a
    topic id so its events, observations and report can be tracked over time.
    The user is told, rather than having a topic appear silently.
    """
    form = await request.form()
    raw_topic_id = (form.get("topic_id") or "").strip()

    if raw_topic_id:
        try:
            topic_id = int(raw_topic_id)
        except (TypeError, ValueError):
            return redirect("/", "研究主题无效。", "error")
        if topics_repo.get_topic(session, topic_id) is None:
            return redirect("/", "研究主题不存在。", "error")
        _set_active_topic(session, topic_id)
        session.commit()
        return _start_research(session, topic_id)

    try:
        brief = _brief_from_form(form)
    except BriefError as exc:
        return redirect("/", f"无法开始研究：{exc}", "error")

    topic = topics_repo.create_topic(
        session,
        name=brief.name,
        brief=brief.brief,
        scope=brief.scope,
        focus_areas=brief.focus_areas,
        exclusions=brief.exclusions,
        keywords=brief.keywords,
        regions=brief.regions,
        window_hours=brief.window_hours,
        depth=brief.depth,
    )
    _set_active_topic(session, topic.id)
    session.commit()
    return _start_research(session, topic.id)


@router.get("/simple/run/{run_id}")
def run_fragment(run_id: int, request: Request, session: Session = Depends(get_db)):
    """HTMX fragment: the live research region.

    Polls itself every two seconds while the run is active and renders without
    a trigger once the run is terminal, so polling stops with no client-side
    bookkeeping - the same mechanism the Professional run view uses.
    """
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return partial(request, "partials/simple_run.html", _run_context(session, run))


@router.post("/simple/run/{run_id}/cancel")
def cancel_run(run_id: int, session: Session = Depends(get_db)):
    """Ask an in-flight research run to stop at its next checkpoint."""
    run = runs_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in RunStatus.TERMINAL:
        return redirect("/", "该研究已结束。", "warn")
    manager.cancel(run_id)
    return redirect("/", "已请求停止，研究将在下一个检查点结束。", "warn")


# --- 查看变化 ---------------------------------------------------------------

@router.get("/simple/changes/{report_id}")
def changes(report_id: int, request: Request, session: Session = Depends(get_db)):
    """相比上一期, in plain language over the same Event/Observation data."""
    from ..services import simple_views

    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    context = simple_views.changes_context(session, report)
    context.update(
        {
            "nav": "home",
            "engine": engine_status(session),
            "back_url": "/",
        }
    )
    return render(request, "simple_changes.html", context)


# --- 来源证据 ---------------------------------------------------------------

@router.get("/simple/sources/{report_id}")
def sources(report_id: int, request: Request, session: Session = Depends(get_db)):
    """Evidence as publishers and clickable links. No JSON anywhere."""
    from ..services import simple_views

    report = reports_repo.get_report(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    context = simple_views.sources_context(session, report)
    context.update(
        {
            "nav": "home",
            "engine": engine_status(session),
            "back_url": "/",
        }
    )
    return render(request, "simple_sources.html", context)


# --- 历史记录 ---------------------------------------------------------------

@router.get("/simple/history")
def history(
    request: Request, topic_id: int = 0, session: Session = Depends(get_db)
):
    """History centred on research topics rather than on runs."""
    from ..services import simple_views

    context = simple_views.history_context(
        session, topic_id=topic_id or None
    )
    context.update({"nav": "history", "engine": engine_status(session)})
    return render(request, "simple_history.html", context)
