"""每天自动研究: the Simple-mode schedule, and the Classic one beside it.

Two properties matter here. First, the toggle in 简易版 has to install a real
job - a switch that quietly does nothing is worse than no switch. Second,
adding research jobs must not disturb the v2.1 Classic monitoring job.
"""

from __future__ import annotations

import pytest

from aios.repositories import research_topics as topics_repo
from aios.services import scheduler as scheduler_module
from aios.services import settings_service


@pytest.fixture
def live_scheduler(db, monkeypatch):
    """A real, running BackgroundScheduler with a throwaway job store.

    Genuinely started, because APScheduler only populates ``next_run_time``
    once it is - and "the toggle installs a job that will actually fire" is the
    property under test. Both job entry points are stubbed out so nothing can
    execute a pipeline even if a trigger were reached.
    """
    monkeypatch.setattr(scheduler_module, "_scheduled_job", lambda: None)
    monkeypatch.setattr(
        scheduler_module, "_scheduled_research_job", lambda topic_id: None
    )

    service = scheduler_module.SchedulerService()
    service.start()
    try:
        yield service
    finally:
        service.shutdown(wait=False)
        service._scheduler = None


class TestResearchJobs:
    def test_a_scheduled_topic_installs_a_job(self, live_scheduler, session):
        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()

        live_scheduler.reload()
        jobs = live_scheduler.research_jobs()
        assert [j["topic_id"] for j in jobs] == [topic.id]
        assert jobs[0]["next_run"] is not None
        assert jobs[0]["next_run"].endswith("08:00")

    def test_an_unscheduled_topic_installs_nothing(self, live_scheduler, session):
        topics_repo.create_topic(session, name="手动主题")
        session.commit()

        live_scheduler.reload()
        assert live_scheduler.research_jobs() == []

    def test_turning_the_toggle_off_removes_the_job(self, live_scheduler, session):
        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()
        live_scheduler.reload()
        assert live_scheduler.research_jobs()

        topics_repo.set_schedule(session, topic, False, "08:00")
        session.commit()
        live_scheduler.reload()
        assert live_scheduler.research_jobs() == []

    def test_an_archived_topic_is_not_scheduled(self, live_scheduler, session):
        topic = topics_repo.create_topic(session, name="归档主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        topics_repo.archive_topic(session, topic)
        session.commit()

        live_scheduler.reload()
        assert live_scheduler.research_jobs() == []

    def test_several_topics_get_several_jobs(self, live_scheduler, session):
        first = topics_repo.create_topic(session, name="主题一")
        second = topics_repo.create_topic(session, name="主题二")
        topics_repo.set_schedule(session, first, True, "07:30")
        topics_repo.set_schedule(session, second, True, "20:00")
        session.commit()

        live_scheduler.reload()
        jobs = {j["topic_id"] for j in live_scheduler.research_jobs()}
        assert jobs == {first.id, second.id}

    def test_an_invalid_stored_time_falls_back_instead_of_crashing(
        self, live_scheduler, session
    ):
        topic = topics_repo.create_topic(session, name="坏时间主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()
        # Corrupt the stored value the way a hand-edited database might.
        topic.schedule_time = "99:99"
        session.commit()

        live_scheduler.reload()
        jobs = live_scheduler.research_jobs()
        assert [j["topic_id"] for j in jobs] == [topic.id]
        assert jobs[0]["next_run"].endswith("08:00")

    def test_research_jobs_coexist_with_the_classic_job(self, live_scheduler, session):
        """Adding v2.2 scheduling must not displace the v2.1 job."""
        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        settings_service.set_many(
            session, {"scheduler_enabled": True, "scheduler_time": "06:00"}
        )
        session.commit()

        live_scheduler.reload()
        assert live_scheduler._scheduler.get_job(scheduler_module.JOB_ID) is not None
        assert live_scheduler.research_jobs()

    def test_status_reports_research_jobs(self, live_scheduler, session):
        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()

        live_scheduler.reload()
        status = live_scheduler.status()
        assert [j["topic_id"] for j in status["research_jobs"]] == [topic.id]


class TestTheJobItself:
    def test_the_job_skips_a_topic_that_is_no_longer_scheduled(
        self, db, session, monkeypatch
    ):
        """State is re-read at fire time, not captured at install time."""
        started: list[int] = []
        from aios.services.run_manager import RunManager

        monkeypatch.setattr(
            RunManager, "start_research_run",
            lambda self, topic_id, trigger_type="manual": started.append(topic_id) or 1,
        )

        topic = topics_repo.create_topic(session, name="已关闭主题")
        session.commit()
        topic_id = topic.id

        scheduler_module._scheduled_research_job(topic_id)
        assert started == []

    def test_the_job_skips_when_the_engine_is_not_ready(self, db, session, monkeypatch):
        """A run that could not produce a truthful result is not started."""
        started: list[int] = []
        from aios.services.run_manager import RunManager

        monkeypatch.setattr(
            RunManager, "start_research_run",
            lambda self, topic_id, trigger_type="manual": started.append(topic_id) or 1,
        )

        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()

        # A fresh install has no usable engine.
        scheduler_module._scheduled_research_job(topic.id)
        assert started == []

    def test_the_job_starts_a_run_when_everything_is_ready(
        self, db, session, monkeypatch
    ):
        started: list[tuple[int, str]] = []
        from aios.services import scheduler as sched
        from aios.services.run_manager import RunManager

        monkeypatch.setattr(
            RunManager, "start_research_run",
            lambda self, topic_id, trigger_type="manual": (
                started.append((topic_id, trigger_type)) or 1
            ),
        )
        monkeypatch.setattr(
            "aios.services.research.readiness_error", lambda session: ""
        )

        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()

        sched._scheduled_research_job(topic.id)
        assert started == [(topic.id, "scheduled")]

    def test_the_job_never_raises_into_the_scheduler_thread(self, db, monkeypatch):
        from aios.services.run_manager import RunManager

        def boom(self, topic_id, trigger_type="manual"):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(RunManager, "start_research_run", boom)
        monkeypatch.setattr(
            "aios.services.research.readiness_error", lambda session: ""
        )
        # Must not propagate - APScheduler would lose the job otherwise.
        scheduler_module._scheduled_research_job(99999)

    def test_a_concurrent_run_is_skipped_not_queued(self, db, session, monkeypatch):
        from aios.services.run_manager import RunAlreadyActive, RunManager

        def busy(self, topic_id, trigger_type="manual"):
            raise RunAlreadyActive(1)

        monkeypatch.setattr(RunManager, "start_research_run", busy)
        monkeypatch.setattr(
            "aios.services.research.readiness_error", lambda session: ""
        )

        topic = topics_repo.create_topic(session, name="每日主题")
        topics_repo.set_schedule(session, topic, True, "08:00")
        session.commit()

        scheduler_module._scheduled_research_job(topic.id)  # logs and returns


class TestSimpleScheduleWiring:
    def test_the_home_toggle_installs_a_job(self, client, session, monkeypatch):
        """The switch in 简易版 must do something real."""
        reloaded: list[bool] = []
        from aios.services.scheduler import scheduler

        monkeypatch.setattr(
            scheduler, "reload", lambda: reloaded.append(True) or None
        )

        topic = topics_repo.create_topic(session, name="每日主题")
        session.commit()

        client.post(
            f"/simple/topics/{topic.id}/schedule",
            data={"enabled": "on", "time": "08:30"},
            follow_redirects=False,
        )
        session.expire_all()

        assert topics_repo.get_topic(session, topic.id).schedule_enabled is True
        assert reloaded, "the scheduler was not asked to reload"
