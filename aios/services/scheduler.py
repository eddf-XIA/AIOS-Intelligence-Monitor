"""Daily automatic monitoring and research via APScheduler.

Two kinds of job, both rebuilt from the database on startup so they survive a
restart:

* one **Classic** monitoring job from the ``scheduler_*`` settings - the v2.1
  behaviour, unchanged;
* one **research** job per :class:`~aios.models.ResearchTopic` that has
  每天自动研究 switched on.

Concurrency is handled by :class:`~aios.services.run_manager.RunManager`, which
refuses a second run - a job simply logs and skips rather than queueing work
that cannot start. That single queue is also why a Classic job and a research
job scheduled for the same minute cannot corrupt each other.

No cron syntax ever reaches 简易版: a topic stores ``HH:MM`` and this module
turns it into a trigger.
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
#: One research job per topic: ``aios-research-topic-7``.
RESEARCH_JOB_PREFIX = "aios-research-topic-"


def research_job_id(topic_id: int) -> str:
    return f"{RESEARCH_JOB_PREFIX}{topic_id}"


def _scheduled_job() -> None:
    """Entry point APScheduler calls. Never raises into the scheduler thread."""
    try:
        run_id = manager.start_run(trigger_type="scheduled")
        logger.info("Scheduled monitoring run %s started", run_id)
    except RunAlreadyActive as exc:
        logger.warning("Skipping scheduled run: %s", exc)
    except Exception:
        logger.exception("Scheduled monitoring run could not be started")


def _scheduled_research_job(topic_id: int) -> None:
    """Run one research topic. Never raises into the scheduler thread.

    The topic is re-read here rather than captured, so a topic archived or
    switched off since the job was installed is skipped instead of researched.
    """
    try:
        from ..repositories import research_topics as topics_repo

        with session_scope() as session:
            topic = topics_repo.get_topic(session, topic_id)
            if topic is None or topic.archived or not topic.schedule_enabled:
                logger.info("Skipping research for topic %s: no longer scheduled", topic_id)
                return
            name = topic.name

        from .research import readiness_error

        with session_scope() as session:
            error = readiness_error(session)
        if error:
            # Never start a run that could not produce a truthful result.
            logger.warning("Skipping scheduled research for %s: %s", name, error)
            return

        run_id = manager.start_research_run(topic_id, trigger_type="scheduled")
        logger.info("Scheduled research run %s started for %s", run_id, name)
    except RunAlreadyActive as exc:
        logger.warning("Skipping scheduled research: %s", exc)
    except Exception:
        logger.exception("Scheduled research run could not be started")


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
        self._install_research_jobs()

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

    def _install_research_jobs(self) -> int:
        """Install one daily job per scheduled research topic.

        Called from :meth:`reload` after the job table has been cleared, so the
        installed jobs always match the topics with 每天自动研究 switched on.
        """
        if self._scheduler is None:
            return 0

        from ..repositories import research_topics as topics_repo

        try:
            with session_scope() as session:
                scheduled = [
                    (topic.id, topic.name, topic.schedule_time)
                    for topic in topics_repo.scheduled_topics(session)
                ]
        except Exception:  # pragma: no cover - before the table exists
            logger.debug("Could not read scheduled research topics", exc_info=True)
            return 0

        installed = 0
        for topic_id, name, time_text in scheduled:
            try:
                hour, minute = parse_hhmm(time_text or "08:00")
            except ValueError:
                logger.warning(
                    "Invalid schedule time %r for research topic %s, using 08:00",
                    time_text, name,
                )
                hour, minute = 8, 0

            self._scheduler.add_job(
                _scheduled_research_job,
                trigger=CronTrigger(hour=hour, minute=minute),
                id=research_job_id(topic_id),
                args=[topic_id],
                replace_existing=True,
            )
            installed += 1
            logger.info(
                "Research topic %s scheduled daily at %02d:%02d", name, hour, minute
            )
        return installed

    def research_jobs(self) -> list[dict]:
        """Installed research jobs - shown in Settings, asserted by tests."""
        if self._scheduler is None:
            return []
        jobs = []
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(RESEARCH_JOB_PREFIX):
                continue
            jobs.append(
                {
                    "topic_id": int(job.id[len(RESEARCH_JOB_PREFIX) :]),
                    "next_run": self._next_run_of(job),
                }
            )
        return jobs

    @staticmethod
    def _next_run_of(job) -> Optional[str]:
        """Local-time text for one job's next fire, or None.

        ``next_run_time`` is only populated once the scheduler is running, and
        APScheduler does not define the attribute before then - so it is read
        defensively rather than assumed to exist.
        """
        when = getattr(job, "next_run_time", None)
        return when.strftime("%Y-%m-%d %H:%M") if when else None

    def next_run_text(self) -> Optional[str]:
        """Local-time text for the next Classic monitoring execution."""
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(JOB_ID)
        return self._next_run_of(job) if job is not None else None

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
            "research_jobs": self.research_jobs(),
        }


#: Process-wide scheduler instance.
scheduler = SchedulerService()
