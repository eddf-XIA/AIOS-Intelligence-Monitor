"""Per-run source health persistence.

The pipeline writes these rows at every collection checkpoint so the live run
view and the finished run page read the same numbers from the same place. The
counters are upserted rather than appended: one row per (run, collector).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RunSourceStat
from ..timeutil import utcnow

#: Counter fields copied straight from
#: :class:`aios.services.source_health.SourceCounters`.
_COUNTER_FIELDS = (
    "requests_attempted",
    "requests_succeeded",
    "zero_result_responses",
    "candidates_returned",
    "timeouts",
    "rate_limited",
    "network_errors",
    "parse_errors",
    "http_errors",
    "circuit_activations",
    "circuit_open_skips",
    "cache_hits",
)


def stats_for_run(session: Session, run_id: int) -> list[RunSourceStat]:
    stmt = (
        select(RunSourceStat)
        .where(RunSourceStat.run_id == run_id)
        .order_by(RunSourceStat.collector)
    )
    return list(session.scalars(stmt))


def get_stat(session: Session, run_id: int, collector: str) -> Optional[RunSourceStat]:
    stmt = select(RunSourceStat).where(
        RunSourceStat.run_id == run_id, RunSourceStat.collector == collector
    )
    return session.scalars(stmt).one_or_none()


def upsert_stat(session: Session, run_id: int, payload: dict) -> RunSourceStat:
    """Write one collector's counters for one run.

    ``payload`` is the dict form of ``SourceCounters`` plus its derived
    ``state``; unknown keys are ignored so the health model can grow without a
    migration on every field.
    """
    collector = str(payload.get("collector") or "")
    row = get_stat(session, run_id, collector)
    if row is None:
        row = RunSourceStat(run_id=run_id, collector=collector)
        session.add(row)

    row.display_name = str(payload.get("display_name") or collector)[:120]
    row.state = str(payload.get("state") or "unknown")[:32]
    for field in _COUNTER_FIELDS:
        setattr(row, field, int(payload.get(field) or 0))
    row.avg_latency_ms = int(payload.get("avg_latency_ms") or 0)
    row.last_error = str(payload.get("last_error") or "")[:1000]
    row.updated_at = utcnow()
    session.flush()
    return row


def replace_stats(session: Session, run_id: int, payloads: list[dict]) -> int:
    """Write every collector's counters for a run in one transaction."""
    written = 0
    for payload in payloads or []:
        if not payload.get("collector"):
            continue
        upsert_stat(session, run_id, payload)
        written += 1
    return written


def degraded_sources(session: Session, run_id: int) -> list[RunSourceStat]:
    """Rows whose state means coverage was not what the user configured."""
    from ..services.source_health import HealthState

    return [
        row for row in stats_for_run(session, run_id) if row.state in HealthState.IMPAIRED
    ]
