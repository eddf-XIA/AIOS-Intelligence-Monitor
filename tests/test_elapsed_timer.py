"""The live 用时 clock on Run Detail.

The bug this pins down: 用时 was rendered once, server-side, in the page
*header* - which sits outside the polled ``#run-live`` region. Loading the page
the moment a run started therefore froze the clock at ``0 秒`` for the whole
run.

The fix keeps the 2s state poll as-is and lets the browser derive the clock
from a timestamp. So the tests here check two things the server owns:

* the markup carries an unambiguous instant and an honest live/terminal flag;
* the Python and JavaScript formatters cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from aios.models import RunStatus
from aios.timeutil import cn_duration_text, duration_text, iso_utc

APP_JS = Path(__file__).resolve().parent.parent / "aios" / "static" / "js" / "app.js"

def strip_js_comments(source: str) -> str:
    """Drop /* block */ and // line comments so checks look at code only."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", " ", source)



@pytest.fixture
def run_row(client, session):
    from aios.repositories import modules as modules_repo
    from aios.repositories import runs as runs_repo

    module = modules_repo.get_module_by_key(session, "mobile")
    run = runs_repo.create_run(session, dt.date(2026, 9, 15), "manual")
    runs_repo.create_module_run(session, run.id, module.id, module.name)
    session.commit()
    return run.id


def start_run(session, run_id, seconds_ago, status=RunStatus.RUNNING, ran_for=None):
    """Place a run at a known point in time.

    ``started_at`` is captured once and ``finished_at`` derived from it, so the
    stored duration is exactly ``ran_for`` seconds - calling ``utcnow()`` twice
    would leave a few microseconds behind and truncate 161s to 160s.
    """
    from aios.repositories import runs as runs_repo
    from aios.timeutil import utcnow

    started = utcnow() - dt.timedelta(seconds=seconds_ago)
    run = runs_repo.get_run(session, run_id)
    run.status = status
    run.stage = "collecting" if status in RunStatus.ACTIVE else status
    run.started_at = started
    run.finished_at = (
        started + dt.timedelta(seconds=ran_for) if ran_for is not None else None
    )
    session.commit()
    return run


# --- formatting -------------------------------------------------------------

class TestChineseDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0 秒"),
            (1, "1 秒"),
            (27, "27 秒"),
            (59, "59 秒"),
            (60, "1 分 00 秒"),
            (68, "1 分 08 秒"),
            (161, "2 分 41 秒"),
            (3600, "1 小时 00 分"),
            (4080, "1 小时 08 分"),
            (7260, "2 小时 01 分"),
        ],
    )
    def test_matches_the_specified_format(self, seconds, expected):
        assert cn_duration_text(seconds) == expected

    def test_no_milliseconds_ever_appear(self):
        assert cn_duration_text(27.999) == "27 秒"
        assert "." not in cn_duration_text(161.5)

    def test_a_negative_duration_renders_as_nothing(self):
        assert cn_duration_text(-5) == ""

    def test_duration_text_measures_an_open_run_against_now(self):
        from aios.timeutil import utcnow

        started = utcnow() - dt.timedelta(seconds=68)
        assert duration_text(started, None) == "1 分 08 秒"

    def test_duration_text_is_fixed_once_the_run_has_finished(self):
        started = dt.datetime(2026, 9, 15, 10, 0, 0)
        finished = started + dt.timedelta(seconds=161)
        assert duration_text(started, finished) == "2 分 41 秒"

    def test_missing_start_renders_as_nothing(self):
        assert duration_text(None, None) == ""


class TestFormatterParity:
    """Python and JavaScript render the same value; they must not drift."""

    def _js_source(self) -> str:
        return APP_JS.read_text(encoding="utf-8")

    def test_the_javascript_formatter_exists_and_is_documented_as_a_mirror(self):
        js = self._js_source()
        assert "function formatElapsed(" in js
        assert "cn_duration_text" in js, "the JS names its Python counterpart"

    def test_both_formatters_use_the_same_units_and_padding(self):
        js = self._js_source()
        for unit in ('" 秒"', '" 分 "', '" 小时 "', '" 分"'):
            assert unit in js, f"missing unit {unit} in the JS formatter"
        assert "pad2(" in js, "the JS zero-pads the minor component like Python does"

    def test_the_clock_is_derived_not_incremented(self):
        """`seconds += 1` drifts in a throttled background tab."""
        # The source deliberately *mentions* the anti-pattern to explain why it
        # is not used, so only the code is checked.
        js = strip_js_comments(self._js_source())

        assert "Date.now() - startedMs" in js
        assert not re.search(r"seconds\s*\+=", js)
        assert not re.search(r"seconds\s*\+\+|\+\+\s*seconds", js)

    def test_only_one_interval_can_ever_exist(self):
        js = self._js_source()
        assert "elapsedTimer = null" in js
        assert "elapsedTimer === null" in js, "a timer is only created when there is none"
        assert "clearInterval(elapsedTimer)" in js

    def test_the_clock_reinitialises_after_htmx_swaps(self):
        js = self._js_source()
        assert 'htmx:afterSwap", syncElapsed' in js

    def test_a_background_tab_catches_up_on_wake(self):
        js = self._js_source()
        assert "visibilitychange" in js
        assert "document.hidden" in js


# --- markup the browser needs -----------------------------------------------

