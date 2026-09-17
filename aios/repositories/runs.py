"""Queries over monitoring runs, per-module progress, logs and LLM usage."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import LLMUsage, ModuleRun, MonitoringRun, RunLog, RunStatus
from ..timeutil import utcnow


def get_run(session: Session, run_id: int) -> Optional[MonitoringRun]:
    stmt = (
        select(MonitoringRun)
        .options(selectinload(MonitoringRun.module_runs))
        .where(MonitoringRun.id == run_id)
    )
    return session.scalars(stmt).unique().one_or_none()


def latest_run(session: Session) -> Optional[MonitoringRun]:
    stmt = (
        select(MonitoringRun)
        .options(selectinload(MonitoringRun.module_runs))
        .order_by(MonitoringRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).unique().first()


def active_run(session: Session) -> Optional[MonitoringRun]:
    """A run that is queued or executing - used to prevent concurrent runs."""
    stmt = (
        select(MonitoringRun)
        .where(MonitoringRun.status.in_(list(RunStatus.ACTIVE)))
        .order_by(MonitoringRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def list_runs(session: Session, limit: int = 40) -> list[MonitoringRun]:
    stmt = (
        select(MonitoringRun)
        .options(selectinload(MonitoringRun.module_runs))
        .order_by(MonitoringRun.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def create_run(session: Session, report_date: dt.date, trigger_type: str) -> MonitoringRun:
    run = MonitoringRun(
        report_date=report_date,
        trigger_type=trigger_type,
        status=RunStatus.PENDING,
        stage="queued",
    )
    session.add(run)
    session.flush()
    return run


def create_module_run(session: Session, run_id: int, module_id: int, module_name: str) -> ModuleRun:
    module_run = ModuleRun(run_id=run_id, module_id=module_id, module_name=module_name)
    session.add(module_run)
    session.flush()
    return module_run


def get_module_run(session: Session, module_run_id: int) -> Optional[ModuleRun]:
    return session.get(ModuleRun, module_run_id)


def module_runs_for(session: Session, run_id: int) -> list[ModuleRun]:
    stmt = select(ModuleRun).where(ModuleRun.run_id == run_id).order_by(ModuleRun.id)
    return list(session.scalars(stmt))


def add_log(
    session: Session, run_id: int, message: str, level: str = "info", module_key: str = ""
) -> RunLog:
    entry = RunLog(
        run_id=run_id,
        message=message,
        level=level,
        module_key=module_key,
        timestamp=utcnow(),
    )
    session.add(entry)
    session.flush()
    return entry


def logs_for(session: Session, run_id: int, after_id: int = 0, limit: int = 500) -> list[RunLog]:
    stmt = (
        select(RunLog)
        .where(RunLog.run_id == run_id, RunLog.id > after_id)
        .order_by(RunLog.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def add_usage(session: Session, **fields) -> LLMUsage:
    usage = LLMUsage(**fields)
    session.add(usage)
    session.flush()
    return usage


def usage_summary(session: Session, run_id: int) -> dict:
    """Aggregate LLM usage for one run. Tokens stay None when unreported."""
    row = session.execute(
        select(
            func.count(LLMUsage.id),
            func.sum(LLMUsage.total_tokens),
            func.sum(LLMUsage.prompt_tokens),
            func.sum(LLMUsage.completion_tokens),
            func.avg(LLMUsage.latency_ms),
            func.sum(LLMUsage.latency_ms),
        ).where(LLMUsage.run_id == run_id)
    ).one()
    calls, total, prompt, completion, avg_latency, sum_latency = row
    return {
        "calls": calls or 0,
        "total_tokens": total,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "avg_latency_ms": int(avg_latency) if avg_latency else 0,
        "total_latency_ms": int(sum_latency) if sum_latency else 0,
    }


def count_runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(MonitoringRun)) or 0
