"""The live Run Detail region.

Polling must start when the run is active, stop by itself when it is not, and
report only real progress. Nothing here asserts on a timer: the presence or
absence of ``hx-trigger`` in the rendered fragment *is* the behaviour.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.models import ModuleRunStatus, RunStatus


@pytest.fixture
def run_row(client, session):
    """A run with one module, ready to be driven through its states."""
    from aios.repositories import modules as modules_repo
    from aios.repositories import runs as runs_repo

    module = modules_repo.get_module_by_key(session, "mobile")
    run = runs_repo.create_run(session, dt.date(2026, 9, 15), "manual")
    module_run = runs_repo.create_module_run(session, run.id, module.id, module.name)
    session.commit()
    return {"run_id": run.id, "module_run_id": module_run.id, "module_id": module.id}


def set_status(session, run_id, status, stage="collecting"):
    from aios.repositories import runs as runs_repo

    run = runs_repo.get_run(session, run_id)
    run.status = status
    run.stage = stage
    if status in RunStatus.TERMINAL:
        from aios.timeutil import utcnow

        run.finished_at = utcnow()
    session.commit()


# --- 27-29. polling starts and stops ----------------------------------------

class TestPolling:
    @pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
    def test_an_active_run_polls(self, client, session, run_row, status):
        set_status(session, run_row["run_id"], status)
        response = client.get(f"/runs/{run_row['run_id']}/live")

        assert response.status_code == 200
        assert 'hx-trigger="every 2s"' in response.text
        assert f'hx-get="/runs/{run_row["run_id"]}/live"' in response.text
        assert 'hx-swap="outerHTML"' in response.text

    @pytest.mark.parametrize(
        "status",
        [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ],
    )
    def test_a_terminal_run_does_not_poll(self, client, session, run_row, status):
        set_status(session, run_row["run_id"], status, stage=status)
        response = client.get(f"/runs/{run_row['run_id']}/live")

        assert response.status_code == 200
        assert "hx-trigger" not in response.text, f"{status} must stop polling"
        assert "/live" not in response.text.split("data-run-finished")[0][-400:]

    def test_the_run_detail_page_embeds_the_live_region(self, client, session, run_row):
        set_status(session, run_row["run_id"], RunStatus.RUNNING)
        page = client.get(f"/runs/{run_row['run_id']}")
        assert page.status_code == 200
        assert 'id="run-live"' in page.text
        assert 'hx-trigger="every 2s"' in page.text

    def test_the_log_stream_polls_only_while_active(self, client, session, run_row):
        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)
        assert 'hx-get="/runs/%s/logs' % run_id in client.get(f"/runs/{run_id}/logs").text

        set_status(session, run_id, RunStatus.COMPLETED, stage="completed")
        assert "hx-trigger" not in client.get(f"/runs/{run_id}/logs").text

    def test_a_finished_run_marks_itself_finished_for_the_one_time_refresh(
        self, client, session, run_row
    ):
        set_status(session, run_row["run_id"], RunStatus.COMPLETED, stage="completed")
        response = client.get(f"/runs/{run_row['run_id']}/live")
        assert 'data-run-finished="1"' in response.text


# --- 30. source health appears live -----------------------------------------

class TestLiveSourceHealth:
    def test_source_health_changes_between_polls(self, client, session, run_row):
        from aios.repositories import sources as sources_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)

        first = client.get(f"/runs/{run_id}/live")
        assert "数据源状态" not in first.text, "nothing recorded yet, nothing claimed"

        sources_repo.upsert_stat(
            session, run_id,
            {
                "collector": "google_news", "display_name": "Google News",
                "state": "healthy", "requests_attempted": 23,
                "requests_succeeded": 21, "timeouts": 2,
            },
        )
        session.commit()

        second = client.get(f"/runs/{run_id}/live")
        assert "数据源状态" in second.text
        assert "Google News" in second.text
        assert "23 次请求" in second.text
        assert "21 成功" in second.text
        assert "2 超时" in second.text

        sources_repo.upsert_stat(
            session, run_id,
            {
                "collector": "gdelt", "display_name": "GDELT", "state": "rate_limited",
                "requests_attempted": 21, "requests_succeeded": 7, "rate_limited": 14,
            },
        )
        session.commit()

        third = client.get(f"/runs/{run_id}/live")
        assert "GDELT" in third.text
        assert "14 次 429" in third.text
        assert "受限" in third.text

    def test_no_percentage_is_invented_when_nothing_was_attempted(
        self, client, session, run_row
    ):
        from aios.repositories import sources as sources_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)
        sources_repo.upsert_stat(
            session, run_id,
            {"collector": "rss", "display_name": "RSS / Atom", "state": "unknown"},
        )
        session.commit()

        response = client.get(f"/runs/{run_id}/live")
        assert "本次未发起请求" in response.text
        assert "100%" not in response.text

    def test_a_paused_source_says_so(self, client, session, run_row):
        from aios.repositories import sources as sources_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)
        sources_repo.upsert_stat(
            session, run_id,
            {
                "collector": "google_news", "display_name": "Google News",
                "state": "circuit_open", "circuit_open_skips": 40,
            },
        )
        session.commit()

        response = client.get(f"/runs/{run_id}/live")
        assert "已暂停" in response.text
        assert "40" in response.text


# --- module rows and real progress ------------------------------------------

class TestLiveProgress:
    def test_module_rows_update_between_polls(self, client, session, run_row):
        from aios.repositories import runs as runs_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)

        module_run = runs_repo.get_module_run(session, run_row["module_run_id"])
        module_run.status = ModuleRunStatus.COLLECTING
        module_run.candidate_count = 0
        session.commit()
        assert "采集中" in client.get(f"/runs/{run_id}/live").text

        module_run = runs_repo.get_module_run(session, run_row["module_run_id"])
        module_run.status = ModuleRunStatus.ANALYZING
        module_run.candidate_count = 18
        module_run.article_count = 12
        session.commit()

        later = client.get(f"/runs/{run_id}/live").text
        assert "分析中" in later
        assert ">18<" in later.replace(" ", "").replace("\n", "")

    def test_progress_is_a_real_fraction_of_planned_modules(self, client, session):
        from aios.repositories import modules as modules_repo
        from aios.repositories import runs as runs_repo

        run = runs_repo.create_run(session, dt.date(2026, 9, 15), "manual")
        run.status = RunStatus.RUNNING
        run.stage = "collecting:mobile"
        modules = modules_repo.list_modules(session)[:8]
        for index, module in enumerate(modules):
            module_run = runs_repo.create_module_run(session, run.id, module.id, module.name)
            if index < 6:
                module_run.status = ModuleRunStatus.COMPLETED
        session.commit()

        response = client.get(f"/runs/{run.id}/live")
        assert "6 / 8" in response.text
        assert "75.0%" in response.text, "the bar is drawn from the real fraction"

    def test_a_module_with_warnings_shows_its_collection_status(
        self, client, session, run_row
    ):
        from aios.repositories import runs as runs_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)
        module_run = runs_repo.get_module_run(session, run_row["module_run_id"])
        module_run.status = ModuleRunStatus.COMPLETED_WITH_WARNINGS
        module_run.collection_status = "partial_collection"
        session.commit()

        response = client.get(f"/runs/{run_id}/live")
        assert "部分数据源异常" in response.text

    def test_the_stage_list_reflects_the_current_stage(self, client, session, run_row):
        run_id = run_row["run_id"]
        for stage, label in (
            ("checking_sources", "检测数据源"),
            ("collecting:mobile", "采集来源"),
            ("analyzing:mobile", "AI 分析"),
            ("synthesizing", "综合研判"),
        ):
            set_status(session, run_id, RunStatus.RUNNING, stage=stage)
            text = client.get(f"/runs/{run_id}/live").text
            assert label in text
            assert "进行中" in text


# --- 31. new log lines appear live ------------------------------------------

class TestLiveLogs:
    def test_new_log_lines_appear_without_a_page_reload(self, client, session, run_row):
        from aios.repositories import runs as runs_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)

        first = runs_repo.add_log(session, run_id, "Collecting 移动智能终端侧")
        session.commit()

        response = client.get(f"/runs/{run_id}/logs?after=0")
        assert "Collecting 移动智能终端侧" in response.text
        assert f"after={first.id}" in response.text

        runs_repo.add_log(session, run_id, "  HarmonyOS: 12 candidates -> 8 articles")
        session.commit()

        incremental = client.get(f"/runs/{run_id}/logs?after={first.id}")
        assert "12 candidates" in incremental.text
        assert "Collecting 移动智能终端侧" not in incremental.text, "only new lines are sent"

    def test_the_log_view_does_not_force_scroll_when_the_reader_scrolled_up(self):
        """The autoscroll must respect a reader who scrolled back."""
        from pathlib import Path

        script = Path("aios/static/js/app.js").read_text(encoding="utf-8")
        assert "htmx:beforeSwap" in script
        assert "dataset.pinned" in script
        assert "atBottom" in script


class TestLogAppending:
    """The log panel must accumulate, not show only the last poll's lines."""

    def test_the_poller_is_empty_so_new_lines_append_in_front_of_it(
        self, client, session, run_row
    ):
        from aios.repositories import runs as runs_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)
        first = runs_repo.add_log(session, run_id, "line one")
        session.commit()

        body = client.get(f"/runs/{run_id}/logs?after=0").text
        # The lines come before the polled element, never inside it: a swap of
        # the poller must not be able to take the history with it.
        assert body.index("line one") < body.index('id="run-log-stream"')
        tail = body[body.index('id="run-log-stream"'):]
        assert "log-line" not in tail, "the polled element must stay empty"
        assert f"after={first.id}" in body

    def test_history_survives_repeated_polls(self, client, session, run_row):
        """Simulate the browser: append each response, nothing is lost."""
        from aios.repositories import runs as runs_repo

        run_id = run_row["run_id"]
        set_status(session, run_id, RunStatus.RUNNING)

        rendered = ""
        after = 0
        for index in range(3):
            runs_repo.add_log(session, run_id, f"事件 {index}")
            session.commit()
            body = client.get(f"/runs/{run_id}/logs?after={after}").text
            # The browser replaces the poller with this response.
            rendered = rendered.replace(
                rendered[rendered.index('<div id="run-log-stream"'):] if
                '<div id="run-log-stream"' in rendered else "", ""
            ) + body
            after = int(body.split("after=")[1].split('"')[0])

        for index in range(3):
            assert f"事件 {index}" in rendered
