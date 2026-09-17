"""Background execution of monitoring runs.

An HTTP request must never wait for a pipeline that takes minutes, so the
router creates a queued run, hands it to this manager, and returns immediately;
the browser then polls ``/runs/{id}/status``.

A single worker thread is used deliberately: concurrent runs would fight over
the same SQLite file and produce two reports for the same day.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from ..database import session_scope
from ..models import RunStatus
from ..repositories import runs as runs_repo
from ..timeutil import local_today, utcnow
from .pipeline import run_pipeline

logger = logging.getLogger(__name__)


class RunAlreadyActive(RuntimeError):
    """Raised when a run is requested while another is queued or executing."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"Monitoring run #{run_id} is already in progress.")
        self.run_id = run_id


class RunManager:
    """Owns the worker thread and the cancellation flags."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aios-run")
        self._lock = threading.Lock()
        self._current_run_id: Optional[int] = None
        self._cancel_flags: dict[int, threading.Event] = {}
        self._futures: dict[int, Future] = {}

    # -- state ---------------------------------------------------------------

    @property
    def current_run_id(self) -> Optional[int]:
        with self._lock:
            return self._current_run_id

    def is_busy(self) -> bool:
        return self.current_run_id is not None

    # -- starting ------------------------------------------------------------

    def start_run(
        self, trigger_type: str = "manual", report_date: Optional[dt.date] = None
    ) -> int:
        """Queue a run and return its id.

        Raises :class:`RunAlreadyActive` if one is already in flight - the
        scheduler relies on this to skip rather than pile up.
        """
        with self._lock:
            if self._current_run_id is not None:
                raise RunAlreadyActive(self._current_run_id)

            with session_scope() as session:
                existing = runs_repo.active_run(session)
                if existing is not None:
                    # A run left behind by a crash would block us forever;
                    # only treat it as active if this process owns it.
                    if existing.id in self._futures and not self._futures[existing.id].done():
                        raise RunAlreadyActive(existing.id)
                    existing.status = RunStatus.FAILED
                    existing.stage = "interrupted"
                    existing.finished_at = utcnow()
                    existing.error_message = (
                        "Interrupted - the application stopped while this run was active."
                    )

                run = runs_repo.create_run(
                    session, report_date=report_date or local_today(), trigger_type=trigger_type
                )
                run_id = run.id

            self._current_run_id = run_id
            cancel_event = threading.Event()
            self._cancel_flags[run_id] = cancel_event
            future = self._executor.submit(self._execute, run_id, cancel_event)
            self._futures[run_id] = future

        logger.info("Queued monitoring run %s (%s)", run_id, trigger_type)
        return run_id

    def _execute(self, run_id: int, cancel_event: threading.Event) -> str:
        try:
            return run_pipeline(run_id, cancel_check=cancel_event.is_set)
        except Exception:  # pragma: no cover - the pipeline handles its own errors
            logger.exception("Run %s crashed outside the pipeline", run_id)
            try:
                with session_scope() as session:
                    run = runs_repo.get_run(session, run_id)
                    if run is not None and run.status in RunStatus.ACTIVE:
                        run.status = RunStatus.FAILED
                        run.finished_at = utcnow()
                        run.stage = "failed"
            except Exception:
                logger.exception("Could not mark run %s as failed", run_id)
            return RunStatus.FAILED
        finally:
            with self._lock:
                if self._current_run_id == run_id:
                    self._current_run_id = None
                self._cancel_flags.pop(run_id, None)

    # -- cancelling ----------------------------------------------------------

    def cancel(self, run_id: int) -> bool:
        """Ask a run to stop at its next checkpoint."""
        with self._lock:
            event = self._cancel_flags.get(run_id)
        if event is not None:
            event.set()
        with session_scope() as session:
            run = runs_repo.get_run(session, run_id)
            if run is None or run.status in RunStatus.TERMINAL:
                return False
            run.cancel_requested = True
        logger.info("Cancellation requested for run %s", run_id)
        return True

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting work; used on application shutdown."""
        with self._lock:
            for event in self._cancel_flags.values():
                event.set()
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


#: Process-wide manager. The app is single-user, so one instance is correct.
manager = RunManager()


def start_run(trigger_type: str = "manual", report_date: Optional[dt.date] = None) -> int:
    return manager.start_run(trigger_type=trigger_type, report_date=report_date)


def cancel_run(run_id: int) -> bool:
    return manager.cancel(run_id)


def reset_stale_runs() -> int:
    """Mark runs left ``running`` by a previous process as failed.

    Called once at startup: without this a crash would leave a phantom active
    run that blocks every future run.
    """
    cleared = 0
    with session_scope() as session:
        for run in runs_repo.list_runs(session, limit=20):
            if run.status in RunStatus.ACTIVE:
                run.status = RunStatus.FAILED
                run.stage = "interrupted"
                run.finished_at = run.finished_at or utcnow()
                run.error_message = (
                    "Interrupted - the application stopped while this run was active."
                )
                cleared += 1
    if cleared:
        logger.info("Cleared %s stale run(s) from a previous session", cleared)
    return cleared