class TestRunDetailMarkup:
    def test_an_active_run_exposes_a_parseable_start_instant(
        self, client, session, run_row
    ):
        run = start_run(session, run_row, seconds_ago=9)
        page = client.get(f"/runs/{run_row}")

        assert page.status_code == 200
        assert "data-elapsed" in page.text
        assert f'data-elapsed-start="{iso_utc(run.started_at)}"' in page.text
        assert 'data-elapsed-live="1"' in page.text

    def test_the_start_instant_carries_an_explicit_utc_offset(
        self, client, session, run_row
    ):
        """Without an offset the browser would read UTC as local time."""
        start_run(session, run_row, seconds_ago=30)
        page = client.get(f"/runs/{run_row}")

        match = re.search(r'data-elapsed-start="([^"]+)"', page.text)
        assert match, "no start instant in the markup"
        value = match.group(1)
        assert value.endswith("+00:00"), value
        parsed = dt.datetime.fromisoformat(value)
        assert parsed.tzinfo is not None

    def test_the_server_still_renders_the_current_value_for_first_paint(
        self, client, session, run_row
    ):
        """With JS off, or before the first tick, the number must be right."""
        start_run(session, run_row, seconds_ago=68)
        page = client.get(f"/runs/{run_row}")
        assert "1 分 08 秒" in page.text
        assert "用时" in page.text

    @pytest.mark.parametrize(
        "status",
        [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ],
    )
    def test_a_terminal_run_is_marked_not_live(self, client, session, run_row, status):
        start_run(session, run_row, seconds_ago=600, status=status, ran_for=161)
        page = client.get(f"/runs/{run_row}")

        assert 'data-elapsed-live="0"' in page.text
        assert 'data-elapsed-live="1"' not in page.text
        assert "2 分 41 秒" in page.text

    def test_the_final_duration_does_not_drift_after_completion(
        self, client, session, run_row
    ):
        """Two requests minutes apart must show the same finished duration."""
        start_run(session, run_row, seconds_ago=600, status=RunStatus.COMPLETED,
                  ran_for=161)

        def clock_text(body: str) -> str:
            # Scoped to the timer element: "开始于 10 分钟前" elsewhere on the
            # page is a relative timestamp, not the elapsed clock.
            match = re.search(
                r"<span\s+data-elapsed[^>]*>([^<]*)</span>", body, re.DOTALL
            )
            assert match, "no elapsed element in the page"
            return match.group(1).strip()

        first = clock_text(client.get(f"/runs/{run_row}").text)
        second = clock_text(client.get(f"/runs/{run_row}").text)

        assert first == "2 分 41 秒"
        assert second == first, "the finished duration moved between requests"


class TestLiveMarkerSynchronisation:
    """The polled region carries the clock's authoritative state."""

    def test_the_live_fragment_publishes_clock_state(self, client, session, run_row):
        run = start_run(session, run_row, seconds_ago=5)
        fragment = client.get(f"/runs/{run_row}/live").text

        assert 'id="run-elapsed-sync"' in fragment
        assert 'data-elapsed-live="1"' in fragment
        assert f'data-elapsed-start="{iso_utc(run.started_at)}"' in fragment

    def test_the_fragment_flips_to_terminal_with_the_canonical_duration(
        self, client, session, run_row
    ):
        start_run(session, run_row, seconds_ago=600, status=RunStatus.COMPLETED,
                  ran_for=161)

        fragment = client.get(f"/runs/{run_row}/live").text
        assert 'data-elapsed-live="0"' in fragment
        assert 'data-elapsed-final="2 分 41 秒"' in fragment

    def test_polling_and_the_clock_stop_together(self, client, session, run_row):
        """Both must go quiet on completion - neither may outlive the run."""
        start_run(session, run_row, seconds_ago=5)
        running = client.get(f"/runs/{run_row}/live").text
        assert 'hx-trigger="every 2s"' in running
        assert 'data-elapsed-live="1"' in running

        start_run(session, run_row, seconds_ago=30, status=RunStatus.COMPLETED,
                  ran_for=30)
        finished = client.get(f"/runs/{run_row}/live").text
        assert "hx-trigger" not in finished
        assert 'data-elapsed-live="0"' in finished


class TestTerminalPageRefresh:
    """Page chrome outside the polled region must catch up when a run ends.

    The clock is only half the story: the status badge, the output counters and
    the report link all live in the header. A one-time reload brings them into
    line once the run is terminal.
    """

    def _js_source(self) -> str:
        return APP_JS.read_text(encoding="utf-8")

    def test_the_refresh_looks_the_marker_up_in_the_document(self):
        """`event.detail.target` is not the swapped node for an outerHTML swap."""
        js = self._js_source()
        assert "data-run-finished=" in js
        assert "document.querySelector(" in js

        # Comments are stripped first: the source explains the pitfall by name,
        # so a naive substring search would flag its own documentation.
        code = strip_js_comments(js)
        assert "event.detail.target" not in code, (
            "the reload must not key off the swap event's target"
        )
        assert "data-run-finished" in code, "the marker is still consulted in code"

    def test_the_refresh_happens_at_most_once(self):
        js = self._js_source()
        assert "window.__aiosReloaded" in js
        assert "if (window.__aiosReloaded) return;" in js

    def test_a_finished_run_publishes_the_marker_the_refresh_looks_for(
        self, client, session, run_row
    ):
        start_run(session, run_row, seconds_ago=200, status=RunStatus.COMPLETED,
                  ran_for=161)
        fragment = client.get(f"/runs/{run_row}/live").text
        assert 'data-run-finished="1"' in fragment

    def test_an_active_run_does_not_publish_the_marker(self, client, session, run_row):
        start_run(session, run_row, seconds_ago=5)
        fragment = client.get(f"/runs/{run_row}/live").text
        assert "data-run-finished" not in fragment
        assert 'hx-trigger="every 2s"' in fragment
