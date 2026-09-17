"""Daily automatic monitoring via APScheduler.

The schedule lives in the settings table, so it survives restarts: the app
reads it on startup and rebuilds the job. Concurrency is handled by
:class:`~aios.services.run_manager.RunManager`, which refuses a second run -
the job simply logs and skips rather than queueing work that cannot start.
"""

from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..database import session_scope
from ..timeutil import local_tz_name, parse_hhmm
from . import settings_service
from .run_manager import RunAlreadyActive, manager

logger = logging.getLogger(__name__)

JOB_ID = "aios-daily-monitoring"


def _scheduled_job() -> None:
    """Entry point APScheduler calls. Never raises into the scheduler thread."""
    try:
        run_id = manager.start_run(trigger_type="scheduled")
        logger.info("Scheduled monitoring run %s started", run_id)
    except RunAlreadyActive as exc:
        logger.warning("Skipping scheduled run: %s", exc)
    except Exception:
        logger.exception("Scheduled monitoring run could not be started")


class SchedulerService:
    """Thin wrapper around a BackgroundScheduler holding at most one job."""

    def __init__(self) -> None:
        self._scheduler: Optional[BackgroundScheduler] = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        """Start the scheduler and install the job if it is enabled."""
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler(
                job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
            )
        if not self._scheduler.running:
            self._scheduler.start()
        self.reload()

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    def reload(self) -> Optional[str]:
        """Re-read settings and (re)install or remove the job.

        Returns the next fire time as local text, or None when disabled.
        """
        if self._scheduler is None:
            return None

        with session_scope() as session:
            enabled = settings_service.get_bool(session, "scheduler_enabled", False)
            time_text = settings_service.get_str(session, "scheduler_time", "06:00")

        self._scheduler.remove_all_jobs()
        if not enabled:
            logger.info("Automatic monitoring is disabled")
            return None

        try:
            hour, minute = parse_hhmm(time_text)
        except ValueError:
            logger.warning("Invalid scheduler_time %r, falling back to 06:00", time_text)
            hour, minute = 6, 0

        self._scheduler.add_job(
            _scheduled_job,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=JOB_ID,
            replace_existing=True,
        )
        next_run = self.next_run_text()
        logger.info("Automatic monitoring scheduled daily at %02d:%02d (%s)", hour, minute, next_run)
        return next_run

    def next_run_text(self) -> Optional[str]:
        """Local-time text for the next scheduled execution."""
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(JOB_ID)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.strftime("%Y-%m-%d %H:%M")

    def status(self) -> dict:
        """What the Settings page displays about automatic monitoring."""
        with session_scope() as session:
            enabled = settings_service.get_bool(session, "scheduler_enabled", False)
            time_text = settings_service.get_str(session, "scheduler_time", "06:00")
        return {
            "enabled": enabled,
            "time": time_text,
            "timezone": local_tz_name(),
            "running": self.running,
            "next_run": self.next_run_text(),
        }


#: Process-wide scheduler instance.
scheduler = SchedulerService()
